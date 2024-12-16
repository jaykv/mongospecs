import typing as t

from pymongo import ASCENDING

from mongospecs.mixins.base import MongoBaseMixin
from mongospecs.types import SpecDocumentType


class IndexManagementMixin(MongoBaseMixin):
    @classmethod
    def create_index(cls, keys: t.Union[str, list[tuple[str, int]]], **kwargs: t.Any) -> str:
        """
        Create an index on the specified keys (a single key or a list of keys).
        """
        index_keys = [(keys, ASCENDING)] if isinstance(keys, str) else keys
        return cls.get_collection().create_index(index_keys, **kwargs)

    @classmethod
    def drop_index(cls, index_name: str) -> None:
        """
        Drop an index by its name.
        """
        cls.get_collection().drop_index(index_name)

    @classmethod
    def list_indexes(cls) -> list[SpecDocumentType]:
        """
        List all indexes on the collection.
        """
        return list(cls.get_collection().list_indexes())
