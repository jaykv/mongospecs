

import typing as t
from pymongo import ASCENDING

from .base import MongoBaseMixin

class IndexManagementMixin(MongoBaseMixin):

    @classmethod
    def create_index(cls, keys: t.Union[str, list[str]], direction: t.Union[int, str] = ASCENDING, **kwargs: t.Any) -> str:
        """
        Create an index on the specified keys (a single key or a list of keys).
        """
        if isinstance(keys, str):
            index_keys = [(keys, direction)]
        else:
            index_keys = [(key, direction) for key in keys]

        return cls.get_collection().create_index(index_keys, **kwargs)

    @classmethod
    def drop_index(cls, index_name: str) -> None:
        """
        Drop an index by its name.
        """
        cls.get_collection().drop_index(index_name)

    @classmethod
    def list_indexes(cls) -> list[t.MutableMapping[str, t.Any]]:
        """
        List all indexes on the collection.
        """
        return list(cls.get_collection().list_indexes())
