"""Quart plug. Quart validates nothing itself, so everything rests on the neutral core."""

from __future__ import annotations

import typing as t

from quart import Blueprint, Quart
from quart import Response as QuartResponse
from quart import request as quart_request

from mongospecs_web.api import Api, Route
from mongospecs_web.errors import HTTPError
from mongospecs_web.plugs.asgi import _send
from mongospecs_web.resource import Request, Response

__all__ = ["BodyLimit", "blueprint", "init_app"]


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


class BodyLimit:
    """ASGI middleware: read request bodies for the API under its size limit, before Quart does.

    Quart moves every chunk a client sends into an unbounded queue as it arrives, whether or not
    the view has asked for it, so a limit applied inside a view comes too late. This reads the
    body first (never more than ``api.max_body`` bytes), answers 413 itself when it is too
    large, and otherwise hands Quart the body it read.
    """

    def __init__(self, app: t.Any, api: Api) -> None:
        self.app = app
        self.api = api

    async def __call__(self, scope: t.Any, receive: t.Any, send: t.Any) -> None:
        if scope["type"] != "http" or not self._ours(scope):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        gone = False

        async def chunks() -> t.AsyncIterator[bytes]:
            nonlocal gone
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    gone = True
                    return
                yield message.get("body", b"")
                if not message.get("more_body"):
                    return

        try:
            body = await self.api.read_body(headers, chunks())
        except HTTPError as exc:
            await _send(send, Response.error(exc))
            return
        if gone:
            return
        replayed = False

        async def replay() -> t.Any:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    def _ours(self, scope: t.Any) -> bool:
        path: str = scope.get("path", "")
        root: str = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :] or "/"
        return path == self.api.prefix or path.startswith(self.api.prefix + "/")


def init_app(app: Quart, api: Api, *, registry: t.Any = None, name: str = "mongospecs") -> None:
    """Register the API on a Quart app; with ``registry=`` also open and close it with the server.

    Also installs :class:`BodyLimit`, so request bodies for the API are never read past
    ``api.max_body``. If you register :func:`blueprint` yourself, wrap the app the same way:
    ``app.asgi_app = BodyLimit(app.asgi_app, api)``.
    """
    app.register_blueprint(blueprint(api, name=name))
    app.asgi_app = BodyLimit(app.asgi_app, api)  # type: ignore[method-assign]
    if registry is not None:
        app.before_serving(registry.open)
        app.after_serving(registry.close)
