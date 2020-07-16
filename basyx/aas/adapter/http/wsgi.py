# Copyright 2020 PyI40AAS Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.


import io
import json
from lxml import etree  # type: ignore
import urllib.parse
import werkzeug
from werkzeug.exceptions import BadRequest, InternalServerError, NotFound, NotImplemented
from werkzeug.routing import Rule, Submount
from werkzeug.wrappers import Request, Response

from aas import model
from ..xml import xml_deserialization
from ..json import json_deserialization
from .._generic import IDENTIFIER_TYPES, IDENTIFIER_TYPES_INVERSE
from .response import get_response_type, http_exception_to_response

from typing import Dict, Optional, Type


def parse_request_body(request: Request, expect_type: Type[model.base._RT]) -> model.base._RT:
    """
    TODO: werkzeug documentation recommends checking the content length before retrieving the body to prevent
          running out of memory. but it doesn't state how to check the content length
          also: what would be a reasonable maximum content length? the request body isn't limited by the xml/json schema
    """
    xml_constructors = {
        model.Submodel: xml_deserialization._construct_submodel,
        model.View: xml_deserialization._construct_view,
        model.ConceptDictionary: xml_deserialization._construct_concept_dictionary,
        model.ConceptDescription: xml_deserialization._construct_concept_description,
        model.SubmodelElement: xml_deserialization._construct_submodel_element
    }

    valid_content_types = ("application/json", "application/xml", "text/xml")

    if request.mimetype not in valid_content_types:
        raise werkzeug.exceptions.UnsupportedMediaType(f"Invalid content-type: {request.mimetype}! Supported types: "
                                                       + ", ".join(valid_content_types))

    if request.mimetype == "application/json":
        json_data = request.get_data()
        try:
            rv = json.loads(json_data, cls=json_deserialization.AASFromJsonDecoder)
        except json.decoder.JSONDecodeError as e:
            raise BadRequest(str(e)) from e
    else:
        parser = etree.XMLParser(remove_blank_text=True, remove_comments=True)
        xml_data = io.BytesIO(request.get_data())
        try:
            tree = etree.parse(xml_data, parser)
        except etree.XMLSyntaxError as e:
            raise BadRequest(str(e)) from e
        # TODO: check tag of root element
        root = tree.getroot()
        try:
            rv = xml_constructors[expect_type](root, failsafe=False)
        except (KeyError, ValueError) as e:
            raise BadRequest(xml_deserialization._exception_to_str(e)) from e

    assert(isinstance(rv, expect_type))
    return rv


def identifier_uri_encode(id_: model.Identifier) -> str:
    return IDENTIFIER_TYPES[id_.id_type] + ":" + urllib.parse.quote(id_.id, safe="")


def identifier_uri_decode(id_str: str) -> model.Identifier:
    try:
        id_type_str, id_ = id_str.split(":", 1)
    except ValueError as e:
        raise ValueError(f"Identifier '{id_str}' is not of format 'ID_TYPE:ID'")
    id_type = IDENTIFIER_TYPES_INVERSE.get(id_type_str)
    if id_type is None:
        raise ValueError(f"Identifier Type '{id_type_str}' is invalid")
    return model.Identifier(urllib.parse.unquote(id_), id_type)


