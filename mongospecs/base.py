from __future__ import annotations

import typing as t
from abc import abstractmethod
from contextlib import contextmanager
from copy import deepcopy

from blinker import signal
from bson import BSON, ObjectId
from pymongo import MongoClient, UpdateOne
from pymongo.client_session import ClientSession
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ConnectionFailure, OperationFailure
from typing_extensions import Self

from mongospecs.empty import Empty, EmptyObject

from .query import Condition, Group

FilterType = t.Union[None, t.MutableMapping[str, t.Any], Condition, Group]
SpecDocumentType = t.TypeVar("SpecDocumentType", bound=t.MutableMapping[str, t.Any], covariant=True)
Specs = t.Sequence["SpecBase[SpecDocumentType]"]
RawDocuments = t.Sequence[SpecDocumentType]
SpecsOrRawDocuments = t.Sequence[t.Union["SpecBase[SpecDocumentType]", SpecDocumentType]]

T = t.TypeVar("T")


class SpecBase(t.Generic[SpecDocumentType]):
    _client: t.ClassVar[t.Optional[MongoClient[t.Any]]] = None
    _db: t.ClassVar[t.Optional[Database[t.Any]]] = None
    _collection: t.ClassVar[t.Optional[str]] = None
    _collection_context: t.ClassVar[t.Optional[Collection[t.Any]]] = None
    _default_projection: t.ClassVar[dict[str, t.Any]] = {}
    _empty_type: t.ClassVar[t.Any] = Empty
    _id: t.Union[EmptyObject, ObjectId]

    @classmethod
    @abstractmethod
    def get_fields(cls) -> set[str]:
        raise NotImplementedError

    @classmethod
    def from_document(cls, document: dict[str, t.Any]) -> Self:
        return cls(**document)

    @classmethod
    def from_raw_bson(cls, raw_bson: t.Any) -> t.Any:
        decoded_data = BSON.decode(raw_bson)
        return cls(**decoded_data)

    def get(self, name: str, default: t.Any = None) -> t.Any:
        return self.to_dict().get(name, default)

    @abstractmethod
    def encode(self, **encode_kwargs: t.Any) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def decode(self, data: t.Any, **decode_kwargs: t.Any) -> t.Any:
        raise NotImplementedError

    @abstractmethod
    def to_json_type(self) -> dict[str, t.Any]:
        raise NotImplementedError

    @abstractmethod
    def to_dict(self) -> dict[str, t.Any]:
        raise NotImplementedError

    def to_tuple(self) -> tuple[t.Any, ...]:
        raise NotImplementedError

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
        assert "_id" in self_document, "Can't update documents without `_id`"

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

        assert "_id" in self.to_dict(), "Can't delete documents without `_id`"

        # Send delete signal
        signal("delete").send(self.__class__, specs=[self])

        # Delete the document
        self.get_collection().delete_one({"_id": self._id}, **delete_one_kwargs)

        # Send deleted signal
        signal("deleted").send(self.__class__, specs=[self])

    def soft_delete(self, **update_one_kwargs: t.Any) -> None:
        """Soft delete this document by setting a deleted flag."""
        assert "_id" in self.to_dict(), "Can't delete documents without `_id`"

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
    def insert_many(cls, documents: SpecsOrRawDocuments[SpecDocumentType], **kwargs: t.Any) -> Specs[t.Any]:
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
        documents: SpecsOrRawDocuments[SpecDocumentType],
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

        all_count = len(documents)
        assert len([f for f in specs if "_id" in f.to_dict()]) == all_count, "Can't update documents without `_id`s"

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
    def unset_many(
        cls, documents: SpecsOrRawDocuments[SpecDocumentType], *fields: t.Any, **update_many_kwargs: t.Any
    ) -> None:
        """Unset the given list of fields for given documents."""

        # Ensure all documents have been converted to specs
        specs = cls._ensure_specs(documents)

        all_count = len(documents)
        assert len([f for f in specs if "_id" in f.to_dict()]) == all_count, "Can't update documents without `_id`s"

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
    def delete_many(cls, documents: SpecsOrRawDocuments[SpecDocumentType], **delete_many_kwargs: t.Any) -> None:
        """Delete multiple documents"""

        # Ensure all documents have been converted to specs
        specs = cls._ensure_specs(documents)

        all_count = len(documents)
        assert len([f for f in specs if "_id" in f.to_dict()]) == all_count, "Can't delete documents without `_id`s"

        # Send delete signal
        signal("delete").send(cls, specs=specs)

        # Prepare the documents to be deleted
        ids = [f._id for f in specs]

        # Delete the documents
        cls.get_collection().delete_many({"_id": {"$in": ids}}, **delete_many_kwargs)

        # Send deleted signal
        signal("deleted").send(cls, specs=specs)

    # Querying

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

    @classmethod
    def get_collection(cls) -> Collection[t.Any]:
        """Return a reference to the database collection for the class"""
        if cls._collection_context is not None:
            return cls._collection_context

        return t.cast(Collection[t.Any], getattr(cls.get_db(), cls._collection or cls.__name__))

    @classmethod
    def get_db(cls) -> Database[SpecDocumentType]:
        """Return the database for the collection"""
        if not cls._client:
            raise NotImplementedError("_client is not setup yet")
        if cls._db is not None:
            return t.cast(Database[SpecDocumentType], getattr(cls._client, cls._db.name))
        return t.cast(Database[SpecDocumentType], cls._client.get_default_database())

    @classmethod
    @contextmanager
    def with_options(cls, **options: t.Any) -> t.Generator[t.Any, t.Any, None]:
        existing_context = getattr(cls, "_collection_context", None)

        try:
            collection = cls.get_collection()
            cls._collection_context = collection.with_options(**options)
            yield cls._collection_context

        finally:
            if cls._collection_context is None:
                del cls._collection_context
            else:
                cls._collection_context = existing_context

    @classmethod
    def _path_to_value(cls, path: str, parent_dict: t.MutableMapping[str, t.Any]) -> t.Any:
        """Return a value from a dictionary at the given path"""
        keys: list[str] = cls._path_to_keys(path)

        # Traverse to the tip of the path
        child_dict = parent_dict
        for key in keys[:-1]:
            child_dict = child_dict.get(key)  # type: ignore[assignment]

            # unpaved path- return None
            if child_dict is None:
                return None

        return child_dict.get(keys[-1])

    @classmethod
    def _path_to_keys(cls, path: str) -> list[str]:
        """Return a list of keys for a given path"""
        return path.split(".")

    @classmethod
    def _ensure_specs(cls, documents: SpecsOrRawDocuments[SpecDocumentType]) -> Specs[SpecDocumentType]:
        """
        Ensure all items in a list are specs by converting those that aren't.
        """
        specs = []
        for document in documents:
            if isinstance(document, cls):
                specs.append(document)
            elif isinstance(document, dict):
                specs.append(cls(**document))
        return specs

    @classmethod
    def _apply_sub_specs(cls, documents: RawDocuments[SpecDocumentType], subs: dict[str, t.Any]) -> None:
        """Convert embedded documents to sub-specs for one or more documents"""

        # Dereference each reference
        for path, projection in subs.items():
            # Get the SubSpec class we'll use to wrap the embedded document
            sub = None
            expect_map = False
            if "$sub" in projection:
                sub = projection.pop("$sub")
            elif "$sub." in projection:
                sub = projection.pop("$sub.")
                expect_map = True
            else:
                continue

            # Add sub-specs to the documents
            raw_subs: list[t.Any] = []
            for document in documents:
                value = cls._path_to_value(path, document)
                if value is None:
                    continue

                if isinstance(value, dict):
                    if expect_map:
                        # Dictionary of embedded documents
                        raw_subs += value.values()
                        for k, v in value.items():
                            if isinstance(v, list):
                                value[k] = [sub(u) for u in v if isinstance(u, dict)]
                            else:
                                value[k] = sub(**v)

                    # Single embedded document
                    else:
                        raw_subs.append(value)
                        value = sub(**value)

                elif isinstance(value, list):
                    # List of embedded documents
                    raw_subs += value
                    value = [sub(**v) for v in value if isinstance(v, dict)]

                else:
                    raise TypeError("Not a supported sub-spec type")

                child_document = document
                keys = cls._path_to_keys(path)
                for key in keys[:-1]:
                    child_document = child_document[key]
                child_document[keys[-1]] = value

            # Apply the projection to the list of sub specs
            if projection:
                sub._apply_projection(raw_subs, projection)

    @classmethod
    def _flatten_projection(
        cls, projection: dict[str, t.Any]
    ) -> tuple[dict[str, t.Any], dict[str, t.Any], dict[str, t.Any]]:
        """
        Flatten a structured projection (structure projections support for
        projections of (to be) dereferenced fields.
        """

        # If `projection` is empty return a full projection based on `_fields`
        if not projection:
            return {f: True for f in cls.get_fields()}, {}, {}

        # Flatten the projection
        flat_projection: dict[str, t.Any] = {}
        references = {}
        subs = {}
        inclusive = True
        for key, value in deepcopy(projection).items():
            if isinstance(value, dict):
                # Build the projection value for the field (allowing for
                # special mongo directives).
                values_to_project = {
                    k: v for k, v in value.items() if k.startswith("$") and k not in ["$ref", "$sub", "$sub."]
                }
                project_value = True if len(values_to_project) == 0 else {key: values_to_project}

                if project_value is not True:
                    inclusive = False

                # Store a reference/sub-spec projection
                if "$ref" in value:
                    references[key] = value

                elif "$sub" in value or "$sub." in value:
                    subs[key] = value
                    sub_spec = None
                    if "$sub" in value:
                        sub_spec = value["$sub"]

                    if "$sub." in value:
                        sub_spec = value["$sub."]

                    if sub_spec:
                        project_value = sub_spec._projection_to_paths(key, value)

                if isinstance(project_value, dict):
                    flat_projection |= project_value
                else:
                    flat_projection[key] = project_value

            elif key == "$ref":
                # Strip any $ref key
                continue

            elif key == "$sub" or key == "$sub.":
                # Strip any $sub key
                continue

            elif key.startswith("$"):
                # Strip mongo operators
                continue

            else:
                # Store the root projection value
                flat_projection[key] = value
                inclusive = False

        # If only references and sub-specs where specified in the projection
        # then return a full projection based on `_fields`.
        if inclusive:
            flat_projection = {f: True for f in cls.get_fields()}

        return flat_projection, references, subs

    @classmethod
    def _dereference(cls, documents: RawDocuments[t.Any], references: dict[str, t.Any]) -> None:
        """Dereference one or more documents"""

        # Dereference each reference
        for path, projection in references.items():
            # Check there is a $ref in the projection, else skip it
            if "$ref" not in projection:
                continue

            # Collect Ids of documents to dereference
            ids = set()
            for document in documents:
                value = cls._path_to_value(path, document)
                if not value:
                    continue

                if isinstance(value, list):
                    ids.update(value)

                elif isinstance(value, dict):
                    ids.update(value.values())

                else:
                    ids.add(value)

            # Find the referenced documents
            ref = projection.pop("$ref")

            specs = ref.many({"_id": {"$in": list(ids)}}, projection=projection)
            specs = {f._id: f for f in specs}

            # Add dereferenced specs to the document
            for document in documents:
                value = cls._path_to_value(path, document)
                if not value:
                    continue

                if isinstance(value, list):
                    # List of references
                    value = [specs[id] for id in value if id in specs]

                elif isinstance(value, dict):
                    # Dictionary of references
                    value = {key: specs.get(id) for key, id in value.items()}

                else:
                    value = specs.get(value)

                child_document = document
                keys = cls._path_to_keys(path)
                for key in keys[:-1]:
                    child_document = child_document[key]
                child_document[keys[-1]] = value

    @classmethod
    def _remove_keys(cls, parent_dict: dict[str, t.Any], paths: list[str]) -> None:
        """
        Remove a list of keys from a dictionary.

        Keys are specified as a series of `.` separated paths for keys in child
        dictionaries, e.g 'parent_key.child_key.grandchild_key'.
        """

        for path in paths:
            keys = cls._path_to_keys(path)

            # Traverse to the tip of the path
            child_dict = parent_dict
            for key in keys[:-1]:
                child_dict = child_dict.get(key, {})

                if not isinstance(child_dict, dict):
                    continue

            if not isinstance(child_dict, dict):
                continue

            child_dict.pop(keys[-1], None)

    # Signals
    @classmethod
    def listen(cls, event: str, func: t.Callable[..., t.Any]) -> None:
        """Add a callback for a signal against the class"""
        signal(event).connect(func, sender=cls)

    @classmethod
    def stop_listening(cls, event: str, func: t.Callable[..., t.Any]) -> None:
        """Remove a callback for a signal against the class"""
        signal(event).disconnect(func, sender=cls)

    # Integrity helpers

    @classmethod
    def cascade(cls, ref_cls: "SpecBase[SpecDocumentType]", field: str, specs: Specs[SpecDocumentType]) -> None:
        """Apply a cascading delete (does not emit signals)"""
        ids = [to_refs(getattr(f, field)) for f in specs if hasattr(f, field)]
        ref_cls.get_collection().delete_many({"_id": {"$in": ids}})

    @classmethod
    def nullify(cls, ref_cls: "SpecBase[SpecDocumentType]", field: str, specs: Specs[SpecDocumentType]) -> None:
        """Nullify a reference field (does not emit signals)"""
        ids = [to_refs(f) for f in specs]
        ref_cls.get_collection().update_many({field: {"$in": ids}}, {"$set": {field: None}})

    @classmethod
    def pull(cls, ref_cls: "SpecBase[SpecDocumentType]", field: str, specs: Specs[SpecDocumentType]) -> None:
        """Pull references from a list field (does not emit signals)"""
        ids = [to_refs(f) for f in specs]
        ref_cls.get_collection().update_many({field: {"$in": ids}}, {"$pull": {field: {"$in": ids}}})

    # Index management
    @classmethod
    def create_index(cls, keys: t.Union[str, list[str]], **kwargs: t.Any) -> str:
        """
        Create an index on the specified keys (a single key or a list of keys).
        """
        if isinstance(keys, str):
            index_keys = [(keys, 1)]  # 1 for ascending order
        else:
            index_keys = [(key, 1) for key in keys]  # 1 for ascending order

        return cls.get_collection().create_index(index_keys, **kwargs)

    # Transactions
    @classmethod
    @contextmanager
    def transaction(cls, **start_transaction_kwargs: t.Any) -> t.Generator[ClientSession, t.Any, None]:
        """Context manager for handling MongoDB transactions."""
        if not cls._client:
            raise RuntimeError("MongoDB client (_client) is not set. Cannot start a transaction.")
        session = cls._client.start_session()
        session.start_transaction(**start_transaction_kwargs)

        try:
            yield session  # Allow operations to be performed within this session
            session.commit_transaction()  # Commit if no exceptions
            signal("transaction_committed").send(cls)  # Emit signal after commit
        except (ConnectionFailure, OperationFailure) as e:
            session.abort_transaction()  # Abort on error
            signal("transaction_aborted").send(cls)  # Emit signal after abort
            raise e  # Re-raise the exception for handling
        finally:
            session.end_session()

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

    def __eq__(self, other: t.Any) -> bool:
        if not isinstance(other, self.__class__):
            return False

        return self._id == other._id

    def __lt__(self, other: t.Any) -> t.Any:
        return self._id < other._id


