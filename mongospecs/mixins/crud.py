import typing as t

from blinker import signal
from pymongo import UpdateOne
from pymongo.collection import Collection
from typing_extensions import Self

from mongospecs.helpers.query import Condition, Group
from mongospecs.mixins.query import QueryMixin
from mongospecs.types import FilterType, SpecDocumentType, SpecsOrRawDocuments
from mongospecs.utils import to_refs


class CrudMixin(QueryMixin):
    # Operations
    def insert(self, **insert_one_kwargs: t.Any) -> None:
        """Insert this document"""
        # Send insert signal
        signal("insert").send(self.__class__, specs=[self])

        document_dict = self.to_dict()
        if not self._id:
            document_dict.pop("_id", None)
        # Prepare the document to be inserted
        document = to_refs(document_dict)

        # Insert the document and update the Id
        self._id = self.get_collection().insert_one(document, **insert_one_kwargs).inserted_id

        # Send inserted signal
        signal("inserted").send(self.__class__, specs=[self])

    def unset(self, *fields: t.Any, **update_one_kwargs: t.Any) -> None:
        """Unset the given list of fields for this document."""

        # Send update signal
        signal("update").send(self.__class__, specs=[self])

        # Clear the fields from the document and build the unset object
        unset = {}
        for field in fields:
            setattr(self, field, self._empty_type)
            unset[field] = True

        # Update the document
        self.get_collection().update_one({"_id": self._id}, {"$unset": unset}, **update_one_kwargs)

        # Send updated signal
        signal("updated").send(self.__class__, specs=[self])

    def update(self, *fields: t.Any, **update_one_kwargs: t.Any) -> None:
        """
        Update this document. Optionally a specific list of fields to update can
        be specified.
        """
        self_document = self.to_dict()
        if "_id" not in self_document:
            raise ValueError("Can't update documents without `_id`")

        # Send update signal
        signal("update").send(self.__class__, specs=[self])

        # Check for selective updates
        if fields:
            document = {field: self._path_to_value(field, self_document) for field in fields}
        else:
            document = self_document

        # Prepare the document to be updated
        document = to_refs(document)
        document.pop("_id", None)

        # Update the document
        self.get_collection().update_one({"_id": self._id}, {"$set": document}, **update_one_kwargs)

        # Send updated signal
        signal("updated").send(self.__class__, specs=[self])

    def upsert(self, *fields: t.Any, **operation_kwargs: t.Any) -> None:
        """
        Update or Insert this document depending on whether it exists or not.
        The presense of an `_id` value in the document is used to determine if
        the document exists.

        NOTE: This method is not the same as specifying the `upsert` flag when
        calling MongoDB. When called for a document with an `_id` value, this
        method will call the database to see if a record with that Id exists,
        if not it will call `insert`, if so it will call `update`. This
        operation is therefore not atomic and much slower than the equivalent
        MongoDB operation (due to the extra call).
        """

        # If no `_id` is provided then we insert the document
        if not self._id:
            return self.insert()

        # If an `_id` is provided then we need to check if it exists before
        # performing the `upsert`.
        #
        if self.count({"_id": self._id}) == 0:
            self.insert(**operation_kwargs)
        else:
            self.update(*fields, **operation_kwargs)

    def delete(self, **delete_one_kwargs: t.Any) -> None:
        """Delete this document"""

        if "_id" not in self.to_dict():
            raise ValueError("Can't delete documents without `_id`")

        # Send delete signal
        signal("delete").send(self.__class__, specs=[self])

        # Delete the document
        self.get_collection().delete_one({"_id": self._id}, **delete_one_kwargs)

        # Send deleted signal
        signal("deleted").send(self.__class__, specs=[self])

    @classmethod
    def find(cls, filter: FilterType = None, **kwargs: t.Any) -> list[SpecDocumentType]:
        """Return a list of documents matching the filter"""
        # Flatten the projection
        kwargs["projection"], references, subs = cls._flatten_projection(
            kwargs.get("projection", cls._default_projection)
        )

        # Find the document
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()

        documents = list(cls.get_collection().find(to_refs(filter), **kwargs))

        # Make sure we found documents
        if not documents:
            return []

        # Dereference the documents (if required)
        if references:
            cls._dereference(documents, references)

        # Add sub-specs to the documents (if required)
        if subs:
            cls._apply_sub_specs(documents, subs)

        return documents

    @classmethod
    def find_one(cls, filter: FilterType = None, **kwargs: t.Any) -> SpecDocumentType:
        """Return the first document matching the filter"""
        # Flatten the projection
        kwargs["projection"], references, subs = cls._flatten_projection(
            kwargs.get("projection", cls._default_projection)
        )

        # Find the document
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()

        coll: Collection[SpecDocumentType] = cls.get_collection()
        document = coll.find_one(to_refs(filter), **kwargs)

        # Make sure we found a document
        if not document:
            return t.cast(SpecDocumentType, {})

        # Dereference the document (if required)
        if references:
            cls._dereference([document], references)

        # Add sub-specs to the document (if required)
        if subs:
            cls._apply_sub_specs([document], subs)

        return document

    def reload(self, **kwargs: t.Any) -> None:
        """Reload the document"""
        spec = self.find_one({"_id": self._id}, **kwargs)
        for field in spec:
            setattr(self, field, spec[field])

    @classmethod
    def insert_many(cls, documents: SpecsOrRawDocuments, **kwargs: t.Any) -> t.Sequence[Self]:
        """Insert a list of documents"""
        # Ensure all documents have been converted to specs
        specs = cls._ensure_specs(documents)

        # Send insert signal
        signal("insert").send(cls, specs=specs)

        # Prepare the documents to be inserted
        _documents = [to_refs(f.to_dict()) for f in specs]

        for _document in _documents:
            if not _document["_id"]:
                _document.pop("_id")

        # Bulk insert
        ids = cls.get_collection().insert_many(_documents, **kwargs).inserted_ids

        # Apply the Ids to the specs
        for i, id in enumerate(ids):
            specs[i]._id = id

        # Send inserted signal
        signal("inserted").send(cls, specs=specs)

        return specs

    @classmethod
    def update_many(
        cls,
        documents: SpecsOrRawDocuments,
        *fields: t.Any,
        update_one_kwargs: t.Any = None,
        bulk_write_kwargs: t.Any = None,
    ) -> None:
        """
        Update multiple documents. Optionally a specific list of fields to
        update can be specified.
        """
        # Ensure all documents have been converted to specs
        specs = cls._ensure_specs(documents)

        if not all(f._id for f in specs):
            raise ValueError("Can't update documents without `_id`s")

        # Send update signal
        signal("update").send(cls, specs=specs)

        # Prepare the documents to be updated

        # Check for selective updates
        if fields:
            _documents = []
            for spec in specs:
                document = {"_id": spec._id}
                for field in fields:
                    document[field] = cls._path_to_value(field, spec.to_dict())
                _documents.append(to_refs(document))
        else:
            _documents = [to_refs(f.to_dict()) for f in specs]

        if not update_one_kwargs:
            update_one_kwargs = {}
        if not bulk_write_kwargs:
            bulk_write_kwargs = {}

        # Update the documents
        requests = []
        for _document in _documents:
            _id = _document.pop("_id")
            requests.append(UpdateOne({"_id": _id}, {"$set": _document}, **update_one_kwargs))

        cls.get_collection().bulk_write(requests, **bulk_write_kwargs)

        # Send updated signal
        signal("updated").send(cls, specs=specs)

    @classmethod
    def unset_many(cls, documents: SpecsOrRawDocuments, *fields: t.Any, **update_many_kwargs: t.Any) -> None:
        """Unset the given list of fields for given documents."""

        # Ensure all documents have been converted to specs
        specs = cls._ensure_specs(documents)

        if not all(f._id for f in specs):
            raise ValueError("Can't update documents without `_id`s")

        # Send update signal
        signal("update").send(cls, specs=specs)

        ids = [spec._id for spec in specs if spec._id]
        # Build the unset object
        unset = {}
        for field in fields:
            unset[field] = True
            for spec in specs:
                spec.to_dict().pop(field, None)

        # Update the document
        cls.get_collection().update_many({"_id": {"$in": ids}}, {"$unset": unset}, **update_many_kwargs)

        # Send updated signal
        signal("updated").send(cls, specs=specs)

    @classmethod
    def delete_many(cls, documents: SpecsOrRawDocuments, **delete_many_kwargs: t.Any) -> None:
        """Delete multiple documents"""

        # Ensure all documents have been converted to specs
        specs = cls._ensure_specs(documents)

        if not all(f._id for f in specs):
            raise ValueError("Can't delete documents without `_id`s")

        # Send delete signal
        signal("delete").send(cls, specs=specs)

        # Prepare the documents to be deleted
        ids = [f._id for f in specs]

        # Delete the documents
        cls.get_collection().delete_many({"_id": {"$in": ids}}, **delete_many_kwargs)

        # Send deleted signal
        signal("deleted").send(cls, specs=specs)

    def soft_delete(self, **update_one_kwargs: t.Any) -> None:
        """Soft delete this document by setting a deleted flag."""
        if "_id" not in self.to_dict():
            raise ValueError("Can't delete documents without `_id`")

        # Send delete signal
        signal("soft_delete").send(self.__class__, specs=[self])

        # Update the document to set the deleted flag
        self.get_collection().update_one({"_id": self._id}, {"$set": {"deleted": True}}, **update_one_kwargs)

        # Send deleted signal
        signal("soft_deleted").send(self.__class__, specs=[self])

    @classmethod
    def find_active(cls, filter: FilterType = None, **find_kwargs: t.Any) -> list[SpecDocumentType]:
        """Return a list of active documents (not soft deleted)."""
        if filter is None:
            filter = {}
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()
        filter["deleted"] = {"$ne": True}  # Exclude soft deleted documents
        return cls.find(filter, **find_kwargs)
