from __future__ import annotations
from contextlib import contextmanager

from copy import deepcopy

import attrs
from .empty import Empty, EmptyObject
import msgspec
from typing_extensions import Self
from typing import Callable, ClassVar, Any, Generator, Mapping, Optional, Union, Sequence
from bson import ObjectId
from pymongo import MongoClient, UpdateOne
from pymongo.database import Database
from pymongo.collection import Collection
from blinker import signal

from .utils import to_refs
from .query import Condition, Group

from .se import MongoEncoder, mongo_dec_hook, mongo_enc_hook

__all__ = ["Spec", "SubSpec"]


Specs = Sequence["Spec"]
RawDocuments = Sequence[dict[str, Any]]
SpecsOrRawDocuments = Union[Specs, RawDocuments]


@attrs.define(kw_only=True)
class Spec:
    _client: ClassVar[Optional[MongoClient]] = None
    _db: ClassVar[Optional[Database]] = None
    _collection: ClassVar[Optional[str]] = None
    _collection_context: ClassVar[Optional[Collection]] = None
    _default_projection: ClassVar[dict[str, Any]] = {}

    _id: Union[EmptyObject, ObjectId] = attrs.field(default=Empty)

    def get(self, name, default=None):  # -> Any:
        return self.to_dict().get(name, default)

    # Operations
    def insert(self) -> None:
        """Insert this document"""
        # Send insert signal
        signal("insert").send(self.__class__, frames=[self])

        document_dict = self.to_dict()
        if self._id:
            document_dict.pop("_id", None)
        # Prepare the document to be inserted
        document = to_refs(document_dict)

        # Insert the document and update the Id
        self._id = self.get_collection().insert_one(document).inserted_id

        # Send inserted signal
        signal("inserted").send(self.__class__, frames=[self])

    def unset(self, *fields: Any) -> None:
        """Unset the given list of fields for this document."""

        # Send update signal
        signal("update").send(self.__class__, frames=[self])

        # Clear the fields from the document and build the unset object
        unset = {}
        for field in fields:
            setattr(self, field, attrs.NOTHING)
            unset[field] = True

        # Update the document
        self.get_collection().update_one({"_id": self._id}, {"$unset": unset})

        # Send updated signal
        signal("updated").send(self.__class__, frames=[self])

    def update(self, *fields: Any) -> None:
        """
        Update this document. Optionally a specific list of fields to update can
        be specified.
        """
        self_document = self.to_dict()
        assert "_id" in self_document, "Can't update documents without `_id`"

        # Send update signal
        signal("update").send(self.__class__, frames=[self])

        # Check for selective updates
        if len(fields) > 0:
            document = {}
            for field in fields:
                document[field] = self._path_to_value(field, self_document)
        else:
            document = self_document

        # Prepare the document to be updated
        document = to_refs(document)
        document.pop("_id", None)

        # Update the document
        self.get_collection().update_one({"_id": self._id}, {"$set": document})

        # Send updated signal
        signal("updated").send(self.__class__, frames=[self])

    def upsert(self, *fields: Any) -> None:
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
            self.insert()
        else:
            self.update(*fields)

    def delete(self) -> None:
        """Delete this document"""

        assert "_id" in self.to_dict(), "Can't delete documents without `_id`"

        # Send delete signal
        signal("delete").send(self.__class__, frames=[self])

        # Delete the document
        self.get_collection().delete_one({"_id": self._id})

        # Send deleted signal
        signal("deleted").send(self.__class__, frames=[self])

    @classmethod
    def find_one(cls, filter=None, **kwargs) -> Mapping[str, Any]:
        """Return the first document matching the filter"""
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
            return {}

        # Dereference the document (if required)
        if references:
            cls._dereference([document], references)

        # Add sub-frames to the document (if required)
        if subs:
            cls._apply_sub_frames([document], subs)

        return document

    def reload(self, **kwargs):
        """Reload the document"""
        frame = self.find_one({"_id": self._id}, **kwargs)
        for field in frame:
            setattr(self, field, frame[field])

    @classmethod
    def insert_many(cls, documents: SpecsOrRawDocuments) -> Specs:
        """Insert a list of documents"""
        # Ensure all documents have been converted to frames
        frames = cls._ensure_frames(documents)

        # Send insert signal
        signal("insert").send(cls, frames=frames)

        # Prepare the documents to be inserted
        _documents = [to_refs(f.to_dict()) for f in frames]

        for _document in _documents:
            if not _document["_id"]:
                _document.pop("_id")

        # Bulk insert
        ids = cls.get_collection().insert_many(_documents).inserted_ids

        # Apply the Ids to the frames
        for i, id in enumerate(ids):
            frames[i]._id = id

        # Send inserted signal
        signal("inserted").send(cls, frames=frames)

        return frames

    @classmethod
    def update_many(cls, documents: SpecsOrRawDocuments, *fields: Any) -> None:
        """
        Update multiple documents. Optionally a specific list of fields to
        update can be specified.
        """
        # Ensure all documents have been converted to frames
        frames = cls._ensure_frames(documents)

        all_count = len(documents)
        assert len([f for f in frames if "_id" in f.to_dict()]) == all_count, "Can't update documents without `_id`s"

        # Send update signal
        signal("update").send(cls, frames=frames)

        # Prepare the documents to be updated

        # Check for selective updates
        if len(fields) > 0:
            _documents = []
            for frame in frames:
                document = {"_id": frame._id}
                for field in fields:
                    document[field] = cls._path_to_value(field, frame.to_dict())
                _documents.append(to_refs(document))
        else:
            _documents = [to_refs(f.to_dict()) for f in frames]

        # Update the documents
        requests = []
        for _document in _documents:
            _id = _document.pop("_id")
            requests.append(UpdateOne({"_id": _id}, {"$set": _document}))

        cls.get_collection().bulk_write(requests)

        # Send updated signal
        signal("updated").send(cls, frames=frames)

    @classmethod
    def unset_many(cls, documents: SpecsOrRawDocuments, *fields: Any) -> None:
        """Unset the given list of fields for given documents."""

        # Ensure all documents have been converted to frames
        frames = cls._ensure_frames(documents)

        all_count = len(documents)
        assert len([f for f in frames if "_id" in f.to_dict()]) == all_count, "Can't update documents without `_id`s"

        # Send update signal
        signal("update").send(cls, frames=frames)

        # Clear the fields from the documents and build a list of ids to
        # update.
        ids = []
        for frame in frames:
            if frame._id:
                ids.append(frame._id)

        # Build the unset object
        unset = {}
        for field in fields:
            unset[field] = True
            for frame in frames:
                frame.to_dict().pop(field, None)

        # Update the document
        cls.get_collection().update_many({"_id": {"$in": ids}}, {"$unset": unset})

        # Send updated signal
        signal("updated").send(cls, frames=frames)

    @classmethod
    def delete_many(cls, documents: SpecsOrRawDocuments):
        """Delete multiple documents"""

        # Ensure all documents have been converted to frames
        frames = cls._ensure_frames(documents)

        all_count = len(documents)
        assert len([f for f in frames if "_id" in f.to_dict()]) == all_count, "Can't delete documents without `_id`s"

        # Send delete signal
        signal("delete").send(cls, frames=frames)

        # Prepare the documents to be deleted
        ids = [f._id for f in frames]

        # Delete the documents
        cls.get_collection().delete_many({"_id": {"$in": ids}})

        # Send deleted signal
        signal("deleted").send(cls, frames=frames)

    @classmethod
    def _ensure_frames(cls, documents: SpecsOrRawDocuments) -> Specs:
        """
        Ensure all items in a list are frames by converting those that aren't.
        """
        frames = []
        for document in documents:
            if not isinstance(document, Spec):
                frames.append(cls(**document))
            else:
                frames.append(document)
        return frames

    # Querying

    @classmethod
    def by_id(cls, id, **kwargs) -> Self | None:
        """Get a document by ID"""
        return cls.one({"_id": id}, **kwargs)

    @classmethod
    def count(cls, filter=None, **kwargs) -> int:
        """Return a count of documents matching the filter"""
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()

        filter = to_refs(filter)

        if filter:
            return cls.get_collection().count_documents(to_refs(filter), **kwargs)
        else:
            return cls.get_collection().estimated_document_count(**kwargs)

    @classmethod
    def ids(cls, filter=None, **kwargs) -> list[ObjectId]:
        """Return a list of Ids for documents matching the filter"""
        # Find the documents
        if isinstance(filter, (Condition, Group)):
            filter = filter.to_dict()

        documents = cls.get_collection().find(to_refs(filter), projection={"_id": True}, **kwargs)

        return [d["_id"] for d in list(documents)]

    @classmethod
    def one(cls, filter=None, **kwargs) -> Optional[Self]:
        """Return the first document matching the filter"""
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

        # Add sub-frames to the document (if required)
        if subs:
            cls._apply_sub_frames([document], subs)

        return cls(**document)

    @classmethod
    def many(cls, filter=None, **kwargs) -> list[Self]:
        """Return a list of documents matching the filter"""
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

        # Add sub-frames to the documents (if required)
        if subs:
            cls._apply_sub_frames(documents, subs)

        return [cls(**d) for d in documents]

    @classmethod
    def get_collection(cls) -> Collection[Any]:
        """Return a reference to the database collection for the class"""
        if cls._collection_context is not None:
            return cls._collection_context

        return getattr(cls.get_db(), cls._collection or cls.__name__)

    @classmethod
    def get_db(cls) -> Database:
        """Return the database for the collection"""
        if not cls._client:
            raise NotImplementedError("_client is not setup yet")
        if cls._db is not None:
            return getattr(cls._client, cls._db.name)
        return cls._client.get_default_database()

    @classmethod
    @contextmanager
    def with_options(cls, **options: Any) -> Generator[Any, Any, None]:
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
    def _apply_sub_frames(cls, documents: RawDocuments, subs: dict[str, Any]) -> None:
        """Convert embedded documents to sub-frames for one or more documents"""

        # Dereference each reference
        for path, projection in subs.items():

            # Get the SubFrame class we'll use to wrap the embedded document
            sub = None
            expect_map = False
            if "$sub" in projection:
                sub = projection.pop("$sub")
            elif "$sub." in projection:
                sub = projection.pop("$sub.")
                expect_map = True
            else:
                continue

            # Add sub-frames to the documents
            raw_subs: list[Any] = []
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
                    raise TypeError("Not a supported sub-frame type")

                child_document = document
                keys = cls._path_to_keys(path)
                for key in keys[:-1]:
                    child_document = child_document[key]
                child_document[keys[-1]] = value

            # Apply the projection to the list of sub frames
            if projection:
                sub._apply_projection(raw_subs, projection)

    @classmethod
    def _dereference(cls, documents: RawDocuments, references: dict[str, Any]):
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

            frames = ref.many({"_id": {"$in": list(ids)}}, projection=projection)
            frames = {f._id: f for f in frames}

            # Add dereferenced frames to the document
            for document in documents:
                value = cls._path_to_value(path, document)
                if not value:
                    continue

                if isinstance(value, list):
                    # List of references
                    value = [frames[id] for id in value if id in frames]

                elif isinstance(value, dict):
                    # Dictionary of references
                    value = {key: frames.get(id) for key, id in value.items()}

                else:
                    value = frames.get(value, None)

                child_document = document
                keys = cls._path_to_keys(path)
                for key in keys[:-1]:
                    child_document = child_document[key]
                child_document[keys[-1]] = value

    @classmethod
    def _flatten_projection(cls, projection: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """
        Flatten a structured projection (structure projections support for
        projections of (to be) dereferenced fields.
        """

        # If `projection` is empty return a full projection based on `_fields`
        if not projection:
            return {f: True for f in attrs.fields(cls)}, {}, {}

        # Flatten the projection
        flat_projection = {}
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

                # Store a reference/sub-frame projection
                if "$ref" in value:
                    references[key] = value

                elif "$sub" in value or "$sub." in value:
                    subs[key] = value
                    sub_frame = None
                    if "$sub" in value:
                        sub_frame = value["$sub"]

                    if "$sub." in value:
                        sub_frame = value["$sub."]

                    if sub_frame:
                        project_value = sub_frame._projection_to_paths(key, value)

                if isinstance(project_value, dict):
                    flat_projection.update(project_value)

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

        # If only references and sub-frames where specified in the projection
        # then return a full projection based on `_fields`.
        if inclusive:
            flat_projection = {f: True for f in attrs.fields(cls)}

        return flat_projection, references, subs

    @classmethod
    def _path_to_value(cls, path: str, parent_dict: dict[str, Any]) -> Any:
        """Return a value from a dictionary at the given path"""
        keys: list[str] = Spec._path_to_keys(path)  # type: ignore

        # Traverse to the tip of the path
        child_dict = parent_dict
        for key in keys[:-1]:
            child_dict = child_dict.get(key)  # type: ignore[assignment]

            # unpaved path- return None
            if child_dict is None:
                return None

        return child_dict.get(keys[-1])

    # @lru_cache()
    @classmethod
    def _path_to_keys(cls, path: str) -> list[str]:
        """Return a list of keys for a given path"""
        return path.split(".")

    # Signals

    @classmethod
    def listen(cls, event: str, func: Callable) -> None:
        """Add a callback for a signal against the class"""
        signal(event).connect(func, sender=cls)

    @classmethod
    def stop_listening(cls, event: str, func: Callable) -> None:
        """Remove a callback for a signal against the class"""
        signal(event).disconnect(func, sender=cls)

    # Integrity helpers

    @classmethod
    def cascade(cls, ref_cls, field, frames):
        """Apply a cascading delete (does not emit signals)"""
        ids = [to_refs(getattr(f, field)) for f in frames if hasattr(f, field)]
        ref_cls.get_collection().delete_many({"_id": {"$in": ids}})

    @classmethod
    def nullify(cls, ref_cls, field, frames):
        """Nullify a reference field (does not emit signals)"""
        ids = [to_refs(f) for f in frames]
        ref_cls.get_collection().update_many({field: {"$in": ids}}, {"$set": {field: None}})

    @classmethod
    def pull(cls, ref_cls, field, frames):
        """Pull references from a list field (does not emit signals)"""
        ids = [to_refs(f) for f in frames]
        ref_cls.get_collection().update_many({field: {"$in": ids}}, {"$pull": {field: {"$in": ids}}})

    def encode(self, **encode_kwargs: Any) -> bytes:
        return msgspec.json.encode(self, **encode_kwargs) if encode_kwargs else MongoEncoder.encode(self)

    def decode(self, data: Any) -> Any:
        return msgspec.json.decode(data, type=self.__class__, dec_hook=mongo_dec_hook)

    def to_json_type(self) -> Any:
        return msgspec.to_builtins(self, enc_hook=mongo_enc_hook)

    def to_dict(self) -> dict[str, Any]:
        return attrs.asdict(self)

    def to_tuple(self) -> tuple[Any, ...]:
        return attrs.astuple(self)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, self.__class__):
            return False
        if self._id is Empty or other._id is Empty:
            return False
        return self._id == other._id

    def __lt__(self, other: Any) -> Any:
        return self._id < other._id


