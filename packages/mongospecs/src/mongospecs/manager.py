"""The manager performs every database operation for one model on one registry.

``Document`` class methods (``Article.find``, ``Article.update`` ...) delegate to
the model's default manager. ``Article.using(db="tenant_a")`` returns another
manager for the same model.

This is the single place where filters are scoped and commands are rewritten by
plugins, so a scope (soft delete, tenant, discriminator) cannot be missed on
one code path.
"""

from __future__ import annotations

import typing as t

from pymongo import ReturnDocument

from mongospecs import tracking
from mongospecs.errors import (
    DocumentNotFound,
    DuplicateKeyError,
    OperationFailed,
    QueryError,
    StaleDocumentError,
)
from mongospecs.expr import Expr, Trusted, and_filters, compile_filter
from mongospecs.hooks import OpCtx, Plugin
from mongospecs.markers import CreatedAt, DeletedAt, Revision, UpdatedAt
from mongospecs.meta import ModelMeta, meta_of
from mongospecs.registry import Registry, Tx, driver_session, op_scope
from mongospecs.updates import UpdateOp, compile_update

if t.TYPE_CHECKING:
    from pymongo.asynchronous.collection import AsyncCollection

    from mongospecs.query import Query

__all__ = ["BulkResult", "Manager"]

T = t.TypeVar("T")

_MANAGED_MARKERS = (CreatedAt, UpdatedAt, DeletedAt, Revision)
# find() option name -> aggregate() option name
_AGG_NAMES = {"max_time_ms": "maxTimeMS", "batch_size": "batchSize", "allow_disk_use": "allowDiskUse"}


class BulkResult(t.Generic[T]):
    """Outcome of ``insert_many``: the documents that were written and per-item failures."""

    __slots__ = ("errors", "inserted")

    def __init__(self, inserted: list[T], errors: list[tuple[int, Exception]]) -> None:
        self.inserted = inserted
        self.errors = errors
        """``(input index, error)`` for every document that was not written."""

    def __len__(self) -> int:
        return len(self.inserted)

    def __iter__(self) -> t.Iterator[T]:
        return iter(self.inserted)

    def __repr__(self) -> str:
        return f"BulkResult(inserted={len(self.inserted)}, errors={len(self.errors)})"


