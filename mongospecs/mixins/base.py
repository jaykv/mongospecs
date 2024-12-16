from __future__ import annotations

import typing as t
from abc import abstractmethod
from contextlib import contextmanager
from copy import deepcopy

from bson import BSON, ObjectId
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from typing_extensions import Self

from mongospecs.helpers.empty import Empty, EmptyObject
from mongospecs.types import RawDocuments, SpecBaseType, SpecDocumentType, SpecsOrRawDocuments


class MongoBaseMixin(SpecBaseType):
    _client: t.ClassVar[t.Optional[MongoClient[t.Any]]] = None
    _db: t.ClassVar[t.Optional[Database[t.Any]]] = None
    _collection: t.ClassVar[t.Optional[str]] = None
    _collection_context: t.ClassVar[t.Optional[Collection[t.Any]]] = None
    _default_projection: t.ClassVar[dict[str, t.Any]] = {}
    _empty_type: t.ClassVar[t.Any] = Empty
    _id: t.Union[EmptyObject, ObjectId]

    @classmethod
    def from_document(cls, document: dict[str, t.Any]) -> Self:
        return cls(**document)

    @classmethod
    def from_raw_bson(cls, raw_bson: t.Any) -> t.Any:
        decoded_data = BSON.decode(raw_bson)
        return cls(**decoded_data)

    def __eq__(self, other: t.Any) -> bool:
        if not isinstance(other, self.__class__):
            return False

        return self._id == other._id

    def __lt__(self, other: t.Any) -> bool:
        return self._id < other._id  # type: ignore[no-any-return]

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
            cls._collection_context = existing_context

    @classmethod
    def _path_to_value(cls, path: str, parent_dict: SpecDocumentType) -> t.Any:
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
    def _ensure_specs(cls, documents: SpecsOrRawDocuments) -> t.Sequence[Self]:
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
    def _apply_sub_specs(cls, documents: RawDocuments, subs: dict[str, t.Any]) -> None:
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
    def _dereference(cls, documents: RawDocuments, references: dict[str, t.Any]) -> None:
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

    @classmethod
    @abstractmethod
    def get_fields(cls) -> set[str]:
        raise NotImplementedError