@attrs.define(kw_only=True, dict=True)
class SubSpec:
    def get(self, name, default=None):  # -> Any:
        return self.to_dict().get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)

    @classmethod
    def _apply_projection(cls, documents, projection):

        # Find reference and sub-frame mappings
        references = {}
        subs = {}
        for key, value in deepcopy(projection).items():

            if not isinstance(value, dict):
                continue

            # Store a reference/sub-frame projection
            if "$ref" in value:
                references[key] = value
            elif "$sub" in value or "$sub." in value:
                subs[key] = value

        # Dereference the documents (if required)
        if references:
            Spec._dereference(documents, references)

        # Add sub-frames to the documents (if required)
        if subs:
            Spec._apply_sub_frames(documents, subs)

    @classmethod
    def _projection_to_paths(cls, root_key, projection):
        """
        Expand a $sub/$sub. projection to a single projection of True (if
        inclusive) or a map of full paths (e.g `employee.company.tel`).
        """

        # Referenced projections are handled separately so just flag the
        # reference field to true.
        if "$ref" in projection:
            return True

        inclusive = True
        sub_projection = {}
        for key, value in projection.items():
            if key in ["$sub", "$sub."]:
                continue

            if key.startswith("$"):
                sub_projection[root_key] = {key: value}
                inclusive = False
                continue

            sub_key = root_key + "." + key

            if isinstance(value, dict):
                sub_value = cls._projection_to_paths(sub_key, value)
                if isinstance(sub_value, dict):
                    sub_projection.update(sub_value)
                else:
                    sub_projection[sub_key] = True

            else:
                sub_projection[sub_key] = True
                inclusive = False

        if inclusive:
            # No specific keys so this is inclusive
            return True

        return sub_projection
