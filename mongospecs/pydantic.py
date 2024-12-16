import typing as t

from blinker import signal
from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import core_schema
from pymongo import MongoClient

from mongospecs.base import SpecBase, SubSpecBase
from mongospecs.helpers.empty import EmptyObject

__all__ = ["Spec", "SubSpec"]


class _ObjectIdPydanticAnnotation:
    # Based on https://docs.pydantic.dev/latest/usage/types/custom/#handling-third-party-types.

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: t.Any,
        _handler: t.Callable[[t.Any], core_schema.CoreSchema],
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


PyObjectId = t.Annotated[ObjectId, _ObjectIdPydanticAnnotation]


class Spec(BaseModel, SpecBase):
    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    id: t.Optional[PyObjectId] = Field(default=None, alias="_id")

    @classmethod
    def from_document(cls, document: dict[str, t.Any]) -> "Spec":
        return cls.model_construct(**document)

    @property
    def _id(self) -> t.Union[EmptyObject, ObjectId]:
        return t.cast(t.Union[EmptyObject, ObjectId], self.id)

    @_id.setter
    def _id(self, value: ObjectId) -> None:
        self.id = value

    def unset(self, *fields: t.Any) -> None:
        """Unset the given list of fields for this document."""

        # Send update signal
        signal("update").send(self.__class__, specs=[self])

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
        signal("updated").send(self.__class__, specs=[self])

    def encode(self, **encode_kwargs: t.Any) -> bytes:
        return str.encode(self.model_dump_json(**encode_kwargs))

    def decode(self, data: t.Any, **decode_kwargs: t.Any) -> t.Any:
        return self.__class__.model_validate_json(data, **decode_kwargs)

    def to_json_type(self) -> t.Any:
        return self.model_dump(mode="json", by_alias=True, exclude_unset=True)

    def to_dict(self) -> dict[str, t.Any]:
        copy_dict = self.__dict__.copy()
        copy_dict["_id"] = copy_dict.pop("id")
        return copy_dict

    def to_tuple(self) -> tuple[t.Any, ...]:
        return tuple(self.to_dict())

    @classmethod
    def get_fields(cls) -> set[str]:
        return set(cls.model_fields.keys())  # type: ignore[attr-defined,unused-ignore]


class SubSpec(BaseModel, SubSpecBase):
    _parent: t.ClassVar[t.Any] = Spec

    def get(self, name: str, default: t.Any = None) -> t.Any:
        return self.to_dict().get(name, default)

    def to_dict(self) -> dict[str, t.Any]:
        return self.model_dump()


T = t.TypeVar("T", bound=BaseModel)


class PydanticAdapter(Spec, BaseModel):
    def __init__(self, **data: t.Any) -> None:
        """Create a new model by parsing and validating input data from keyword arguments.

        Raises [ValidationError][pydantic_core.ValidationError] if the input data cannot
        be validated to form a valid model.

        __init__ uses __pydantic_self__ instead of the more common self for the first arg
        to allow self as a field name.
        """
        ...


class AdapterBuilder(t.Generic[T]):
    def __call__(
        self, obj: type[T], *, collection: str, client: t.Optional[MongoClient[t.Any]] = None, **kwds: t.Any
    ) -> t.Any:
        class BuiltSpecAdapter(obj, Spec):  # type: ignore
            pass

        BuiltSpecAdapter.__name__ = f"{obj.__name__}SpecAdapter"
        BuiltSpecAdapter._collection = collection
        BuiltSpecAdapter.__doc__ = obj.__doc__
        if client:
            BuiltSpecAdapter._client = client
        return BuiltSpecAdapter


SpecAdapter: AdapterBuilder[BaseModel] = AdapterBuilder()
