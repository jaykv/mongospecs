"""Pure ASGI plug: no framework required. The reference the other plugs are checked against."""

from __future__ import annotations

import json
import typing as t
from urllib.parse import parse_qsl

from mongospecs_web.api import Api
from mongospecs_web.errors import HTTPError
from mongospecs_web.resource import Response

__all__ = ["AsgiApp"]

Scope = dict[str, t.Any]
Receive = t.Callable[[], t.Awaitable[dict[str, t.Any]]]
Send = t.Callable[[dict[str, t.Any]], t.Awaitable[None]]


class AsgiApp:
    """An ASGI application for one :class:`Api`.

    With ``registry=`` the application opens the registry on ASGI lifespan
    startup and closes it on shutdown.
    """

    def __init__(self, api: Api, *, registry: t.Any = None) -> None:
        self.api = api
        self.registry = registry
        self._openapi: bytes | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "http":
            await self._http(scope, receive, send)
        else:
            raise RuntimeError(f"unsupported ASGI scope type {scope['type']!r}")

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    if self.registry is not None:
                        await self.registry.open()
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self.registry is not None:
                    await self.registry.close()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        method: str = scope["method"]
        root = scope.get("root_path", "")
        path: str = scope["path"]
        if root and path.startswith(root):
            path = path[len(root) :] or "/"
        if method == "GET" and path == self.api.openapi_path and self.api.expose_openapi:
            if self._openapi is None:
                self._openapi = json.dumps(self.api.openapi(), separators=(",", ":")).encode()
            await _send(send, Response(200, self._openapi, {}))
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
            return  # the client left before the request was complete: there is nothing to run
        query = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        response = await self.api.handle(method, path, query=query, headers=headers, body=body, raw=scope)
        await _send(send, response)


async def _send(send: Send, response: Response) -> None:
    headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in response.headers.items()]
    if response.body or response.status not in (204, 304):  # includes the answer to a HEAD
        headers.append((b"content-type", response.media_type.encode("latin-1")))
    if "content-length" not in response.headers:  # HEAD carries the length of the GET body
        headers.append((b"content-length", str(len(response.body)).encode("latin-1")))
    await send({"type": "http.response.start", "status": response.status, "headers": headers})
    await send({"type": "http.response.body", "body": response.body})
