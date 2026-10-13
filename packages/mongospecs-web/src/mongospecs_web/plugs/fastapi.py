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
from mongospecs_web.errors import HTTPError
from mongospecs_web.openapi import build_openapi, operation_object
from mongospecs_web.plugs.starlette import lifespan, to_request, to_response
from mongospecs_web.resource import Response

__all__ = ["lifespan", "mount", "router"]


def _endpoint(api: Api, route: Route) -> t.Callable[..., t.Awaitable[StarletteResponse]]:
    async def endpoint(request: StarletteRequest) -> StarletteResponse:
        try:
            neutral = await to_request(api, request)
        except HTTPError as exc:
            return to_response(Response.error(exc))
        return to_response(await api.run(route, neutral))

    return endpoint


def router(api: Api, *, openapi: bool | None = None) -> APIRouter:
    """An ``APIRouter`` with one route per operation.

    ``openapi`` says whether the operations appear in the application's OpenAPI document;
    by default that follows ``Api(openapi=...)``.
    """
    out = APIRouter()
    documented = api.expose_openapi if openapi is None else openapi
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
            include_in_schema=documented,
        )
    return out


def mount(app: FastAPI, api: Api, *, openapi: bool | None = None) -> None:
    """Add the API to a FastAPI application and merge its schemas into ``app.openapi()``.

    With ``Api(openapi=False)`` (or ``openapi=False`` here) the routes are served but left out
    of the application's ``/openapi.json`` and ``/docs``.
    """
    documented = api.expose_openapi if openapi is None else openapi
    app.include_router(router(api, openapi=documented))
    if not documented:
        return
    original = app.openapi

    def merged() -> dict[str, t.Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original()
        ours = build_openapi(api)["components"]
        components = schema.setdefault("components", {})
        components.setdefault("schemas", {}).update(ours["schemas"])
        components.setdefault("responses", {}).update(ours["responses"])
        for route in api._routes():
            operation = schema["paths"][route.path][route.method.lower()]
            described = operation_object(
                route.resource, route.operation, secured=api.authenticate is not None
            )
            # FastAPI adds its own default responses; ours are the truth.
            operation["responses"] = described["responses"]
        app.openapi_schema = schema
        return schema

    app.openapi = merged  # type: ignore[method-assign]
