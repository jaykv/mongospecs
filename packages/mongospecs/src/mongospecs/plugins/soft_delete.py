"""``DeletedAt``: soft delete.

``delete`` sets the marked field instead of removing the document, and every
read, count, update and delete excludes documents where it is set. Use
``.unscoped("soft_delete")`` to see them, ``hard=True`` to really delete, and
``Model.find(...).unscoped("soft_delete").update(deleted_at=None)`` to restore.

A unique index on a soft-deleted model usually wants
``partial={"<deleted field>": None}`` so deleted documents do not block reuse.
"""

from __future__ import annotations

import typing as t

from mongospecs.errors import ConfigurationError
from mongospecs.hooks import OpCtx, Plugin
from mongospecs.markers import DeletedAt
from mongospecs.plugins.timestamps import utcnow

if t.TYPE_CHECKING:
    from mongospecs.meta import ModelMeta

__all__ = ["SoftDelete"]


class SoftDelete(Plugin):
    name = "soft_delete"

    def __init__(self, meta: ModelMeta) -> None:
        fields = [f for f in meta.fields.values() if f.has(DeletedAt)]
        if len(fields) != 1:
            raise ConfigurationError(f"{meta.model.__name__}: exactly one field may be marked DeletedAt")
        self.field = fields[0]
        if self.field.required:
            raise ConfigurationError(
                f"{meta.model.__name__}.{self.field.name}: a DeletedAt field needs a None default"
            )

    def on_filter(self, ctx: OpCtx, flt: dict[str, t.Any]) -> dict[str, t.Any] | None:
        if self.field.stored in flt:
            return None  # the caller is filtering on the field explicitly
        return {self.field.stored: None}

    def on_delete(self, ctx: OpCtx, flt: dict[str, t.Any]) -> dict[str, t.Any] | None:
        return {"$set": {self.field.stored: utcnow()}}

    def applied(self, ctx: OpCtx, doc: t.Any, written: dict[str, t.Any]) -> None:
        if ctx.op == "delete" and self.field.stored in written:
            setattr(doc, self.field.name, written[self.field.stored])
