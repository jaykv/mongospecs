"""Quart plug. Quart validates nothing itself, so everything rests on the neutral core."""

from __future__ import annotations

import typing as t

from quart import Blueprint, Quart
from quart import Response as QuartResponse
from quart import request as quart_request

from mongospecs_web.api import Api, Route
from mongospecs_web.errors import HTTPError
from mongospecs_web.resource import Request, Response

__all__ = ["blueprint", "init_app"]


def _to_quart(response: Response) -> QuartResponse:
    out = QuartResponse(response.body, response.status, dict(response.headers))
    if response.body or response.status not in (204, 304):
        out.headers["content-type"] = response.media_type
    else:
        out.headers.pop("content-type", None)
    return out


def _view(api: Api, route: Route) -> t.Callable[..., t.Awaitable[QuartResponse]]:
    async def view(**path_params: t.Any) -> QuartResponse:
        headers = {k.lower(): v for k, v in quart_request.headers.items()}
        try:
            body = await api.read_body(headers, quart_request.body)
        except HTTPError as exc:
            return _to_quart(Response.error(exc))
        neutral = Request(
            quart_request.method,
            {k: str(v) for k, v in path_params.items()},
            list(quart_request.args.items(multi=True)),
            headers,
            body,
            None,
            quart_request,
        )
        return _to_quart(await api.run(route, neutral))

    return view


def blueprint(api: Api, *, name: str = "mongospecs", openapi: bool | None = None) -> Blueprint:
    """A Blueprint with one rule per operation (plus ``openapi.json``)."""
    bp = Blueprint(name, __name__)
    for route in api._routes():
        rule = route.path
        for param in route.operation.path_parameters:
            rule = rule.replace("{" + param + "}", f"<{param}>")
        bp.add_url_rule(
            rule, endpoint=route.name.replace(".", "_"), view_func=_view(api, route), methods=[route.method]
        )
    if api.expose_openapi if openapi is None else openapi:

        async def spec() -> dict[str, t.Any]:
            return api.openapi()

        bp.add_url_rule(api.openapi_path, endpoint="openapi", view_func=spec, methods=["GET"])
    return bp


def init_app(app: Quart, api: Api, *, registry: t.Any = None, name: str = "mongospecs") -> None:
    """Register the API on a Quart app; with ``registry=`` also open and close it with the server."""
    app.register_blueprint(blueprint(api, name=name))
    if registry is not None:
        app.before_serving(registry.open)
        app.after_serving(registry.close)
