"""OpenAPI 3.1 generation from operation descriptors and transformed model schemas."""

from __future__ import annotations

import copy
import typing as t

from mongospecs_web.resource import Operation, Resource

if t.TYPE_CHECKING:
    from mongospecs_web.api import Api

__all__ = ["build_openapi", "operation_object"]

PROBLEM = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"field": {"type": ["string", "null"]}, "message": {"type": "string"}},
            },
        },
    },
    "required": ["type", "title", "status"],
}

_ERRORS: dict[str, tuple[str, ...]] = {
    "list": ("400",),
    "get": ("404",),
    "create": ("409", "422"),
    "patch": ("404", "409", "412", "422"),
    "delete": ("404",),
    "custom": ("404", "422"),
}
_ERROR_TEXT = {
    "400": "Bad request",
    "401": "Unauthorized",
    "403": "Forbidden",
    "404": "Not found",
    "409": "Conflict",
    "412": "Precondition failed",
    "422": "Validation failed",
}


def _rewrite_refs(node: t.Any, prefix: str) -> t.Any:
    """Point ``#/$defs/X`` references at ``#/components/schemas/<prefix>X``."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                out[key] = f"#/components/schemas/{prefix}{value.rsplit('/', 1)[1]}"
            else:
                out[key] = _rewrite_refs(value, prefix)
        return out
    if isinstance(node, list):
        return [_rewrite_refs(v, prefix) for v in node]
    return node


def component_schemas(resource: Resource[t.Any]) -> dict[str, t.Any]:
    """Read/Create/Patch schemas of a resource, plus the nested definitions they refer to."""
    schemas: dict[str, t.Any] = {}
    prefix = resource.model.__name__ + "_"
    kinds: tuple[t.Literal["read", "create", "patch"], ...] = ("read", "create", "patch")
    for kind in kinds:
        schema = copy.deepcopy(resource.view.schema(kind))
        for name, definition in schema.pop("$defs", {}).items():
            schemas[prefix + name] = _rewrite_refs(definition, prefix)
        schemas[resource.schema_name(kind)] = _rewrite_refs(schema, prefix)
    return schemas


def operation_object(resource: Resource[t.Any], op: Operation, *, secured: bool) -> dict[str, t.Any]:
    """The OpenAPI Operation Object for one operation."""
    out: dict[str, t.Any] = {
        "operationId": f"{resource.name}_{op.name}",
        "summary": op.summary or op.name,
        "tags": list(resource.tags),
    }
    parameters = [
        {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
        for name in op.path_parameters
    ]
    parameters.extend(op.query_parameters)
    if op.kind == "patch" and resource.view.revision:
        parameters.append(
            {
                "name": "If-Match",
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": "ETag of the version being changed; the update fails with 412 if it is stale.",
            }
        )
    if parameters:
        out["parameters"] = parameters
    if op.request_schema is not None:
        out["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": op.request_schema}},
        }
    responses: dict[str, t.Any] = {}
    if op.status == 204 or (op.response_schema is None and op.kind == "delete"):
        responses["204"] = {"description": "No content"}
    else:
        content = {"application/json": {"schema": op.response_schema or {}}}
        responses[str(op.status)] = {"description": "Success", "content": content}
    codes = [*_ERRORS.get(op.kind, ()), *(("401", "403") if secured else ())]
    for code in sorted(set(codes)):
        responses[code] = {"$ref": f"#/components/responses/Problem{code}"}
    out["responses"] = responses
    return out


def build_openapi(api: Api) -> dict[str, t.Any]:
    schemas: dict[str, t.Any] = {"Problem": PROBLEM}
    paths: dict[str, dict[str, t.Any]] = {}
    for resource in api.resources:
        schemas.update(component_schemas(resource))
    for route in api.routes:
        resource, op = route.resource, route.operation
        secured = api.authenticate is not None
        paths.setdefault(route.path, {})[route.method.lower()] = operation_object(
            resource, op, secured=secured
        )
    return {
        "openapi": "3.1.0",
        "info": {"title": api.title, "version": api.version},
        "paths": paths,
        "components": {
            "schemas": schemas,
            "responses": {
                f"Problem{code}": {
                    "description": text,
                    "content": {
                        "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
                    },
                }
                for code, text in _ERROR_TEXT.items()
            },
        },
    }
