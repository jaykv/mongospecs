"""One error hierarchy for mongospecs.

Driver errors are wrapped, never subclassed or shadowed: the original pymongo
exception is always available as ``__cause__``.
"""

from __future__ import annotations

import typing as t
from contextlib import contextmanager

from bson.errors import BSONError
from pymongo import errors as pe

__all__ = [
    "BulkWriteFailed",
    "ConfigurationError",
    "ConnectionFailed",
    "DocumentNotFound",
    "DocumentTooLarge",
    "DriverError",
    "DuplicateKeyError",
    "EncodingError",
    "MongoSpecsError",
    "NotBoundError",
    "NotLoadedError",
    "OperationFailed",
    "OperationTimeout",
    "PoolTimeout",
    "QueryError",
    "RegistryClosedError",
    "StaleDocumentError",
    "TransactionError",
    "ValidationError",
]


class MongoSpecsError(Exception):
    """Base class for every error raised by mongospecs."""


class ConfigurationError(MongoSpecsError):
    """A model, plugin or registry is declared incorrectly."""


class NotBoundError(MongoSpecsError):
    """A model was used before being bound to a registry."""


class RegistryClosedError(MongoSpecsError):
    """An operation was attempted on a registry that is not open."""


class QueryError(MongoSpecsError, ValueError):
    """A query names an unknown field, an uncastable value or an untrusted operator."""


class ValidationError(MongoSpecsError, ValueError):
    """Input failed model validation.

    ``errors`` is a list of ``{"field": ..., "message": ...}`` items; ``field``
    is ``None`` when the backend reports a document-level problem.
    """

    def __init__(self, message: str, errors: list[dict[str, t.Any]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict[str, t.Any]] = errors or [{"field": None, "message": message}]


class EncodingError(MongoSpecsError, ValueError):
    """A value could not be encoded to BSON."""


class NotLoadedError(MongoSpecsError):
    """A reference was dereferenced before being loaded with ``include()`` or ``fetch()``."""


class DocumentNotFound(MongoSpecsError, LookupError):
    """An acknowledged read or write matched no document."""


class StaleDocumentError(MongoSpecsError):
    """An optimistic-lock check failed: the document changed since it was loaded."""


class DriverError(MongoSpecsError):
    """A driver error with no more specific mapping."""


class ConnectionFailed(DriverError):
    """The server could not be reached."""


class PoolTimeout(ConnectionFailed):
    """No pooled connection became available in time."""


class OperationTimeout(DriverError):
    """The operation exceeded its time limit."""


class OperationFailed(DriverError):
    """The server rejected the operation."""

    def __init__(self, message: str, *, code: int | None = None, details: t.Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class DuplicateKeyError(OperationFailed):
    """A unique index was violated. ``fields`` maps index keys to the offending values."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        details: t.Any = None,
        fields: dict[str, t.Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.fields: dict[str, t.Any] = fields or {}


class DocumentTooLarge(OperationFailed):
    """A document, or an aggregation result document, exceeded the BSON size limit."""


class BulkWriteFailed(OperationFailed):
    """A bulk write failed for some items. ``write_errors`` lists them by input index."""

    def __init__(self, message: str, *, details: t.Any = None) -> None:
        super().__init__(message, code=None, details=details)
        self.write_errors: list[dict[str, t.Any]] = list((details or {}).get("writeErrors", []))


class TransactionError(DriverError):
    """A transaction could not be committed."""


# Server codes for "object too large" style failures.
_TOO_LARGE_CODES = frozenset({10334, 17419, 17420, 16389})


def _duplicate_fields(details: t.Any) -> dict[str, t.Any]:
    if isinstance(details, dict):
        value = details.get("keyValue")
        if isinstance(value, dict):
            return dict(value)
        pattern = details.get("keyPattern")
        if isinstance(pattern, dict):
            return {k: None for k in pattern}
    return {}


def translate(exc: BaseException) -> MongoSpecsError:
    """Map a pymongo/bson exception to exactly one mongospecs error."""
    msg = str(exc)
    if isinstance(exc, MongoSpecsError):
        return exc
    if isinstance(exc, pe.DuplicateKeyError):
        return DuplicateKeyError(
            msg, code=exc.code, details=exc.details, fields=_duplicate_fields(exc.details)
        )
    if isinstance(exc, pe.BulkWriteError):
        details = exc.details or {}
        errors = details.get("writeErrors", [])
        if errors and all(e.get("code") == 11000 for e in errors) and len(errors) == 1:
            first = errors[0]
            return DuplicateKeyError(
                first.get("errmsg", msg), code=11000, details=details, fields=_duplicate_fields(first)
            )
        return BulkWriteFailed(msg, details=details)
    if isinstance(exc, pe.DocumentTooLarge):
        return DocumentTooLarge(msg)
    if isinstance(exc, pe.ExecutionTimeout | pe.NetworkTimeout | pe.WTimeoutError):
        return OperationTimeout(msg)
    if isinstance(exc, pe.WaitQueueTimeoutError):
        return PoolTimeout(msg)
    if isinstance(exc, pe.ConnectionFailure):
        return ConnectionFailed(msg)
    if isinstance(exc, pe.OperationFailure):
        if exc.code in _TOO_LARGE_CODES:
            return DocumentTooLarge(msg, code=exc.code, details=exc.details)
        return OperationFailed(msg, code=exc.code, details=exc.details)
    if isinstance(exc, pe.InvalidOperation):
        return DriverError(msg)
    if isinstance(exc, BSONError):
        return EncodingError(msg)
    return DriverError(msg)


@contextmanager
def wrap_errors() -> t.Iterator[None]:
    """Translate driver errors raised inside the block."""
    try:
        yield
    except MongoSpecsError:
        raise
    except (pe.PyMongoError, BSONError) as exc:
        raise translate(exc) from exc
