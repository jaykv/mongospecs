"""``Document``: the active-record API shared by every backend's ``Spec`` base class.

The mixin has no fields and no state. Class methods delegate to the model's
default :class:`~mongospecs.manager.Manager`; instance methods delegate to the
manager the document was loaded through.
"""

from __future__ import annotations

import typing as t

from mongospecs import codec as _codec_module
from mongospecs import tracking
from mongospecs.codec import Codec

if t.TYPE_CHECKING:
    from pymongo.asynchronous.collection import AsyncCollection

    from mongospecs.manager import Aggregate, BulkResult, Manager
    from mongospecs.markers import SpecConfig
    from mongospecs.query import Query
    from mongospecs.updates import UpdateOp

__all__ = ["Document"]

T = t.TypeVar("T", bound="Document")
F = t.TypeVar("F", bound=t.Callable[..., t.Any])


class Document:
    """Mixin giving a model class its database API."""

    __slots__ = ()

    if t.TYPE_CHECKING:
        __codec__: t.ClassVar[Codec]
        __spec__: t.ClassVar[SpecConfig]
        id: t.Any

    # -- managers --------------------------------------------------------------

    @classmethod
    def _manager(cls: type[T]) -> Manager[T]:
        from mongospecs.registry import registry_of

        return registry_of(cls).manager(cls)

    @classmethod
    def using(
        cls: type[T],
        *,
        db: str | None = None,
        registry: t.Any = None,
        read_preference: t.Any = None,
        read_concern: t.Any = None,
        write_concern: t.Any = None,
    ) -> Manager[T]:
        """A manager bound to another database, registry or set of driver options.

        ``Article.using(db="tenant_a").find(...)``. Documents loaded through it
        are saved back through it.
        """
        base: Manager[T] = registry.manager(cls) if registry is not None else cls._manager()
        return base.using(
            db=db, read_preference=read_preference, read_concern=read_concern, write_concern=write_concern
        )

    @classmethod
    def collection(cls) -> AsyncCollection[dict[str, t.Any]]:
        """The raw driver collection, for anything mongospecs does not model."""
        return cls._manager().collection

    # -- reads -----------------------------------------------------------------

    @classmethod
    def find(cls: type[T], *conditions: t.Any, session: t.Any = None) -> Query[T]:
        """Start a query. Conditions are ANDed: ``Article.find(Q.views > 10, Q.title.prefix("a"))``."""
        return cls._manager().find(*conditions, session=session)

    @classmethod
    async def get(cls: type[T], id: t.Any, *, session: t.Any = None) -> T | None:
        """The document with this id, or ``None``."""
        return await cls._manager().get(id, session=session)

    @classmethod
    async def count(cls, *conditions: t.Any, session: t.Any = None) -> int:
        """Exact count of matching documents (scopes apply)."""
        return await cls._manager().count(*conditions, session=session)

    @classmethod
    async def estimated_count(cls) -> int:
        """Fast count of the whole collection from metadata; ignores scopes and filters."""
        return await cls._manager().estimated_count()

    @classmethod
    async def exists(cls, *conditions: t.Any, session: t.Any = None) -> bool:
        return await cls._manager().exists(*conditions, session=session)

    @classmethod
    def hydrate(cls: type[T], raw: t.Mapping[str, t.Any]) -> T:
        """Build a tracked document from a raw MongoDB document you already have."""
        return cls._manager().hydrate(raw)

    # -- writes ----------------------------------------------------------------

    @classmethod
    async def update(
        cls: type[T],
        target: t.Any,
        *ops: UpdateOp,
        session: t.Any = None,
        if_rev: int | None = None,
        returning: t.Literal["after", "before"] = "after",
        **fields: t.Any,
    ) -> T:
        """Atomically update one document, found by id or filter, and return it.

        ``await Article.update(article_id, Inc(views=1), title="New")``
        """
        return await cls._manager().update(
            target, *ops, session=session, if_rev=if_rev, returning=returning, **fields
        )

    @classmethod
    async def upsert(
        cls: type[T], target: t.Any, *ops: UpdateOp, session: t.Any = None, **fields: t.Any
    ) -> T:
        """Update the matching document or insert one, in a single round trip."""
        return await cls._manager().upsert(target, *ops, session=session, **fields)

    @classmethod
    async def insert_many(
        cls: type[T],
        docs: t.Iterable[T | t.Mapping[str, t.Any]],
        *,
        ordered: bool = True,
        session: t.Any = None,
    ) -> BulkResult[T]:
        return await cls._manager().insert_many(docs, ordered=ordered, session=session)

    @classmethod
    async def bulk_write(
        cls, operations: t.Iterable[t.Any], *, ordered: bool = True, session: t.Any = None
    ) -> t.Any:
        return await cls._manager().bulk_write(operations, ordered=ordered, session=session)

    @classmethod
    async def bulk_save(cls: type[T], docs: t.Iterable[T], *, session: t.Any = None) -> int:
        return await cls._manager().bulk_save(docs, session=session)

    @classmethod
    def aggregate(
        cls,
        pipeline: t.Sequence[t.Mapping[str, t.Any]],
        *,
        as_: t.Any = None,
        session: t.Any = None,
        **options: t.Any,
    ) -> Aggregate[t.Any]:
        return cls._manager().aggregate(pipeline, as_=as_, session=session, **options)

    @classmethod
    def watch(cls, pipeline: t.Sequence[t.Mapping[str, t.Any]] | None = None, **options: t.Any) -> t.Any:
        """Open a change stream: ``async for event in Article.watch(): ...``"""
        return cls._manager().watch(pipeline, **options)

    # -- hooks and scopes ------------------------------------------------------

    @classmethod
    def before(cls, event: t.Literal["insert", "save", "delete"]) -> t.Callable[[F], F]:
        """Register ``async def hook(ctx, doc)`` to run before a document-level operation."""
        from mongospecs.meta import add_hook, check_event

        check_event(event)

        def register(fn: F) -> F:
            add_hook(cls, "before", event, fn)
            return fn

        return register

    @classmethod
    def after(cls, event: t.Literal["insert", "save", "delete"]) -> t.Callable[[F], F]:
        """Register ``async def hook(ctx, doc)`` to run after a document-level operation succeeded."""
        from mongospecs.meta import add_hook, check_event

        check_event(event)

        def register(fn: F) -> F:
            add_hook(cls, "after", event, fn)
            return fn

        return register

    @classmethod
    def scope(cls, fn: F) -> F:
        """Register a named, chainable scope: ``Article.find().published()``.

        The function receives the query (plus any call arguments) and returns a
        query or a condition.
        """
        from mongospecs.meta import add_scope

        add_scope(cls, fn.__name__, fn)
        return fn

    # -- instance API -------------------------------------------------------------

    def _own_manager(self) -> Manager[t.Any]:
        state = tracking.state_of(self)
        if state is not None and state.manager is not None and state.manager.registry.is_open:
            return t.cast("Manager[t.Any]", state.manager)
        return type(self)._manager()

    @property
    def is_new(self) -> bool:
        """True until the document has been inserted or was loaded from the database."""
        return tracking.state_of(self) is None

    def changes(self) -> dict[str, t.Any]:
        """What ``save()`` would send: ``{"$set": {...}, "$unset": {...}}`` (empty when unchanged)."""
        state = tracking.state_of(self)
        if state is None:
            return {"$set": self._own_manager().meta.encode(self)}
        return self._own_manager()._delta(self, state)[1]

    async def insert(self: T, *, session: t.Any = None) -> T:
        return t.cast(T, await self._own_manager().insert(self, session=session))

    async def save(self: T, *, session: t.Any = None) -> T:
        """Insert a new document, or send only the fields that changed since it was loaded."""
        return t.cast(T, await self._own_manager().save(self, session=session))

    async def delete(self, *, session: t.Any = None, hard: bool = False) -> None:
        """Delete this document (a soft delete when the model has a ``DeletedAt`` field, unless ``hard``)."""
        await self._own_manager().delete_document(self, session=session, hard=hard)

    async def reload(self: T, *, session: t.Any = None) -> T:
        """Refresh this document in place from the database."""
        return t.cast(T, await self._own_manager().reload(self, session=session))

    def _normalize_refs(self) -> None:
        """Wrap raw ids and documents assigned to reference fields in ``Ref`` (backends that do not validate on init)."""
        cls = type(self)
        refs = _REF_FIELDS.get(cls)
        if refs is None:
            refs = _REF_FIELDS[cls] = _ref_fields(cls)
        if not refs:
            return
        from mongospecs.refs import Ref

        for name, shape, model in refs:
            value = getattr(self, name)
            if value is None:
                continue
            if shape == "one":
                if type(value) is not Ref:
                    setattr(self, name, Ref(value, model))
                elif value._model is None:
                    value._model = model
            elif shape == "list":
                if any(type(v) is not Ref for v in value):
                    setattr(self, name, [v if type(v) is Ref else Ref(v, model) for v in value])
            elif any(type(v) is not Ref for v in value.values()):
                setattr(self, name, {k: v if type(v) is Ref else Ref(v, model) for k, v in value.items()})


_codec_module._DOCUMENT = Document

_REF_FIELDS: dict[type, tuple[tuple[str, str, t.Any], ...]] = {}


def _ref_fields(cls: type) -> tuple[tuple[str, str, t.Any], ...]:
    from mongospecs.codec import codec_for

    out = []
    for info in codec_for(cls).fields(cls).values():
        if info.ref is not None:
            target = info.ref.targets[0] if len(info.ref.targets) == 1 else None
            out.append((info.name, info.ref.shape, target if isinstance(target, type) else None))
    return tuple(out)
