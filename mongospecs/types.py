import typing as t
from abc import abstractmethod

from mongospecs.helpers.query import Condition, Group

FilterType = t.Union[None, t.MutableMapping[str, t.Any], Condition, Group]
SpecDocumentType = t.MutableMapping[str, t.Any]
RawDocuments = t.Sequence[SpecDocumentType]
SpecsOrRawDocuments = t.Sequence[t.Union["SpecBaseType", SpecDocumentType]]


class SpecBaseType:
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

    @abstractmethod
    def to_tuple(self) -> tuple[t.Any, ...]:
        raise NotImplementedError


class SubSpecBaseType:
    @abstractmethod
    def to_dict(self) -> t.Any:
        raise NotImplementedError
