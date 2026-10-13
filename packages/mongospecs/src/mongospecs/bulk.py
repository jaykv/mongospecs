"""Typed operations for ``Model.bulk_write``.

Unlike raw driver operations these are checked against the model: filters and
values are cast, unknown fields raise, scopes apply, and plugins add timestamps
and revisions exactly as they do for single writes.
"""

from __future__ import annotations

import typing as t

import pymongo

from mongospecs.errors import QueryError
from mongospecs.hooks import OpCtx
from mongospecs.updates import UpdateOp

if t.TYPE_CHECKING:
    from mongospecs.manager import Manager

__all__ = ["DeleteMany", "DeleteOne", "InsertOne", "UpdateMany", "UpdateOne", "compile_bulk"]


class InsertOne:
    __slots__ = ("doc",)

    def __init__(self, doc: t.Any) -> None:
        self.doc = doc


class UpdateOne:
    many = False
    __slots__ = ("fields", "ops", "target", "upsert")

    def __init__(self, target: t.Any, *ops: UpdateOp, upsert: bool = False, **fields: t.Any) -> None:
        self.target = target
        self.ops = ops
        self.fields = fields
        self.upsert = upsert


class UpdateMany(UpdateOne):
    many = True
    __slots__ = ()


class DeleteOne:
    many = False
    __slots__ = ("hard", "target")

    def __init__(self, target: t.Any, *, hard: bool = False) -> None:
        self.target = target
        self.hard = hard


class DeleteMany(DeleteOne):
    many = True
    __slots__ = ()


def compile_bulk(
    manager: Manager[t.Any], ctx: OpCtx, operations: t.Iterable[t.Any]
) -> tuple[list[t.Any], dict[int, tuple[dict[str, t.Any], bool]]]:
    """Compile typed bulk operations to driver requests.

    Also returns the hard deletes as ``{request index: (filter, many)}`` so the caller
    can run cascade rules for them.
    """
    requests: list[t.Any] = []
    deletes: dict[int, tuple[dict[str, t.Any], bool]] = {}
    for op in operations:
        if isinstance(op, InsertOne):
            doc = manager.meta.decode(dict(op.doc), stored=False) if isinstance(op.doc, t.Mapping) else op.doc
            encoded = manager.meta.encode(doc)
            if encoded.get("_id") is None:
                encoded.pop("_id", None)
            insert_ctx = manager.ctx("insert", ctx.session, many=True)
            for plugin in manager.plugins:
                plugin.on_insert(insert_ctx, encoded)
            requests.append(pymongo.InsertOne(encoded))
        elif isinstance(op, UpdateOne):
            update_ctx = manager.ctx("update", ctx.session, many=op.many)
            flt = manager.scoped(update_ctx, manager.compile(op.target))
            update = manager._update_doc(update_ctx, flt, op.ops, op.fields, upsert=op.upsert)
            factory = pymongo.UpdateMany if op.many else pymongo.UpdateOne
            requests.append(factory(flt, update, upsert=op.upsert))
        elif isinstance(op, DeleteOne):
            delete_ctx = manager.ctx("delete", ctx.session, many=op.many)
            flt = manager.scoped(delete_ctx, manager.compile(op.target))
            soft = None if op.hard else manager._soft_delete(delete_ctx, flt)
            if soft is not None:
                requests.append((pymongo.UpdateMany if op.many else pymongo.UpdateOne)(flt, soft))
            else:
                deletes[len(requests)] = (flt, op.many)
                requests.append((pymongo.DeleteMany if op.many else pymongo.DeleteOne)(flt))
        else:
            raise QueryError(
                f"bulk_write takes mongospecs.bulk operations, got {type(op).__name__}; "
                "use Model.collection().bulk_write for raw driver operations"
            )
    return requests, deletes
