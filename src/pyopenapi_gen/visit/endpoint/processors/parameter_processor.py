"""
Helper class for processing parameters for an endpoint method.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Tuple

from pyopenapi_gen.core.utils import NameSanitizer
from pyopenapi_gen.helpers.endpoint_utils import get_param_type, get_request_body_type
from pyopenapi_gen.helpers.url_utils import extract_url_variables
from pyopenapi_gen.types.services.type_service import UnifiedTypeService

if TYPE_CHECKING:
    from pyopenapi_gen import IROperation, IRSchema
    from pyopenapi_gen.context.render_context import RenderContext

logger = logging.getLogger(__name__)


class EndpointParameterProcessor:
    """
    Processes IROperation parameters and request body to prepare a list of
    method parameters for the endpoint signature and further processing.
    """

    def __init__(self, schemas: dict[str, Any] | None = None) -> None:
        self.schemas: dict[str, Any] = schemas or {}

    def process_parameters(
        self, op: IROperation, context: RenderContext
    ) -> Tuple[List[dict[str, Any]], str | None, str | None, bool]:
        """
        Prepares and orders parameters for an endpoint method, including path,
        query, header, and request body parameters.

        Returns:
            A tuple containing:
            - ordered_params: List of parameter dictionaries for method signature.
            - primary_content_type: The dominant content type for the request body.
            - resolved_body_type: The Python type hint for the request body.
            - body_exploded: Whether the JSON body was exploded into individual params.
        """
        ordered_params: List[dict[str, Any]] = []
        param_details_map: dict[str, dict[str, Any]] = {}

        for param in op.parameters:
            param_name_sanitized = NameSanitizer.sanitize_method_name(param.name)
            param_info = {
                "name": param_name_sanitized,
                "type": get_param_type(param, context, self.schemas),
                "required": param.required,
                "default": param.schema.default if param.schema else None,
                "param_in": param.param_in,
                "original_name": param.name,
            }
            ordered_params.append(param_info)
            param_details_map[param_name_sanitized] = param_info

        primary_content_type: str | None = None
        resolved_body_type: str | None = None
        body_exploded: bool = False

        if op.request_body:
            content_types = op.request_body.content.keys()
            body_param_name = "body"  # Default name
            context.add_import("typing", "Any")  # General fallback
            body_specific_param_info: dict[str, Any] | None = None

            if "multipart/form-data" in content_types:
                primary_content_type = "multipart/form-data"
                body_param_name = "files"
                context.add_import("typing", "Dict")
                context.add_import("typing", "IO")
                resolved_body_type = "dict[str, IO[Any]]"
                body_specific_param_info = {
                    "name": body_param_name,
                    "type": resolved_body_type,
                    "required": op.request_body.required,
                    "default": None,
                    "param_in": "body",
                    "original_name": body_param_name,
                }
            elif "application/json" in content_types:
                primary_content_type = "application/json"
                body_param_name = "body"
                resolved_body_type = get_request_body_type(op.request_body, context, self.schemas)

                # Try to explode body schema into individual parameters
                json_schema = op.request_body.content.get("application/json")
                resolved_schema = self._resolve_body_schema(json_schema) if json_schema else None

                if resolved_schema and self._can_explode_body(resolved_schema):
                    exploded_params = self._explode_body_params(
                        resolved_schema, resolved_body_type, context, param_details_map, op
                    )
                    if exploded_params is not None:
                        # No collisions — use exploded params
                        body_exploded = True
                        body_specific_param_info = None  # Don't add single body param
                        # Add exploded params after non-body params later
                        for ep in exploded_params:
                            ordered_params.append(ep)
                            param_details_map[ep["name"]] = ep
                    else:
                        # Collision detected — fall back to single body param
                        body_specific_param_info = {
                            "name": body_param_name,
                            "type": resolved_body_type,
                            "required": op.request_body.required,
                            "default": None,
                            "param_in": "body",
                            "original_name": body_param_name,
                        }
                else:
                    body_specific_param_info = {
                        "name": body_param_name,
                        "type": resolved_body_type,
                        "required": op.request_body.required,
                        "default": None,
                        "param_in": "body",
                        "original_name": body_param_name,
                    }
            elif "application/x-www-form-urlencoded" in content_types:
                primary_content_type = "application/x-www-form-urlencoded"
                body_param_name = "form_data"
                context.add_import("typing", "Dict")
                resolved_body_type = "dict[str, Any]"
                body_specific_param_info = {
                    "name": body_param_name,
                    "type": resolved_body_type,
                    "required": op.request_body.required,
                    "default": None,
                    "param_in": "body",
                    "original_name": body_param_name,
                }
            elif content_types:  # Fallback for other content types
                primary_content_type = list(content_types)[0]
                body_param_name = "bytes_content"  # e.g. for application/octet-stream
                resolved_body_type = "bytes"
                body_specific_param_info = {
                    "name": body_param_name,
                    "type": resolved_body_type,
                    "required": op.request_body.required,
                    "default": None,
                    "param_in": "body",
                    "original_name": body_param_name,
                }

            if body_specific_param_info:
                if body_specific_param_info["name"] not in param_details_map:
                    ordered_params.append(body_specific_param_info)
                    param_details_map[body_specific_param_info["name"]] = body_specific_param_info
                else:
                    logger.warning(
                        f"Request body parameter name '{body_specific_param_info['name']}' "
                        f"for operation '{op.operation_id}'"
                        f"collides with an existing path/query/header parameter. Check OpenAPI spec."
                    )

        final_ordered_params = self._ensure_path_variables_as_params(op, ordered_params, param_details_map)

        # Sort: non-body params (required first, then optional),
        # then body_field params (required first, then optional)
        non_body_params = [p for p in final_ordered_params if p.get("param_in") != "body_field"]
        body_field_params = [p for p in final_ordered_params if p.get("param_in") == "body_field"]
        non_body_params.sort(key=lambda p: not p["required"])
        body_field_params.sort(key=lambda p: not p["required"])
        final_ordered_params = non_body_params + body_field_params

        return final_ordered_params, primary_content_type, resolved_body_type, body_exploded

    def _resolve_body_schema(self, schema: IRSchema | None) -> IRSchema | None:
        """Resolve a body schema through references to get the actual object schema."""
        if schema is None:
            return None

        # Follow _refers_to_schema chain
        resolved = schema
        seen: set[int] = set()
        while resolved._refers_to_schema is not None and id(resolved) not in seen:
            seen.add(id(resolved))
            resolved = resolved._refers_to_schema

        # If the resolved schema has no properties but has a name, look it up in self.schemas
        if not resolved.properties and resolved.name and resolved.name in self.schemas:
            resolved = self.schemas[resolved.name]

        # Also try generation_name lookup
        if not resolved.properties and resolved.generation_name:
            for s in self.schemas.values():
                if s.generation_name == resolved.generation_name and s.properties:
                    resolved = s
                    break

        return resolved if resolved.properties else None

    def _can_explode_body(self, schema: IRSchema) -> bool:
        """Check if a body schema can be exploded into individual parameters."""
        # Must have properties
        if not schema.properties:
            return False

        # No composition types
        if schema.any_of or schema.one_of or schema.all_of:
            return False

        # No open additional properties
        if schema.additional_properties is True:
            return False

        # No circular references
        if schema._is_circular_ref or schema._is_self_referential_stub:
            return False

        return True

    def _explode_body_params(
        self,
        resolved_schema: IRSchema,
        resolved_body_type: str | None,
        context: RenderContext,
        existing_param_map: dict[str, dict[str, Any]],
        op: IROperation | None = None,
    ) -> list[dict[str, Any]] | None:
        """
        Explode body schema properties into individual parameters.

        Returns a list of param dicts with param_in="body_field", or None if
        there's a name collision with existing params.
        """
        type_service = UnifiedTypeService(self.schemas)
        exploded: list[dict[str, Any]] = []

        # Also check collision with URL path variables not yet in param map
        path_var_names: set[str] = set()
        if op is not None:
            for var in extract_url_variables(op.path):
                path_var_names.add(NameSanitizer.sanitize_method_name(var))

        for prop_name, prop_schema in resolved_schema.properties.items():
            sanitized_name = NameSanitizer.sanitize_method_name(prop_name)

            # Check for collision with existing path/query/header params or path variables
            if sanitized_name in existing_param_map or sanitized_name in path_var_names:
                return None  # Fall back silently

            is_required = prop_name in resolved_schema.required
            prop_type = type_service.resolve_schema_type(prop_schema, context, required=is_required)

            exploded.append(
                {
                    "name": sanitized_name,
                    "type": prop_type,
                    "required": is_required,
                    "default": None,
                    "param_in": "body_field",
                    "original_name": prop_name,
                    "body_model_type": resolved_body_type,
                    "description": prop_schema.description or "",
                }
            )

        return exploded

    def _ensure_path_variables_as_params(
        self, op: IROperation, current_params: List[dict[str, Any]], param_details_map: dict[str, dict[str, Any]]
    ) -> List[dict[str, Any]]:
        """
        Ensures that all variables in the URL path are present in the list of parameters.
        If a path variable is not already defined as a parameter, it's added as a required string type.
        This also updates the param_details_map.
        """
        url_vars = extract_url_variables(op.path)

        # Make a copy to modify if necessary
        updated_params = list(current_params)

        for var in url_vars:
            sanitized_var_name = NameSanitizer.sanitize_method_name(var)
            if sanitized_var_name not in param_details_map:
                path_var_param_info = {
                    "name": sanitized_var_name,
                    "type": "str",  # Path variables are typically strings
                    "required": True,  # Path variables are always required
                    "default": None,
                    "param_in": "path",
                    "original_name": var,
                }
                updated_params.append(path_var_param_info)
                param_details_map[sanitized_var_name] = path_var_param_info
                # logger.debug(
                #     f"Added missing path variable '{sanitized_var_name}' "
                #     f"to parameters for operation '{op.operation_id}'."
                # )

        return updated_params
