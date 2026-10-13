"""Litestar plug.

Handlers are excluded from Litestar's own schema generation (it derives schemas
from handler signatures, which here are generic); the API's OpenAPI document is
served at ``<prefix>/openapi.json`` instead.
"""

from __future__ import annotations

import contextlib
import typing as t

from litestar import Request as LitestarRequest
from litestar import Response as LitestarResponse
from litestar import get, route
from litestar.config.app import AppConfig
from litestar.plugins import InitPluginProtocol

from mongospecs_web.api import Api, Route
from mongospecs_web.resource import Request, Response

__all__ = ["MongospecsPlugin", "handlers"]


def _to_response(response: Response) -> LitestarResponse[bytes]:
    return LitestarResponse(
        content=response.body,
        status_code=response.status,
        headers=dict(response.headers),
        media_type=response.media_type,
    )


def _handler(api: Api, target: Route) -> t.Any:
    path = target.path
    for name in target.operation.path_parameters:
        path = path.replace("{" + name + "}", "{" + name + ":str}")

    method = t.cast("t.Any", target.method)

    @route(path, http_method=[method], name=target.name, include_in_schema=False, status_code=200)
    async def handler(request: LitestarRequest[t.Any, t.Any, t.Any]) -> LitestarResponse[bytes]:
        neutral = Request(
            request.method,
            {k: str(v) for k, v in request.path_params.items()},
            list(request.query_params.multi_items()),
            {k.lower(): v for k, v in request.headers.items()},
            await request.body(),
            None,
            request,
        )
        return _to_response(await api.run(target, neutral))

    return handler


def handlers(api: Api, *, openapi: bool = True) -> list[t.Any]:
    """Litestar route handlers for every operation of the API."""
    out = [_handler(api, target) for target in api._routes()]
    if openapi:

        @get(api.openapi_path, name="mongospecs_openapi", include_in_schema=False, sync_to_thread=False)
        def spec() -> dict[str, t.Any]:
            return api.openapi()

        out.append(spec)
    return out


class MongospecsPlugin(InitPluginProtocol):
    """``Litestar(plugins=[MongospecsPlugin(api, registry=registry)])``"""

    def __init__(self, api: Api, *, registry: t.Any = None, openapi: bool = True) -> None:
        self.api = api
        self.registry = registry
        self.openapi = openapi

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        app_config.route_handlers.extend(handlers(self.api, openapi=self.openapi))
        if self.registry is not None:
            registry = self.registry

            @contextlib.asynccontextmanager
            async def lifespan(app: t.Any) -> t.AsyncIterator[None]:
                async with registry:
                    yield

            app_config.lifespan.append(lifespan)
        return app_config