class Manager(t.Generic[T]):
    def __init__(
        self,
        model: type[T],
        registry: Registry,
        *,
        db: str | None = None,
        collection_options: t.Mapping[str, t.Any] | None = None,
    ) -> None:
        self.model = model
        self.meta: ModelMeta = meta_of(model)
        self.registry = registry
        self.db_name = db
        self._collection_options = dict(collection_options or {})
        self.plugins: tuple[Plugin, ...] = (*registry.plugins, *self.meta.plugins)
        for plugin in registry.plugins:
            plugin.setup(self.meta)
        self.managed_attrs: tuple[str, ...] = (
            "id",
            *(f.name for f in self.meta.fields.values() if any(f.has(m) for m in _MANAGED_MARKERS)),
        )
        self._collection: AsyncCollection[dict[str, t.Any]] | None = None
        self._raw_collection: t.Any = None
        self._cascades: list[t.Any] | None = None

    # -- plumbing --------------------------------------------------------------

    @property
    def collection(self) -> AsyncCollection[dict[str, t.Any]]:
        """The raw driver collection (with the registry's codec options)."""
        if self._collection is None or not self.registry.is_open:
            cfg = self.meta.config
            options = {
                "read_preference": cfg.read_preference,
                "read_concern": cfg.read_concern,
                "write_concern": cfg.write_concern,
                **self._collection_options,
            }
            options = {k: v for k, v in options.items() if v is not None}
            self._collection = self.registry.database(self.db_name).get_collection(
                self.meta.collection, **options
            )
        return self._collection

    @property
    def raw_collection(self) -> t.Any:
        """The collection, configured to hand back undecoded BSON (used for tracked reads)."""
        if self._raw_collection is None or not self.registry.is_open:
            from bson.raw_bson import RawBSONDocument

            options = self.registry._codec_options.with_options(document_class=RawBSONDocument)
            self._raw_collection = self.collection.with_options(codec_options=options)
        return self._raw_collection

    def using(
        self,
        *,
        db: str | None = None,
        read_preference: t.Any = None,
        read_concern: t.Any = None,
        write_concern: t.Any = None,
    ) -> Manager[T]:
        """A manager for the same model on another database or with other driver options."""
        options = {
            **self._collection_options,
            **{
                k: v
                for k, v in (
                    ("read_preference", read_preference),
                    ("read_concern", read_concern),
                    ("write_concern", write_concern),
                )
                if v is not None
            },
        }
        return Manager(self.model, self.registry, db=db or self.db_name, collection_options=options)

    def _op(self, op: str) -> op_scope:
        return op_scope(self.registry, self.model.__name__, op)

    def ctx(self, op: str, session: t.Any = None, *, many: bool = False, **extra: t.Any) -> OpCtx:
        return OpCtx(self.meta, op, session, self, many, extra)

    def _kw(self, session: t.Any, **extra: t.Any) -> dict[str, t.Any]:
        kw = {k: v for k, v in extra.items() if v is not None}
        s = driver_session(session)
        if s is not None:
            kw["session"] = s
        return kw

    def _read_kw(self, session: t.Any, options: t.Mapping[str, t.Any] | None = None) -> dict[str, t.Any]:
        kw = self._kw(session, **(options or {}))
        if "max_time_ms" not in kw:
            default = self.meta.config.max_time_ms or self.registry.max_time_ms
            if default is not None:
                kw["max_time_ms"] = default
        if "collation" not in kw and self.meta.config.collation is not None:
            kw["collation"] = self.meta.config.collation
        return kw

    def _agg_kw(self, session: t.Any, options: t.Mapping[str, t.Any] | None = None) -> dict[str, t.Any]:
        """Read options spelled the way ``aggregate`` and ``count_documents`` expect them."""
        kw = self._read_kw(session, options)
        return {_AGG_NAMES.get(k, k): v for k, v in kw.items() if k != "no_cursor_timeout"}

    def scoped(
        self,
        ctx: OpCtx,
        flt: t.Mapping[str, t.Any] | None,
        unscoped: t.Collection[str] | bool = False,
    ) -> dict[str, t.Any]:
        """AND the discriminator filter and every plugin scope into ``flt``."""
        parts: list[t.Mapping[str, t.Any] | None] = [flt, self.meta.discriminator_filter()]
        if unscoped is not True:
            skip = unscoped or ()
            for plugin in self.plugins:
                if plugin.name and plugin.name in skip:
                    continue
                parts.append(plugin.on_filter(ctx, dict(flt or {})))
        return and_filters(*parts)

    def compile(self, target: t.Any) -> dict[str, t.Any]:
        """Compile an id, an expression or an untrusted mapping to a filter."""
        if target is None:
            return {}
        if isinstance(target, Expr | t.Mapping):
            return compile_filter(target, self.meta)
        return {"_id": self.meta.cast("id", target)}

    def default_projection(self) -> dict[str, int] | None:
        return {key: 0 for key in self.meta.deferred} or None

    def _shard_filter(self, snapshot: t.Mapping[str, t.Any]) -> dict[str, t.Any]:
        out: dict[str, t.Any] = {}
        for name in self.meta.config.shard_key:
            stored = self.meta.stored_path(name)
            if stored != "_id" and stored in snapshot:
                out[stored] = snapshot[stored]
        return out

    # -- materialising ---------------------------------------------------------

    def hydrate(self, raw: t.Mapping[str, t.Any], *, partial: frozenset[str] | None = None) -> T:
        """Build a tracked document from a raw MongoDB document."""
        doc = self.meta.decode(raw)
        tracking.track(doc, meta_of(type(doc)).encode(doc), self, partial)
        return t.cast(T, doc)

    def _is_tracked_here(self, doc: t.Any) -> tracking.DocState | None:
        return tracking.state_of(doc)

    # -- reads -------------------------------------------------------------------

    def find(self, *conditions: t.Any, session: t.Any = None) -> Query[T]:
        from mongospecs.query import Query

        return Query(self, conditions=tuple(c for c in conditions if c is not None), session=session)

    async def get(self, id: t.Any, *, session: t.Any = None) -> T | None:
        """The document with this id, or ``None``."""
        return await self.find(Trusted({"_id": self.meta.cast("id", id)}), session=session).first()

    async def count(self, *conditions: t.Any, session: t.Any = None) -> int:
        return await self.find(*conditions, session=session).count()

    async def exists(self, *conditions: t.Any, session: t.Any = None) -> bool:
        return await self.find(*conditions, session=session).exists()

    async def estimated_count(self) -> int:
        """Fast collection-level count from metadata. Ignores every scope and filter."""
        with self._op("estimated_count"):
            return int(await self.collection.estimated_document_count())

    # -- document hooks ----------------------------------------------------------

    async def _hooks(self, when: str, events: tuple[str, ...], ctx: OpCtx, doc: t.Any) -> None:
        meta = meta_of(type(doc))
        for event in events:
            for plugin in self.plugins:
                await (plugin.before if when == "before" else plugin.after)(ctx, event, doc)
            for hook in meta.hooks(when, event):
                await hook(ctx, doc)

    # -- document-level writes -----------------------------------------------------

    async def insert(self, doc: T, *, session: t.Any = None) -> T:
        ctx = self.ctx("insert", session, doc=doc)
        await self._hooks("before", ("insert", "save"), ctx, doc)
        if isinstance(session, Tx):
            session.record(doc, self.managed_attrs)
        encoded = self.meta.encode(doc)
        if encoded.get("_id") is None:
            encoded.pop("_id", None)
        for plugin in self.plugins:
            plugin.on_insert(ctx, encoded)
        with self._op("insert"):
            result = await self.collection.insert_one(encoded, **self._kw(session))
        encoded["_id"] = result.inserted_id
        t.cast(t.Any, doc).id = result.inserted_id
        for plugin in self.plugins:
            plugin.applied(ctx, doc, encoded)
        tracking.track(doc, encoded, self)
        await self._hooks("after", ("insert", "save"), ctx, doc)
        return doc

    def _delta(self, doc: t.Any, state: tracking.DocState) -> tuple[dict[str, t.Any], dict[str, t.Any]]:
        """``(current encoding, update document)`` for a tracked document."""
        current = self.meta.encode(doc)
        to_set, to_unset = tracking.diff(state.snapshot, current, state.partial)
        for name in self.meta.immutable:
            stored = self.meta.fields[name].stored
            if stored in to_set or stored in to_unset or any(k.startswith(stored + ".") for k in to_set):
                raise QueryError(f"{self.meta.model.__name__}.{name} is immutable")
        update: dict[str, t.Any] = {}
        if to_set:
            update["$set"] = to_set
        if to_unset:
            update["$unset"] = {key: "" for key in to_unset}
        return current, update

    async def save(self, doc: T, *, session: t.Any = None) -> T:
        """Insert a new document, or send only what changed on a tracked one."""
        state = tracking.state_of(doc)
        if state is None:
            return await self.insert(doc, session=session)
        ctx = self.ctx("update", session, doc=doc, snapshot=state.snapshot)
        await self._hooks("before", ("save",), ctx, doc)
        current, update = self._delta(doc, state)
        if update:
            flt: dict[str, t.Any] = {"_id": state.snapshot["_id"], **self._shard_filter(state.snapshot)}
            user_keys = _touched(update)
            for plugin in self.plugins:
                plugin.on_update(ctx, flt, update)
            with self._op("save"):
                result = await self.collection.update_one(flt, update, **self._kw(session))
            if result.matched_count == 0:
                await self._raise_unmatched(flt, session)
            if isinstance(session, Tx):
                session.record(doc, self.managed_attrs)
            _patch(current, update, skip=user_keys)
            for plugin in self.plugins:
                plugin.applied(ctx, doc, current)
            tracking.track(doc, current, state.manager, state.partial)
        await self._hooks("after", ("save",), ctx, doc)
        return doc

    async def _raise_unmatched(self, flt: dict[str, t.Any], session: t.Any) -> t.NoReturn:
        with self._op("save"):
            exists = await self.collection.count_documents({"_id": flt["_id"]}, limit=1, **self._kw(session))
        if exists and len(flt) > 1:
            raise StaleDocumentError(
                f"{self.model.__name__} {flt['_id']!r} was changed by someone else since it was loaded"
            )
        raise DocumentNotFound(f"{self.model.__name__} {flt['_id']!r} does not exist")

    async def delete_document(self, doc: T, *, session: t.Any = None, hard: bool = False) -> None:
        any_doc = t.cast(t.Any, doc)
        if any_doc.id is None:
            raise QueryError("cannot delete a document that has no id")
        state = tracking.state_of(doc)
        snapshot = state.snapshot if state is not None else {"_id": self.meta.cast("id", any_doc.id)}
        ctx = self.ctx("delete", session, doc=doc, snapshot=snapshot)
        await self._hooks("before", ("delete",), ctx, doc)
        flt: dict[str, t.Any] = {"_id": snapshot["_id"], **self._shard_filter(snapshot)}
        soft = None if hard else self._soft_delete(ctx, flt)

        async def run(s: t.Any) -> None:
            if isinstance(s, Tx):
                s.record(doc, self.managed_attrs)
            if soft is not None:
                with self._op("delete"):
                    result = await self.collection.update_one(flt, soft, **self._kw(s))
                matched = result.matched_count
            else:
                await self._cascade([snapshot["_id"]], s)
                with self._op("delete"):
                    matched = (await self.collection.delete_one(flt, **self._kw(s))).deleted_count
            if matched == 0:
                await self._raise_unmatched(flt, s)

        if soft is None and session is None and self._cascade_rules():
            await self.registry.transaction(run)
        else:
            await run(session)

        if soft is not None:
            if state is not None:
                current = dict(state.snapshot)
                _patch(current, soft, skip=frozenset())
                for plugin in self.plugins:
                    plugin.applied(ctx, doc, current)
                tracking.track(doc, current, state.manager, state.partial)
        else:
            tracking.forget(doc)
        await self._hooks("after", ("delete",), ctx, doc)

    def _soft_delete(self, ctx: OpCtx, flt: dict[str, t.Any]) -> dict[str, t.Any] | None:
        for plugin in self.plugins:
            update = plugin.on_delete(ctx, flt)
            if update is not None:
                for other in self.plugins:
                    other.on_update(ctx, flt, update)
                return update
        return None

    async def reload(self, doc: T, *, session: t.Any = None) -> T:
        """Refresh a document in place from the database and reset its snapshot."""
        any_doc = t.cast(t.Any, doc)
        with self._op("reload"):
            raw = await self.collection.find_one(
                {"_id": self.meta.cast("id", any_doc.id)}, **self._kw(session)
            )
        if raw is None:
            raise DocumentNotFound(f"{self.model.__name__} {any_doc.id!r} does not exist")
        fresh = self.meta.decode(raw)
        for name in meta_of(type(doc)).fields:
            setattr(doc, name, getattr(fresh, name))
        tracking.track(doc, meta_of(type(doc)).encode(doc), self)
        return doc

    # -- filter-based writes ---------------------------------------------------------

    def _update_doc(
        self,
        ctx: OpCtx,
        flt: dict[str, t.Any],
        ops: t.Iterable[UpdateOp],
        fields: t.Mapping[str, t.Any],
        *,
        upsert: bool = False,
    ) -> dict[str, t.Any]:
        update = compile_update(self.meta, ops, fields)
        if not update:
            raise QueryError("an update needs at least one operator or field")
        if upsert:
            ctx.extra["upsert"] = True
            if self.meta.disc_key:
                update.setdefault("$setOnInsert", {})[self.meta.disc_key] = self.meta.disc_value
            # Give the inserted document the model's defaults (Mongoose: setDefaultsOnInsert),
            # except where the update or the filter already decides the value.
            taken = {key.partition(".")[0] for body in update.values() for key in body} | _filter_keys(flt)
            for stored, value in self.meta.insert_defaults().items():
                if stored not in taken:
                    update.setdefault("$setOnInsert", {})[stored] = value
        for plugin in self.plugins:
            plugin.on_update(ctx, flt, update)
        return update

    async def update(
        self,
        target: t.Any,
        *ops: UpdateOp,
        session: t.Any = None,
        upsert: bool = False,
        returning: t.Literal["after", "before"] = "after",
        if_rev: int | None = None,
        unscoped: t.Collection[str] | bool = False,
        **fields: t.Any,
    ) -> T:
        """Atomically update one document (by id or filter) and return it.

        Raises :class:`DocumentNotFound` when nothing matched, or
        :class:`StaleDocumentError` when ``if_rev`` was given and did not match.
        """
        ctx = self.ctx("update", session)
        flt = self.scoped(ctx, self.compile(target), unscoped)
        if if_rev is not None:
            ctx.extra["if_rev"] = if_rev
        update = self._update_doc(ctx, flt, ops, fields, upsert=upsert)
        with self._op("update"):
            raw = await self.collection.find_one_and_update(
                flt,
                update,
                return_document=ReturnDocument.AFTER if returning == "after" else ReturnDocument.BEFORE,
                upsert=upsert,
                projection=self.default_projection(),
                **self._kw(session),
            )
        if raw is None:
            if upsert and returning == "before":
                raise DocumentNotFound(
                    f"{self.model.__name__}: upsert inserted a new document (no prior version)"
                )
            if if_rev is not None:
                with self._op("update"):
                    base = self.scoped(ctx, self.compile(target), unscoped)
                    exists = await self.collection.count_documents(base, limit=1, **self._kw(session))
                if exists:
                    raise StaleDocumentError(f"{self.model.__name__}: revision {if_rev} is no longer current")
            raise DocumentNotFound(f"{self.model.__name__}: no document matched the update")
        partial = self._default_partial()
        return self.hydrate(raw, partial=partial)

    def _default_partial(self) -> frozenset[str] | None:
        return (self.meta.stored_keys - set(self.meta.deferred)) if self.meta.deferred else None

    async def upsert(self, target: t.Any, *ops: UpdateOp, session: t.Any = None, **fields: t.Any) -> T:
        """Update the matching document or insert one, in a single round trip."""
        return await self.update(target, *ops, session=session, upsert=True, **fields)

    async def update_many(
        self,
        flt: dict[str, t.Any],
        ops: t.Iterable[UpdateOp],
        fields: t.Mapping[str, t.Any],
        *,
        session: t.Any = None,
        unscoped: t.Collection[str] | bool = False,
    ) -> int:
        ctx = self.ctx("update", session, many=True)
        flt = self.scoped(ctx, flt, unscoped)
        update = self._update_doc(ctx, flt, ops, fields)
        with self._op("update_many"):
            result = await self.collection.update_many(flt, update, **self._kw(session))
        return int(result.modified_count)

    async def delete_many(
        self,
        flt: dict[str, t.Any],
        *,
        session: t.Any = None,
        hard: bool = False,
        unscoped: t.Collection[str] | bool = False,
    ) -> int:
        ctx = self.ctx("delete", session, many=True)
        flt = self.scoped(ctx, flt, unscoped)
        soft = None if hard else self._soft_delete(ctx, flt)
        if soft is not None:
            with self._op("delete_many"):
                return int((await self.collection.update_many(flt, soft, **self._kw(session))).modified_count)
        if not self._cascade_rules():
            with self._op("delete_many"):
                return int((await self.collection.delete_many(flt, **self._kw(session))).deleted_count)

        async def run(s: t.Any) -> int:
            with self._op("delete_many"):
                ids = await self.collection.distinct("_id", flt, **self._kw(s))
            if not ids:
                return 0
            await self._cascade(ids, s)
            with self._op("delete_many"):
                return int(
                    (await self.collection.delete_many({"_id": {"$in": ids}}, **self._kw(s))).deleted_count
                )

        return await (self.registry.transaction(run) if session is None else run(session))

    # -- cascades ----------------------------------------------------------------------

    def _cascade_rules(self) -> list[t.Any]:
        if self._cascades is None:
            from mongospecs.relations import cascade_rules

            self._cascades = cascade_rules(self)
        return self._cascades

    async def _cascade(self, ids: list[t.Any], session: t.Any) -> None:
        rules = self._cascade_rules()
        if rules:
            from mongospecs.relations import apply_cascades

            await apply_cascades(self, rules, ids, session)

    # -- bulk -----------------------------------------------------------------------------

    async def insert_many(
        self,
        docs: t.Iterable[T | t.Mapping[str, t.Any]],
        *,
        ordered: bool = True,
        session: t.Any = None,
    ) -> BulkResult[T]:
        """Insert documents in one round trip.

        With ``ordered=False`` the server attempts every document; failures are
        reported in ``BulkResult.errors`` instead of raising.
        """
        items: list[t.Any] = [
            self.meta.decode(dict(d), stored=False) if isinstance(d, t.Mapping) else d for d in docs
        ]
        if not items:
            return BulkResult([], [])
        ctx = self.ctx("insert", session, many=True)
        for plugin in self.plugins:
            await plugin.before_bulk(ctx, items)
        encoded: list[dict[str, t.Any]] = []
        for doc in items:
            if isinstance(session, Tx):
                session.record(doc, self.managed_attrs)
            enc = self.meta.encode(doc)
            if enc.get("_id") is None:
                enc.pop("_id", None)
            for plugin in self.plugins:
                plugin.on_insert(ctx, enc)
            encoded.append(enc)
        failed: dict[int, Exception] = {}
        try:
            with self._op("insert_many"):
                await self.collection.insert_many(encoded, ordered=ordered, **self._kw(session))
        except OperationFailed as exc:
            write_errors = (exc.details or {}).get("writeErrors") if isinstance(exc.details, dict) else None
            if ordered or not write_errors:
                raise
            failed = {e["index"]: _write_error(e) for e in write_errors}
        inserted: list[T] = []
        for index, (doc, enc) in enumerate(zip(items, encoded, strict=True)):
            if index in failed:
                continue
            doc.id = enc["_id"]
            for plugin in self.plugins:
                plugin.applied(ctx, doc, enc)
            tracking.track(doc, enc, self)
            inserted.append(doc)
        for plugin in self.plugins:
            await plugin.after_bulk(ctx, inserted)
        return BulkResult(inserted, sorted(failed.items()))

    async def bulk_write(
        self, operations: t.Iterable[t.Any], *, ordered: bool = True, session: t.Any = None
    ) -> t.Any:
        """Run typed bulk operations (see :mod:`mongospecs.bulk`) in one round trip."""
        from mongospecs.bulk import compile_bulk

        ops = list(operations)
        ctx = self.ctx("bulk", session, many=True)
        for plugin in self.plugins:
            await plugin.before_bulk(ctx, ops)
        requests = compile_bulk(self, ctx, ops)
        if not requests:
            return None
        with self._op("bulk_write"):
            result = await self.collection.bulk_write(requests, ordered=ordered, **self._kw(session))
        for plugin in self.plugins:
            await plugin.after_bulk(ctx, ops)
        return result

    async def bulk_save(self, docs: t.Iterable[T], *, session: t.Any = None) -> int:
        """Save many documents in one round trip: inserts for new ones, deltas for tracked ones.

        Returns the number of documents written. Raises :class:`StaleDocumentError`
        if any revision-checked update matched nothing (the batch does not say which).
        """
        from pymongo import InsertOne
        from pymongo import UpdateOne as DriverUpdateOne

        requests: list[t.Any] = []
        pending: list[
            tuple[t.Any, str, OpCtx, dict[str, t.Any], tracking.DocState | None, frozenset[str]]
        ] = []
        for doc in docs:
            state = tracking.state_of(doc)
            if state is None:
                ctx = self.ctx("insert", session, many=True, doc=doc)
                enc = self.meta.encode(doc)
                if enc.get("_id") is None:
                    enc.pop("_id", None)
                for plugin in self.plugins:
                    plugin.on_insert(ctx, enc)
                requests.append(InsertOne(enc))
                pending.append((doc, "insert", ctx, enc, None, frozenset()))
                continue
            ctx = self.ctx("update", session, many=True, doc=doc, snapshot=state.snapshot)
            current, update = self._delta(doc, state)
            if not update:
                continue
            flt: dict[str, t.Any] = {"_id": state.snapshot["_id"], **self._shard_filter(state.snapshot)}
            user_keys = _touched(update)
            for plugin in self.plugins:
                plugin.on_update(ctx, flt, update)
            requests.append(DriverUpdateOne(flt, update))
            _patch(current, update, skip=user_keys)
            pending.append((doc, "update", ctx, current, state, user_keys))
        if not requests:
            return 0
        bulk_ctx = self.ctx("bulk", session, many=True)
        all_docs = [p[0] for p in pending]
        for plugin in self.plugins:
            await plugin.before_bulk(bulk_ctx, all_docs)
        with self._op("bulk_save"):
            result = await self.collection.bulk_write(requests, ordered=False, **self._kw(session))
        updates = sum(1 for p in pending if p[1] == "update")
        if result.matched_count != updates:
            raise StaleDocumentError(
                f"{self.model.__name__}: {updates - result.matched_count} of {updates} documents in the batch "
                "no longer exist or were changed since they were loaded"
            )
        for doc, kind, ctx, enc, state, _ in pending:
            if isinstance(session, Tx):
                session.record(doc, self.managed_attrs)
            if kind == "insert":
                doc.id = enc["_id"]
            for plugin in self.plugins:
                plugin.applied(ctx, doc, enc)
            tracking.track(doc, enc, state.manager if state else self, state.partial if state else None)
        for plugin in self.plugins:
            await plugin.after_bulk(bulk_ctx, all_docs)
        return len(pending)

    # -- escape hatches ---------------------------------------------------------------------

    def aggregate(
        self,
        pipeline: t.Sequence[t.Mapping[str, t.Any]],
        *,
        as_: t.Any = None,
        session: t.Any = None,
        unscoped: t.Collection[str] | bool = False,
        **options: t.Any,
    ) -> Aggregate[t.Any]:
        """Run a raw aggregation pipeline.

        Scopes still apply: the scope filter is prepended as a ``$match`` and
        plugins may rewrite the pipeline. ``as_`` decodes each result into a
        model class; by default results are plain dicts.
        """
        return Aggregate(self, list(pipeline), as_, session, unscoped, options)

    def watch(self, pipeline: t.Sequence[t.Mapping[str, t.Any]] | None = None, **options: t.Any) -> t.Any:
        from mongospecs.streams import ChangeStream

        return ChangeStream(self, list(pipeline or []), options)

    def __repr__(self) -> str:
        return f"<Manager {self.model.__name__} db={self.db_name or 'default'}>"


