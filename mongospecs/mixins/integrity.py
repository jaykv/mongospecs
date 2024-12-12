
from .base import MongoBaseMixin
from ..types import SpecBaseType, SpecDocumentType

class IntegrityMixin(MongoBaseMixin):
    @classmethod
    def cascade(cls, ref_cls: "MongoBaseMixin", field: str, specs: Specs[SpecDocumentType]) -> None:
        """Apply a cascading delete (does not emit signals)"""
        ids = [to_refs(getattr(f, field)) for f in specs if hasattr(f, field)]
        ref_cls.get_collection().delete_many({"_id": {"$in": ids}})

    @classmethod
    def nullify(cls, ref_cls: "MongoBaseMixin", field: str, specs: Specs[SpecDocumentType]) -> None:
        """Nullify a reference field (does not emit signals)"""
        ids = [to_refs(f) for f in specs]
        ref_cls.get_collection().update_many({field: {"$in": ids}}, {"$set": {field: None}})

    @classmethod
    def pull(cls, ref_cls: "MongoBaseMixin", field: str, specs: Specs[SpecDocumentType]) -> None:
        """Pull references from a list field (does not emit signals)"""
        ids = [to_refs(f) for f in specs]
        ref_cls.get_collection().update_many({field: {"$in": ids}}, {"$pull": {field: {"$in": ids}}})