class IdentifierConverter(werkzeug.routing.UnicodeConverter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def to_url(self, value: model.Identifier) -> str:
        return super().to_url(identifier_uri_encode(value))

    def to_python(self, value: str) -> model.Identifier:
        try:
            return identifier_uri_decode(super().to_python(value))
        except ValueError as e:
            raise BadRequest(str(e))


class WSGIApp:
    def __init__(self, object_store: model.AbstractObjectStore):
        self.object_store: model.AbstractObjectStore = object_store
        self.url_map = werkzeug.routing.Map([
            Submount("/api/v1.0", [
                Submount("/shells/<identifier:aas_id>", [
                    Rule("/aas", methods=["GET"], endpoint=self.get_aas),
                    Submount("/aas", [
                        Rule("/asset", methods=["GET"], endpoint=self.get_aas_asset),
                        Rule("/submodels", methods=["GET"], endpoint=self.get_aas_submodels),
                        Rule("/submodels", methods=["PUT"], endpoint=self.put_aas_submodels),
                        Rule("/views", methods=["GET"], endpoint=self.get_aas_views),
                        Rule("/views/<string(minlength=1):id_short>", methods=["GET"],
                             endpoint=self.get_aas_views_specific),
                        Rule("/views/<string(minlength=1):id_short>", methods=["DELETE"],
                             endpoint=self.delete_aas_views_specific),
                        Rule("/conceptDictionaries", methods=["GET"], endpoint=self.get_aas_concept_dictionaries),
                        Rule("/conceptDictionaries", methods=["PUT"], endpoint=self.put_aas_concept_dictionaries),
                        Rule("/conceptDictionaries/<string(minlength=1):id_short>", methods=["GET"],
                             endpoint=self.get_aas_concept_dictionaries_specific),
                        Rule("/conceptDictionaries/<string(minlength=1):id_short>", methods=["DELETE"],
                             endpoint=self.delete_aas_concept_dictionaries_specific),
                        Rule("/submodels/<string(minlength=1):id_short>", methods=["GET"],
                             endpoint=self.get_aas_submodels_specific),
                        Rule("/submodels/<string(minlength=1):id_short>", methods=["DELETE"],
                             endpoint=self.delete_aas_submodels_specific),
                    ])
                ]),
                Submount("/submodels/<identifier:submodel_id>", [
                    Rule("/submodel", methods=["GET"], endpoint=self.get_submodel),
                    Submount("/submodel", [

                    ])
                ])
            ])
        ], converters={"identifier": IdentifierConverter})

    def __call__(self, environ, start_response):
        response = self.handle_request(Request(environ))
        return response(environ, start_response)

    # this is not used yet
    @classmethod
    def mandatory_request_param(cls, request: Request, param: str) -> str:
        req_param = request.args.get(param)
        if req_param is None:
            raise BadRequest(f"Parameter '{param}' is mandatory")
        return req_param

    def get_obj_ts(self, identifier: model.Identifier, type_: Type[model.provider._IT]) -> model.provider._IT:
        identifiable = self.object_store.get(identifier)
        if not isinstance(identifiable, type_):
            raise NotFound(f"No {type_.__name__} with {identifier} found!")
        return identifiable

    def resolve_reference(self, reference: model.AASReference[model.base._RT]) -> model.base._RT:
        try:
            return reference.resolve(self.object_store)
        except (KeyError, TypeError, model.base.UnexpectedTypeError) as e:
            raise InternalServerError(xml_deserialization._exception_to_str(e)) from e

    def handle_request(self, request: Request):
        adapter = self.url_map.bind_to_environ(request.environ)
        try:
            endpoint, values = adapter.match()
            if endpoint is None:
                raise NotImplemented("This route is not yet implemented.")
            return endpoint(request, values)
        # any raised error that leaves this function will cause a 500 internal server error
        # so catch raised http exceptions and return them
        except werkzeug.exceptions.NotAcceptable as e:
            return e
        except werkzeug.exceptions.HTTPException as e:
            try:
                # get_response_type() may raise a NotAcceptable error, so we have to handle that
                return http_exception_to_response(e, get_response_type(request))
            except werkzeug.exceptions.NotAcceptable as e:
                return e

    def get_aas(self, request: Request, url_args: Dict) -> Response:
        # TODO: depth parameter
        response_t = get_response_type(request)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        return response_t(aas)

    def get_aas_asset(self, request: Request, url_args: Dict) -> Response:
        response_t = get_response_type(request)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        asset = self.resolve_reference(aas.asset)
        asset.update()
        return response_t(asset)

    def get_aas_submodels(self, request: Request, url_args: Dict) -> Response:
        # TODO: depth parameter
        response_t = get_response_type(request)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        submodels = [self.resolve_reference(ref) for ref in aas.submodel]
        for submodel in submodels:
            submodel.update()
        identification_id: Optional[str] = request.args.get("identification.id")
        if identification_id is not None:
            # mypy doesn't propagate type restrictions to nested functions: https://github.com/python/mypy/issues/2608
            submodels = filter(lambda s: identification_id in s.identification.id, submodels)  # type: ignore
        semantic_id: Optional[str] = request.args.get("semanticId")
        if semantic_id is not None:
            # mypy doesn't propagate type restrictions to nested functions: https://github.com/python/mypy/issues/2608
            submodels = filter(lambda s: s.semantic_id is not None  # type: ignore
                               and len(s.semantic_id.key) > 0
                               and semantic_id in s.semantic_id.key[0].value, submodels)  # type: ignore
        return response_t(list(submodels))

    def put_aas_submodels(self, request: Request, url_args: Dict) -> Response:
        response_t = get_response_type(request)
        new_submodel = parse_request_body(request, model.Submodel)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        current_submodel = None
        for s in iter(self.resolve_reference(ref) for ref in aas.submodel):
            if s.identification == new_submodel.identification:
                current_submodel = s
                break
        if current_submodel is None:
            aas.submodel.add(model.AASReference.from_referable(new_submodel))
            aas.commit()
            not_referenced_submodel = self.object_store.get(new_submodel.identification)
            assert(isinstance(not_referenced_submodel, model.Submodel))
            current_submodel = not_referenced_submodel
        if current_submodel is not None:
            self.object_store.discard(current_submodel)
        self.object_store.add(new_submodel)
        return response_t(new_submodel, status=201)

    def get_aas_views(self, request: Request, url_args: Dict) -> Response:
        # TODO: filter parameter
        response_t = get_response_type(request)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        if len(aas.view) == 0:
            raise NotFound("No views found!")
        return response_t(list(aas.view))

    def put_aas_views(self, request: Request, url_args: Dict) -> Response:
        response_t = get_response_type(request)
        new_view = parse_request_body(request, model.View)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        old_view = aas.view.get(new_view.id_short)
        if old_view is not None:
            aas.view.discard(old_view)
        aas.view.add(new_view)
        aas.commit()
        return response_t(new_view, status=201)

    def get_aas_views_specific(self, request: Request, url_args: Dict) -> Response:
        response_t = get_response_type(request)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        id_short = url_args["id_short"]
        view = aas.view.get(id_short)
        if view is None:
            raise NotFound(f"No view with idShort '{id_short}' found!")
        view.update()
        return response_t(view)

    def delete_aas_views_specific(self, request: Request, url_args: Dict) -> Response:
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        id_short = url_args["id_short"]
        view = aas.view.get(id_short)
        if view is None:
            raise NotFound(f"No view with idShort '{id_short}' found!")
        view.update()
        aas.view.remove(view.id_short)
        return Response(status=204)

    def get_aas_concept_dictionaries(self, request: Request, url_args: Dict) -> Response:
        # TODO: depth parameter
        response_t = get_response_type(request)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        if len(aas.concept_dictionary) == 0:
            raise NotFound("No concept dictionaries found!")
        return response_t(list(aas.concept_dictionary))

    def put_aas_concept_dictionaries(self, request: Request, url_args: Dict) -> Response:
        response_t = get_response_type(request)
        new_concept_dictionary = parse_request_body(request, model.ConceptDictionary)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        old_concept_dictionary = aas.concept_dictionary.get(new_concept_dictionary.id_short)
        if old_concept_dictionary is not None:
            aas.concept_dictionary.discard(old_concept_dictionary)
        aas.concept_dictionary.add(new_concept_dictionary)
        aas.commit()
        return response_t(new_concept_dictionary, status=201)

    def get_aas_concept_dictionaries_specific(self, request: Request, url_args: Dict) -> Response:
        response_t = get_response_type(request)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        id_short = url_args["id_short"]
        concept_dictionary = aas.concept_dictionary.get(id_short)
        if concept_dictionary is None:
            raise NotFound(f"No concept dictionary with idShort '{id_short}' found!")
        concept_dictionary.update()
        return response_t(concept_dictionary)

    def delete_aas_concept_dictionaries_specific(self, request: Request, url_args: Dict) -> Response:
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        id_short = url_args["id_short"]
        concept_dictionaries = aas.concept_dictionary.get(id_short)
        if concept_dictionaries is None:
            raise NotFound(f"No concept dictionary with idShort '{id_short}' found!")
        concept_dictionaries.update()
        aas.view.remove(concept_dictionaries.id_short)
        return Response(status=204)

    def get_aas_submodels_specific(self, request: Request, url_args: Dict) -> Response:
        response_t = get_response_type(request)
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        id_short = url_args["id_short"]
        for submodel in iter(self.resolve_reference(ref) for ref in aas.submodel):
            submodel.update()
            if submodel.id_short == id_short:
                return response_t(submodel)
        raise NotFound(f"No submodel with idShort '{id_short}' found!")

    def delete_aas_submodels_specific(self, request: Request, url_args: Dict) -> Response:
        aas = self.get_obj_ts(url_args["aas_id"], model.AssetAdministrationShell)
        aas.update()
        id_short = url_args["id_short"]
        for ref in aas.submodel:
            submodel = self.resolve_reference(ref)
            submodel.update()
            if submodel.id_short == id_short:
                aas.submodel.discard(ref)
                self.object_store.discard(submodel)
                return Response(status=204)
        raise NotFound(f"No submodel with idShort '{id_short}' found!")

    def get_submodel(self, request: Request, url_args: Dict) -> Response:
        # TODO: depth parameter
        response_t = get_response_type(request)
        submodel = self.get_obj_ts(url_args["submodel_id"], model.Submodel)
        submodel.update()
        return response_t(submodel)
