import typing as t
import time
from functools import wraps

from pymongo.errors import ConnectionFailure, OperationFailure

from mongospecs.types import SpecBaseType, SubSpecBaseType

__all__ = ["deep_merge", "retry", "handle_database_errors"]


def retry(
    exceptions: t.Type[Exception] = (ConnectionFailure, OperationFailure),
    retries: int = 3,
    delay: float = 0.1,
    exponential_backoff: bool = True,
) -> t.Callable:
    """
    Retry decorator for database operations.

    Args:
        exceptions: Tuple of exceptions to catch and retry on
        retries: Number of retries before giving up
        delay: Initial delay between retries in seconds
        exponential_backoff: Whether to use exponential backoff

    Returns:
        Decorated function that retries on failure

    Example:
        @retry(retries=3, delay=0.5)
        def risky_operation():
            return database.collection.find_one()
    """
    def decorator(func: t.Callable) -> t.Callable:
        @wraps(func)
        def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
            current_delay = delay
            last_exception: t.Optional[Exception] = None

            for attempt in range(retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    if attempt == retries:
                        # Last attempt failed, re-raise
                        raise

                    # Wait before retrying
                    time.sleep(current_delay)

                    # Apply exponential backoff if enabled
                    if exponential_backoff:
                        current_delay *= 2

            # This should never be reached, but just in case
            if last_exception:
                raise last_exception

        return wrapper
    return decorator


def handle_database_errors(func: t.Callable) -> t.Callable:
    """
    Decorator to handle common database errors with better error messages.

    Args:
        func: Function to wrap

    Returns:
        Wrapped function that provides better error handling

    Example:
        @handle_database_errors
        def find_document():
            return collection.find_one()
    """
    @wraps(func)
    def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
        try:
            return func(*args, **kwargs)
        except ConnectionFailure as e:
            raise ConnectionError(
                "Unable to connect to MongoDB. Please check your connection settings."
            ) from e
        except OperationFailure as e:
            raise OperationError(
                f"Database operation failed: {str(e)}",
                error_code=getattr(e, 'code', None),
                details=getattr(e, 'details', None)
            ) from e
        except Exception as e:
            # Re-raise if it's already a custom exception
            if isinstance(e, (ConnectionError, OperationError)):
                raise
            # Otherwise wrap it
            raise DatabaseError(
                f"An unexpected error occurred: {str(e)}"
            ) from e

    return wrapper


class ConnectionError(Exception):
    """Raised when unable to connect to MongoDB."""
    pass


class OperationError(Exception):
    """Raised when a database operation fails."""
    def __init__(self, message: str, error_code: t.Optional[int] = None, details: t.Optional[t.Any] = None):
        super().__init__(message)
        self.error_code = error_code
        self.details = details


class DatabaseError(Exception):
    """Raised for general database errors."""
    pass


def deep_merge(source: dict[str, t.Any], dest: dict[str, t.Any]) -> None:
    """
    Deep merges source dict into dest dict.

    This code was taken directly from the mongothon project:
    https://github.com/gamechanger/mongothon/tree/master/mongothon
    """
    for key, value in source.items():
        if key in dest:
            if isinstance(value, dict) and isinstance(dest[key], dict):
                deep_merge(value, dest[key])
                continue
            elif isinstance(value, list) and isinstance(dest[key], list):
                for item in value:
                    if item not in dest[key]:
                        dest[key].append(item)
                continue
        dest[key] = value


def to_refs(value: t.Any) -> t.Any:
    """Convert all Spec instances within the given value to Ids"""
    # Spec
    if isinstance(value, SpecBaseType):
        return getattr(value, "_id")

    # SubSpec
    elif isinstance(value, SubSpecBaseType):
        return to_refs(value.to_dict())

    # Lists
    elif isinstance(value, (list, tuple)):
        return [to_refs(v) for v in value]

    # Dictionaries
    elif isinstance(value, dict):
        return {k: to_refs(v) for k, v in value.items()}

    return value
