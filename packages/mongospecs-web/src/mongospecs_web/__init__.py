"""Framework-neutral REST resources for mongospecs models.

::

    from mongospecs_web import Api, Resource, List, Get, Create, Patch, Delete

    articles = Resource(Article, ops=[List, Get, Create, Patch, Delete])
    app = Api(articles, prefix="/api").asgi(registry=registry)

Plugs for Starlette, FastAPI, Litestar and Quart live in ``mongospecs_web.plugs``.
"""

from mongospecs_web.api import Api, Route
from mongospecs_web.backend import Filter, ListQuery, MongoBackend, ResourceBackend
from mongospecs_web.errors import (
    BadRequest,
    Conflict,
    Forbidden,
    HTTPError,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    PreconditionFailed,
    Unauthorized,
    Unprocessable,
    UnsupportedMediaType,
)
from mongospecs_web.filters import Cursor, Offset
from mongospecs_web.policy import View, view_of
from mongospecs_web.resource import (
    Create,
    Ctx,
    Delete,
    Get,
    List,
    Operation,
    Patch,
    Put,
    Request,
    Resource,
    Response,
)

__version__ = "0.1.0"

__all__ = [
    "Api",
    "BadRequest",
    "Conflict",
    "Create",
    "Ctx",
    "Cursor",
    "Delete",
    "Filter",
    "Forbidden",
    "Get",
    "HTTPError",
    "List",
    "ListQuery",
    "MethodNotAllowed",
    "MongoBackend",
    "NotFound",
    "Offset",
    "Operation",
    "Patch",
    "PayloadTooLarge",
    "PreconditionFailed",
    "Put",
    "Request",
    "Resource",
    "ResourceBackend",
    "Response",
    "Route",
    "Unauthorized",
    "Unprocessable",
    "UnsupportedMediaType",
    "View",
    "view_of",
]
