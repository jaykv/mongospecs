"""HTTP errors and their RFC 9457 (``application/problem+json``) representation."""

from __future__ import annotations

import typing as t

from mongospecs import errors as me

__all__ = [
    "BadRequest",
    "Conflict",
    "Forbidden",
    "HTTPError",
    "MethodNotAllowed",
    "NotFound",
    "PayloadTooLarge",
    "PreconditionFailed",
    "Unauthorized",
    "Unprocessable",
    "UnsupportedMediaType",
    "problem",
]

PROBLEM_JSON = "application/problem+json"


class HTTPError(Exception):
    """An error with an HTTP status. Raise it from guards, hooks and custom operations."""

    status: t.ClassVar[int] = 500
    title: t.ClassVar[str] = "Internal Server Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: list[dict[str, t.Any]] | None = None,
        headers: dict[str, str] | None = None,
        status: int | None = None,
        title: str | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.errors = errors
        self.headers = headers or {}
        if status is not None:
            self.status = status  # type: ignore[misc]
        if title is not None:
            self.title = title  # type: ignore[misc]

    def body(self) -> dict[str, t.Any]:
        out: dict[str, t.Any] = {"type": "about:blank", "title": self.title, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if self.errors:
            out["errors"] = self.errors
        return out


class BadRequest(HTTPError):
    status, title = 400, "Bad Request"


class Unauthorized(HTTPError):
    status, title = 401, "Unauthorized"


class Forbidden(HTTPError):
    status, title = 403, "Forbidden"


class NotFound(HTTPError):
    status, title = 404, "Not Found"


class MethodNotAllowed(HTTPError):
    status, title = 405, "Method Not Allowed"


class Conflict(HTTPError):
    status, title = 409, "Conflict"


class PreconditionFailed(HTTPError):
    status, title = 412, "Precondition Failed"


class PayloadTooLarge(HTTPError):
    status, title = 413, "Content Too Large"


class UnsupportedMediaType(HTTPError):
    status, title = 415, "Unsupported Media Type"


class Unprocessable(HTTPError):
    status, title = 422, "Unprocessable Content"


def problem(exc: BaseException) -> HTTPError:
    """Map any exception to an :class:`HTTPError`. Unknown errors become a 500 with no detail."""
    if isinstance(exc, HTTPError):
        return exc
    if isinstance(exc, me.ValidationError):
        return Unprocessable("The request body failed validation.", errors=exc.errors)
    if isinstance(exc, me.StaleDocumentError):
        return PreconditionFailed("The resource was changed since the revision you supplied.")
    if isinstance(exc, me.DocumentNotFound):
        return NotFound("The resource does not exist.")
    if isinstance(exc, me.DuplicateKeyError):
        fields = sorted(exc.fields)
        return Conflict(
            "A resource with the same unique value already exists.",
            errors=[{"field": f, "message": "must be unique"} for f in fields] or None,
        )
    if isinstance(exc, me.EncodingError):
        # A value that passed validation but has no BSON form (an integer beyond 64 bits inside
        # an untyped field, say). The request is at fault, so this is not a 500.
        return Unprocessable("The request contains a value that cannot be stored.")
    if isinstance(exc, me.QueryError):
        return BadRequest(str(exc))
    if isinstance(exc, me.OperationTimeout):
        return HTTPError("The database did not answer in time.", status=504, title="Gateway Timeout")
    if isinstance(exc, me.PoolTimeout):
        return HTTPError(
            "The server is busy: no database connection became free in time.",
            status=503,
            title="Service Unavailable",
            headers={"retry-after": "1"},
        )
    if isinstance(exc, me.ConnectionFailed):
        return HTTPError("The database is unavailable.", status=503, title="Service Unavailable")
    return HTTPError()
