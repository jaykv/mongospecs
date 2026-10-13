"""An ``Api`` groups resources under a prefix and is what every plug mounts."""

from __future__ import annotations

import inspect
import logging
import re
import typing as t
from dataclasses import dataclass

from mongospecs_web.errors import HTTPError, MethodNotAllowed, NotFound, PayloadTooLarge
from mongospecs_web.resource import Operation, Request, Resource, Response

__all__ = ["Api", "Authenticate", "Route"]

Authenticate = t.Callable[[Request], t.Any]
"""``authenticate(request) -> principal``; may be async. Return ``None`` for anonymous,
or raise :class:`~mongospecs_web.errors.Unauthorized`."""

_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    path: str
    """Full path template with ``{name}`` parameters, including the prefix."""
    resource: Resource[t.Any]
    operation: Operation
    pattern: re.Pattern[str]

    @property
    def name(self) -> str:
        return f"{self.resource.name}.{self.operation.name}"


class Api:
    """A set of resources mounted under one prefix.

    ::

        api = Api(articles, users, prefix="/api", title="Blog", authenticate=current_user)
        app = api.asgi()                       # pure ASGI
        # or: mongospecs_web.plugs.fastapi.mount(fastapi_app, api)
    """

    def __init__(
        self,
        *resources: Resource[t.Any],
        prefix: str = "",
        title: str = "API",
        version: str = "1.0.0",
        authenticate: Authenticate | None = None,
        max_body: int = 1_048_576,
    ) -> None:
        self.resources: tuple[Resource[t.Any], ...] = resources
        self.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""
        self.title = title
        self.version = version
        self.authenticate = authenticate
        self.max_body = max_body
        names = [r.name for r in resources]
        if len(set(names)) != len(names):
            raise ValueError(f"resource names must be unique, got {names}")

    @property
    def routes(self) -> list[Route]:
        out: list[Route] = []
        for resource in self.resources:
            for op in resource.operations:
                path = f"{self.prefix}/{resource.name}{op.path}"
                pattern = re.compile(
                    "^"
                    + _PARAM.sub(r"(?P<\1>[^/]+)", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
                    + "/?$"
                )
                out.append(Route(op.method, path, resource, op, pattern))
        return out

    @property
    def openapi_path(self) -> str:
        return f"{self.prefix}/openapi.json"

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        """Find the route for a request, or raise 404 / 405."""
        allowed: list[str] = []
        for route in self._routes():
            found = route.pattern.match(path)
            if found is None:
                continue
            if route.method == method.upper():
                return route, found.groupdict()
            allowed.append(route.method)
        if allowed:
            raise MethodNotAllowed(headers={"allow": ", ".join(sorted(set(allowed)))})
        raise NotFound("No such route.")

    def _routes(self) -> list[Route]:
        cached: list[Route] | None = self.__dict__.get("_route_cache")
        if cached is None or self.__dict__.get("_route_count") != sum(
            len(r.operations) for r in self.resources
        ):
            cached = self.routes
            # Literal segments win over parameters: /{id}/publish before /{id}, /search before /{id}.
            cached.sort(key=lambda r: (-r.path.count("/"), r.path.count("{")))
            self.__dict__["_route_cache"] = cached
            self.__dict__["_route_count"] = sum(len(r.operations) for r in self.resources)
        return cached

    async def run(self, route: Route, request: Request) -> Response:
        """Authenticate and dispatch. Never raises."""
        try:
            if len(request.body) > self.max_body:
                raise PayloadTooLarge(f"The request body may be at most {self.max_body} bytes.")
            if self.authenticate is not None:
                principal = self.authenticate(request)
                request.principal = await principal if inspect.isawaitable(principal) else principal
        except Exception as exc:
            response = Response.error(exc)
            if response.status >= 500:
                logging.getLogger("mongospecs_web").error("authentication failed unexpectedly", exc_info=exc)
            return response
        response = await route.resource.dispatch(route.operation, request)
        location = response.headers.get("location")
        if location and not location.startswith(("/", "http://", "https://")):
            # A resource names what it created by id; the URL of that thing depends on where
            # the API is mounted, which only the Api knows.
            response.headers["location"] = f"{self.prefix}/{route.resource.name}/{location}"
        return response

    async def handle(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        raw: t.Any = None,
    ) -> Response:
        """Route and run a request given as plain values. Used by the ASGI plug and handy in tests."""
        try:
            route, params = self.match(method, path)
        except HTTPError as exc:
            return Response.error(exc)
        return await self.run(
            route, Request(method.upper(), params, query or [], headers or {}, body, None, raw)
        )

    def openapi(self) -> dict[str, t.Any]:
        from mongospecs_web.openapi import build_openapi

        return build_openapi(self)

    def asgi(self, *, registry: t.Any = None) -> t.Any:
        """A pure ASGI application serving this API (and ``openapi.json``)."""
        from mongospecs_web.plugs.asgi import AsgiApp

        return AsgiApp(self, registry=registry)
