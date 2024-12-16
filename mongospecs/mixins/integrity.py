import typing as t

from typing_extensions import Self

from mongospecs.mixins.base import MongoBaseMixin
from mongospecs.utils import to_refs


class IntegrityMixin(MongoBaseMixin):
    @classmethod
    def cascade(cls, ref_cls: "type[MongoBaseMixin]", field: str, specs: t.Sequence[Self]) -> None:
        """Apply a cascading delete (does not emit signals)"""
        ids = [to_refs(getattr(f, field)) for f in specs if hasattr(f, field)]
        ref_cls.get_collection().delete_many({"_id": {"$in": ids}})

    @classmethod
    def nullify(cls, ref_cls: "type[MongoBaseMixin]", field: str, specs: t.Sequence[Self]) -> None:
        """Nullify a reference field (does not emit signals)"""
        ids = [to_refs(f) for f in specs]
        ref_cls.get_collection().update_many({field: {"$in": ids}}, {"$set": {field: None}})

    @classmethod
    def pull(cls, ref_cls: "type[MongoBaseMixin]", field: str, specs: t.Sequence[Self]) -> None:
        """Pull references from a list field (does not emit signals)"""
        ids = [to_refs(f) for f in specs]
        ref_cls.get_collection().update_many({field: {"$in": ids}}, {"$pull": {field: {"$in": ids}}})