class SubSpecBase:
    _parent: t.ClassVar[t.Any] = SpecBase

    def to_dict(self) -> t.Any:
        raise NotImplementedError()

    @classmethod
    def _apply_projection(cls, documents: list[t.Any], projection: t.Mapping[str, t.Any]) -> None:
        # Find reference and sub-spec mappings
        references = {}
        subs = {}
        for key, value in deepcopy(projection).items():
            if not isinstance(value, dict):
                continue

            # Store a reference/sub-spec projection
            if "$ref" in value:
                references[key] = value
            elif "$sub" in value or "$sub." in value:
                subs[key] = value

        # Dereference the documents (if required)
        if references:
            cls._parent._dereference(documents, references)

        # Add sub-specs to the documents (if required)
        if subs:
            cls._parent._apply_sub_specs(documents, subs)

    @classmethod
    def _projection_to_paths(cls, root_key: str, projection: t.Mapping[str, t.Any]) -> t.Any:
        """
        Expand a $sub/$sub. projection to a single projection of True (if
        inclusive) or a map of full paths (e.g `employee.company.tel`).
        """

        # Referenced projections are handled separately so just flag the
        # reference field to true.
        if "$ref" in projection:
            return True

        inclusive = True
        sub_projection: dict[str, t.Any] = {}
        for key, value in projection.items():
            if key in ["$sub", "$sub."]:
                continue

            if key.startswith("$"):
                sub_projection[root_key] = {key: value}
                inclusive = False
                continue

            sub_key = f"{root_key}.{key}"

            if isinstance(value, dict):
                sub_value = cls._projection_to_paths(sub_key, value)
                if isinstance(sub_value, dict):
                    sub_projection |= sub_value
                else:
                    sub_projection[sub_key] = True

            else:
                sub_projection[sub_key] = True
                inclusive = False

        if inclusive:
            # No specific keys so this is inclusive
            return True

        return sub_projection


def to_refs(value: t.Any) -> t.Any:
    """Convert all Spec instances within the given value to Ids"""
    # Spec
    if isinstance(value, SpecBase):
        return getattr(value, "_id")

    # SubSpec
    elif isinstance(value, SubSpecBase):
        return to_refs(value.to_dict())

    # Lists
    elif isinstance(value, (list, tuple)):
        return [to_refs(v) for v in value]

    # Dictionaries
    elif isinstance(value, dict):
        return {k: to_refs(v) for k, v in value.items()}

    return value
