"""FastAPI plug.

FastAPI derives request and response schemas from pydantic models only. Here the
operations are documented through ``openapi_extra`` and the component schemas
are merged into the application's OpenAPI document, so models on any backend
(msgspec, attrs, pydantic) appear in ``/docs`` the same way. Responses are sent
as already-serialised bytes.
"""

from __future__ import annotations

import typing as t

from fastapi import APIRouter, FastAPI
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse

from mongospecs_web.api import Api, Route
from mongospecs_web.openapi import build_openapi, operation_object
from mongospecs_web.plugs.starlette import lifespan, to_request, to_response

__all__ = ["lifespan", "mount", "router"]


def _endpoint(api: Api, route: Route) -> t.Callable[..., t.Awaitable[StarletteResponse]]:
    async def endpoint(request: StarletteRequest) -> StarletteResponse:
        return to_response(await api.run(route, await to_request(request)))

    return endpoint


def router(api: Api) -> APIRouter:
    """An ``APIRouter`` with one route per operation."""
    out = APIRouter()
    secured = api.authenticate is not None
    for route in api._routes():
        extra = operation_object(route.resource, route.operation, secured=secured)
        out.add_api_route(
            route.path,
            _endpoint(api, route),
            methods=[route.method],
            name=route.name,
            operation_id=extra.pop("operationId"),
            summary=extra.pop("summary"),
            tags=list(extra.pop("tags")),
            status_code=route.operation.status,
            response_class=StarletteResponse,
            openapi_extra=extra,
        )
    return out


def mount(app: FastAPI, api: Api) -> None:
    """Add the API to a FastAPI application and merge its schemas into ``app.openapi()``."""
    app.include_router(router(api))
    original = app.openapi

    def openapi() -> dict[str, t.Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original()
        ours = build_openapi(api)["components"]
        components = schema.setdefault("components", {})
        components.setdefault("schemas", {}).update(ours["schemas"])
        components.setdefault("responses", {}).update(ours["responses"])
        for route in api._routes():
            operation = schema["paths"][route.path][route.method.lower()]
            documented = operation_object(
                route.resource, route.operation, secured=api.authenticate is not None
            )
            # FastAPI adds its own default responses; ours are the truth.
            operation["responses"] = documented["responses"]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
