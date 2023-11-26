from typing import ClassVar, Optional, Any
from pymongo.database import Database
from pymongo.collection import Collection
from pymongo import MongoClient


class SpecBase:
    _client: ClassVar[Optional[MongoClient]] = None
    _db: ClassVar[Optional[Database]] = None
    _collection: ClassVar[Optional[str]] = None
    _collection_context: ClassVar[Optional[Collection]] = None
    _default_projection: ClassVar[dict[str, Any]] = {}


class SubSpecBase:
    def to_dict(self) -> Any:
        raise NotImplementedError()


def to_refs(value: Any) -> Any:
    """Convert all Frame instances within the given value to Ids"""
    # Frame
    if isinstance(value, SpecBase):
        return getattr(value, "_id")

    # SubFrame
    elif isinstance(value, SubSpecBase):
        return to_refs(value.to_dict())

    # Lists
    elif isinstance(value, (list, tuple)):
        return [to_refs(v) for v in value]

    # Dictionaries
    elif isinstance(value, dict):
        return {k: to_refs(v) for k, v in value.items()}

    return value
