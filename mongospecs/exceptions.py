"""
Custom exceptions for mongospecs.

This module defines custom exception classes to provide more specific
error handling and better error messages for common failure scenarios.
"""

from __future__ import annotations

import typing as t
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    OperationFailure,
    PyMongoError,
)


class MongoSpecError(Exception):
    """Base exception for all mongospecs errors."""
    pass


class ConnectionError(MongoSpecError, ConnectionFailure):
    """Raised when unable to connect to MongoDB."""
    pass


class OperationError(MongoSpecError, OperationFailure):
    """Raised when a database operation fails."""
    pass


class DuplicateKeyError(MongoSpecError, DuplicateKeyError):
    """Raised when attempting to insert a document with a duplicate key."""
    pass


class ValidationError(MongoSpecError, ValueError):
    """Raised when document validation fails."""
    pass


class NotFoundError(MongoSpecError):
    """Raised when a document is not found."""
    pass


class TransactionError(MongoSpecError):
    """Raised when a transaction operation fails."""
    pass


def handle_pymongo_errors(func: t.Callable) -> t.Callable:
    """
    Decorator to handle common PyMongo errors and convert them to custom exceptions.

    This decorator catches common PyMongo errors and re-raises them as more
    specific mongospecs exceptions with better error messages.
    """
    def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
        try:
            return func(*args, **kwargs)
        except DuplicateKeyError as e:
            raise DuplicateKeyError(
                f"Duplicate key error: {str(e)}",
                error_code=e.code,
                details=e.details,
            ) from e
        except ConnectionFailure as e:
            raise ConnectionError(
                f"Failed to connect to MongoDB: {str(e)}",
            ) from e
        except OperationFailure as e:
            raise OperationError(
                f"Operation failed: {str(e)}",
                error_code=e.code,
                details=e.details,
            ) from e
        except PyMongoError as e:
            raise MongoSpecError(f"Unexpected database error: {str(e)}") from e

    return wrapper
