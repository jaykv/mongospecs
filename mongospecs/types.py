
from abc import abstractmethod
import typing as t
from .helpers.query import Condition, Group
    
FilterType = t.Union[None, t.MutableMapping[str, t.Any], Condition, Group]
SpecDocumentType = t.TypeVar("SpecDocumentType", bound=t.MutableMapping[str, t.Any], covariant=True)
Specs = t.Sequence["SpecBaseType[SpecDocumentType]"]
RawDocuments = t.Sequence[SpecDocumentType]
SpecsOrRawDocuments = t.Sequence[t.Union["SpecBaseType[SpecDocumentType]", SpecDocumentType]]

class SpecBaseType(t.Generic[SpecDocumentType]):
    
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
    
    @classmethod
    @abstractmethod
    def get_fields(cls) -> set[str]:
        raise NotImplementedError
    
class SubSpecBaseType(t.Generic[SpecDocumentType]):
    @abstractmethod
    def to_dict(self) -> t.Any:
        raise NotImplementedError()