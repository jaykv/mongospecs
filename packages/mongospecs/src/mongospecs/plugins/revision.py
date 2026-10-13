"""``Revision``: optimistic locking.

Every write increments the marked integer field. Saving or deleting a loaded
document also requires the stored revision to equal the one that was loaded;
otherwise :class:`~mongospecs.errors.StaleDocumentError` is raised.
``Model.update(id, ..., if_rev=n)`` applies the same check to an atomic update.
"""

from __future__ import annotations

import typing as t

from mongospecs.errors import ConfigurationError
from mongospecs.hooks import OpCtx, Plugin
from mongospecs.markers import Revision

if t.TYPE_CHECKING:
    from mongospecs.meta import ModelMeta

__all__ = ["Revisions"]


class Revisions(Plugin):
    name = "revision"

    def __init__(self, meta: ModelMeta) -> None:
        fields = [f for f in meta.fields.values() if f.has(Revision)]
        if len(fields) != 1:
            raise ConfigurationError(f"{meta.model.__name__}: exactly one field may be marked Revision")
        self.field = fields[0]
        if self.field.required:
            raise ConfigurationError(
                f"{meta.model.__name__}.{self.field.name}: a Revision field needs a default of 0"
            )

    def on_insert(self, ctx: OpCtx, doc: dict[str, t.Any]) -> None:
        if doc.get(self.field.stored) is None:
            doc[self.field.stored] = 0

    def on_update(self, ctx: OpCtx, flt: dict[str, t.Any], update: dict[str, t.Any]) -> None:
        stored = self.field.stored
        for op in ("$set", "$unset", "$setOnInsert"):
            body = update.get(op)
            if body and stored in body:
                del body[stored]
                if not body:
                    del update[op]
        update.setdefault("$inc", {})[stored] = 1
        snapshot = ctx.extra.get("snapshot")
        if snapshot is not None:
            flt[stored] = snapshot.get(stored)
        elif ctx.extra.get("if_rev") is not None:
            flt[stored] = ctx.extra["if_rev"]

    def applied(self, ctx: OpCtx, doc: t.Any, written: dict[str, t.Any]) -> None:
        if self.field.stored in written:
            setattr(doc, self.field.name, written[self.field.stored])
