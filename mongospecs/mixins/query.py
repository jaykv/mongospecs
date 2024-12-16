import typing as t

from bson import ObjectId
from typing_extensions import Self

from mongospecs.helpers.query import Condition, Group
from mongospecs.mixins.base import MongoBaseMixin
from mongospecs.types import FilterType
from mongospecs.utils import to_refs


class QueryMixin(MongoBaseMixin):
    @classmethod
    def by_id(cls, id: ObjectId, **kwargs: t.Any) -> t.Optional[Self]:
        """Get a document by ID"""
        return cls.one({"_id": id}, **kwargs)

    @classmethod
    def count(cls, filter: FilterType = None, **kwargs: t.Any) -> int:
        """Return a count of documents matching the filter"""
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()

        filter = to_refs(filter)

        if filter:
            return cls.get_collection().count_documents(to_refs(filter), **kwargs)
        else:
            return cls.get_collection().estimated_document_count(**kwargs)

    @classmethod
    def ids(cls, filter: FilterType = None, **kwargs: t.Any) -> list[ObjectId]:
        """Return a list of Ids for documents matching the filter"""
        # Find the documents
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()

        documents = cls.get_collection().find(to_refs(filter), projection={"_id": True}, **kwargs)

        return [d["_id"] for d in list(documents)]

    @classmethod
    def one(cls, filter: FilterType = None, **kwargs: t.Any) -> t.Optional[Self]:
        """Return the first spec object matching the filter"""
        # Flatten the projection
        kwargs["projection"], references, subs = cls._flatten_projection(
            kwargs.get("projection", cls._default_projection)
        )

        # Find the document
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()

        document = cls.get_collection().find_one(to_refs(filter), **kwargs)

        # Make sure we found a document
        if not document:
            return None

        # Dereference the document (if required)
        if references:
            cls._dereference([document], references)

        # Add sub-specs to the document (if required)
        if subs:
            cls._apply_sub_specs([document], subs)

        return cls.from_document(document)

    @classmethod
    def many(cls, filter: FilterType = None, **kwargs: t.Any) -> list[Self]:
        """Return a list of spec objects matching the filter"""
        # Flatten the projection
        kwargs["projection"], references, subs = cls._flatten_projection(
            kwargs.get("projection", cls._default_projection)
        )

        # Find the documents
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()

        documents = list(cls.get_collection().find(to_refs(filter), **kwargs))

        # Dereference the documents (if required)
        if references:
            cls._dereference(documents, references)

        # Add sub-specs to the documents (if required)
        if subs:
            cls._apply_sub_specs(documents, subs)

        return [cls(**d) for d in documents]
