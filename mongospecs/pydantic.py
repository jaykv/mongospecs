from typing import Annotated, Any, Callable, ClassVar, Optional, Union, cast

from blinker import signal
from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import core_schema
from pymongo import MongoClient

from .base import EmptyObject, SpecBase, SubSpecBase

__all__ = ["Spec", "SubSpec"]


class _ObjectIdPydanticAnnotation:
    # Based on https://docs.pydantic.dev/latest/usage/types/custom/#handling-third-party-types.

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Callable[[Any], core_schema.CoreSchema],
    ) -> core_schema.CoreSchema:
        def validate_from_str(input_value: str) -> ObjectId:
            return ObjectId(input_value)

        return core_schema.union_schema(
            [
                # check if it's an instance first before doing any further work
                core_schema.is_instance_schema(ObjectId),
                core_schema.no_info_plain_validator_function(validate_from_str),
            ],
            serialization=core_schema.to_string_ser_schema(),
        )


PyObjectId = Annotated[ObjectId, _ObjectIdPydanticAnnotation]


def to_refs(value: Any) -> Any:
    """Convert all Frame instances within the given value to Ids"""
    # Frame
    if isinstance(value, SpecBase):
        return getattr(value, "id")

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


class Spec(BaseModel, SpecBase):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: Optional[PyObjectId] = Field(default=None, alias="_id")

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Spec":
        return cls.model_construct(**document)

    @property
    def _id(self) -> Union[EmptyObject, ObjectId]:
        return cast(Union[EmptyObject, ObjectId], self.id)

    @_id.setter
    def _id(self, value: ObjectId) -> None:
        self.id = value

    def unset(self, *fields: Any) -> None:
        """Unset the given list of fields for this document."""

        # Send update signal
        signal("update").send(self.__class__, frames=[self])

        # Clear the fields from the document and build the unset object
        unset = {}
        for field in fields:
            setattr(self, field, self._empty_type)
            unset[field] = True

            ## pydantic specific change:
            ## remove from model fields set so it excludes when `to_json_type` is called
            self.model_fields_set.remove(field)

        # Update the document
        self.get_collection().update_one({"_id": self._id}, {"$unset": unset})

        # Send updated signal
        signal("updated").send(self.__class__, frames=[self])

    def encode(self, **encode_kwargs: Any) -> bytes:
        return str.encode(self.model_dump_json(**encode_kwargs))

    def decode(self, data: Any, **decode_kwargs: Any) -> Any:
        return self.__class__.model_validate_json(data, **decode_kwargs)

    def to_json_type(self) -> Any:
        return self.model_dump(mode="json", by_alias=True, exclude_unset=True)

    def to_dict(self) -> dict[str, Any]:
        copy_dict = self.__dict__.copy()
        copy_dict["_id"] = copy_dict.pop("id")
        return copy_dict

    # note: pydantic BaseModel already has to_tuple

    @classmethod
    def get_fields(cls) -> set[str]:
        return set(cls.model_fields.keys())


class SubSpec(BaseModel, SubSpecBase):
    _parent: ClassVar[Any] = Spec

    def get(self, name: str, default: Any = None) -> Any:
        return self.to_dict().get(name, default)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class PydanticAdapter(Spec, BaseModel):
    def __init__(self, **data: Any) -> None:
        """Create a new model by parsing and validating input data from keyword arguments.

        Raises [ValidationError][pydantic_core.ValidationError] if the input data cannot
        be validated to form a valid model.

        __init__ uses __pydantic_self__ instead of the more common self for the first arg
        to allow self as a field name.
        """
        ...


class AdapterBuilder:
    def __call__(
        self, obj: type[BaseModel], *, collection: str, client: Optional[MongoClient] = None, **kwds: Any
    ) -> type[PydanticAdapter]:
        class BuiltSpecAdapter(Spec, obj):  # type: ignore
            pass

        BuiltSpecAdapter.__name__ = f"{obj.__name__}SpecAdapter"
        BuiltSpecAdapter._collection = collection
        BuiltSpecAdapter.__doc__ = obj.__doc__
        if client:
            BuiltSpecAdapter._client = client
        return cast(type[PydanticAdapter], BuiltSpecAdapter)


SpecAdapter = AdapterBuilder()