class Aggregate(t.Generic[T]):
    """Result of ``aggregate()``: await it for a list, or iterate it with ``async for``."""

    def __init__(
        self,
        manager: Manager[t.Any],
        pipeline: list[t.Mapping[str, t.Any]],
        as_: t.Any,
        session: t.Any,
        unscoped: t.Collection[str] | bool,
        options: dict[str, t.Any],
    ) -> None:
        self._manager = manager
        self._pipeline = pipeline
        self._as = as_
        self._session = session
        self._unscoped = unscoped
        self._options = options

    def compiled(self) -> list[dict[str, t.Any]]:
        """The pipeline that will be sent, after scopes and plugin rewriting."""
        manager = self._manager
        ctx = manager.ctx("aggregate", self._session, many=True)
        pipeline = [dict(stage) for stage in self._pipeline]
        scope = manager.scoped(ctx, None, self._unscoped)
        if scope:
            # Stages that must come first stay first.
            first = next(iter(pipeline[0]), "") if pipeline else ""
            at = 1 if first in ("$geoNear", "$search", "$searchMeta", "$vectorSearch", "$collStats") else 0
            pipeline.insert(at, {"$match": scope})
        if self._unscoped is not True:
            for plugin in manager.plugins:
                pipeline = plugin.on_pipeline(ctx, pipeline)
        return pipeline

    def _decoder(self) -> t.Callable[[dict[str, t.Any]], t.Any]:
        as_ = self._as
        if as_ is None:
            return lambda doc: doc
        from mongospecs.codec import codec_for
        from mongospecs.model import Document

        if isinstance(as_, type) and issubclass(as_, Document):
            return meta_of(as_).decode
        codec = codec_for(as_)
        return lambda doc: codec.decode(as_, doc)

    async def __aiter__(self) -> t.AsyncIterator[T]:
        manager = self._manager
        decode = self._decoder()
        with manager._op("aggregate"):
            cursor = await manager.collection.aggregate(
                self.compiled(), **manager._agg_kw(self._session, self._options)
            )
        async with cursor:
            while True:
                with manager._op("aggregate"):
                    batch = await cursor.to_list(1000)
                for raw in batch:
                    yield decode(raw)
                if len(batch) < 1000:
                    return

    async def all(self) -> list[T]:
        return [item async for item in self]

    def __await__(self) -> t.Generator[t.Any, None, list[T]]:
        return self.all().__await__()


