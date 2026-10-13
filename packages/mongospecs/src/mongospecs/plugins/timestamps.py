"""``CreatedAt`` / ``UpdatedAt``: timestamps on every write path, including bulk and filter-based ones."""

from __future__ import annotations

import datetime as dt
import typing as t

from mongospecs.hooks import OpCtx, Plugin
from mongospecs.markers import CreatedAt, UpdatedAt

if t.TYPE_CHECKING:
    from mongospecs.meta import ModelMeta

__all__ = ["Timestamps", "utcnow"]


def utcnow() -> dt.datetime:
    """Now, in UTC, at the millisecond precision BSON stores."""
    now = dt.datetime.now(dt.UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


class Timestamps(Plugin):
    name = "timestamps"

    def __init__(self, meta: ModelMeta) -> None:
        self.created = [f for f in meta.fields.values() if f.has(CreatedAt)]
        self.updated = [f for f in meta.fields.values() if f.has(UpdatedAt)]
        self._attrs = {f.stored: f.name for f in (*self.created, *self.updated)}

    def on_insert(self, ctx: OpCtx, doc: dict[str, t.Any]) -> None:
        now = utcnow()
        for f in (*self.created, *self.updated):
            if doc.get(f.stored) is None:
                doc[f.stored] = now

    def on_update(self, ctx: OpCtx, flt: dict[str, t.Any], update: dict[str, t.Any]) -> None:
        now = utcnow()
        touched = {key for body in update.values() for key in body}
        for f in self.updated:
            if f.stored not in touched:
                update.setdefault("$set", {})[f.stored] = now
        if ctx.extra.get("upsert"):
            for f in self.created:
                if f.stored not in touched:
                    update.setdefault("$setOnInsert", {})[f.stored] = now

    def applied(self, ctx: OpCtx, doc: t.Any, written: dict[str, t.Any]) -> None:
        for stored, name in self._attrs.items():
            if stored in written:
                setattr(doc, name, written[stored])
