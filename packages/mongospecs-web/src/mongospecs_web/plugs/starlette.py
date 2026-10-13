"""Starlette plug."""

from __future__ import annotations

import contextlib
import typing as t

from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.responses import Response as StarletteResponse
from starlette.routing import BaseRoute
from starlette.routing import Route as StarletteRoute

from mongospecs_web.api import Api, Route
from mongospecs_web.resource import Request, Response

__all__ = ["lifespan", "mount", "routes", "to_request", "to_response"]


async def to_request(request: StarletteRequest) -> Request:
    return Request(
        request.method,
        {k: str(v) for k, v in request.path_params.items()},
        list(request.query_params.multi_items()),
        {k.lower(): v for k, v in request.headers.items()},
        await request.body(),
        None,
        request,
    )


def to_response(response: Response) -> StarletteResponse:
    has_body = bool(response.body) or response.status not in (204, 304)
    return StarletteResponse(
        response.body, response.status, dict(response.headers), response.media_type if has_body else None
    )


def _endpoint(api: Api, route: Route) -> t.Callable[[StarletteRequest], t.Awaitable[StarletteResponse]]:
    async def endpoint(request: StarletteRequest) -> StarletteResponse:
        return to_response(await api.run(route, await to_request(request)))

    return endpoint


def routes(api: Api, *, openapi: bool = True) -> list[BaseRoute]:
    """Starlette routes for every operation of the API (plus ``openapi.json``)."""
    out: list[BaseRoute] = [
        StarletteRoute(route.path, _endpoint(api, route), methods=[route.method], name=route.name)
        for route in api._routes()
    ]
    if openapi:

        async def spec(request: StarletteRequest) -> StarletteResponse:
            return JSONResponse(api.openapi())

        out.append(StarletteRoute(api.openapi_path, spec, methods=["GET"], name="openapi"))
    return out


def mount(app: t.Any, api: Api, *, openapi: bool = True) -> None:
    """Add the API's routes to an existing Starlette application."""
    app.router.routes.extend(routes(api, openapi=openapi))


def lifespan(registry: t.Any) -> t.Callable[[t.Any], t.AsyncContextManager[None]]:
    """A Starlette ``lifespan`` that opens and closes a registry: ``Starlette(lifespan=lifespan(registry))``."""

    @contextlib.asynccontextmanager
    async def manage(app: t.Any) -> t.AsyncIterator[None]:
        async with registry:
            yield

    return manage