def _touched(update: t.Mapping[str, t.Any]) -> frozenset[str]:
    return frozenset(k for body in update.values() for k in body)


def _patch(doc: dict[str, t.Any], update: t.Mapping[str, t.Any], *, skip: frozenset[str]) -> None:
    """Apply plugin-written top-level ``$set``/``$inc`` values to an encoded document."""
    for key, value in update.get("$set", {}).items():
        if key not in skip and "." not in key:
            doc[key] = value
    for key, value in update.get("$inc", {}).items():
        if key not in skip and "." not in key:
            doc[key] = (doc.get(key) or 0) + value


def _write_error(error: t.Mapping[str, t.Any]) -> Exception:
    message = str(error.get("errmsg", "write failed"))
    if error.get("code") == 11000:
        fields = error.get("keyValue")
        return DuplicateKeyError(
            message,
            code=11000,
            details=dict(error),
            fields=dict(fields) if isinstance(fields, dict) else None,
        )
    return OperationFailed(message, code=error.get("code"), details=dict(error))


def _filter_keys(flt: t.Any) -> set[str]:
    """Top-level field names a filter mentions, looking through $and/$or/$nor."""
    keys: set[str] = set()
    if isinstance(flt, dict):
        for key, value in flt.items():
            if key in ("$and", "$or", "$nor") and isinstance(value, list):
                for part in value:
                    keys |= _filter_keys(part)
            elif not key.startswith("$"):
                keys.add(key.partition(".")[0])
    return keys
