"""Registry: owns the client, binds models, runs transactions.

Lifecycle is explicit. Constructing a registry does no I/O; ``async with``
opens it; closing is final. The client binds to the event loop that first uses
it, so use one registry per loop (in tests: one per test, or one session loop).
"""

from __future__ import annotations

import contextvars
import datetime as dt
import decimal
import enum
import logging
import typing as t
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass

from bson.binary import UuidRepresentation
from bson.codec_options import CodecOptions, TypeCodec, TypeRegistry
from bson.decimal128 import Decimal128
from bson.errors import InvalidDocument
from pymongo import AsyncMongoClient, monitoring
from pymongo import errors as pe

from mongospecs import tracking
from mongospecs.errors import (
    ConfigurationError,
    MongoSpecsError,
    NotBoundError,
    RegistryClosedError,
    TransactionError,
    translate,
    wrap_errors,
)
from mongospecs.hooks import Plugin
from mongospecs.refs import Ref

if t.TYPE_CHECKING:
    from pymongo.asynchronous.client_session import AsyncClientSession
    from pymongo.asynchronous.database import AsyncDatabase

    from mongospecs.manager import Manager

__all__ = [
    "CommandEvent",
    "Registry",
    "Tx",
    "logging_listener",
    "otel_listener",
    "registry_of",
    "sentry_listener",
]

R = t.TypeVar("R")

_BINDINGS: weakref.WeakKeyDictionary[type, Registry] = weakref.WeakKeyDictionary()
_IN_TRANSACTION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mongospecs_in_transaction", default=False
)
_CURRENT_OP: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "mongospecs_current_op", default=None
)


# -- BSON codec options -------------------------------------------------------


class _DecimalCodec(TypeCodec):
    python_type = decimal.Decimal
    bson_type = Decimal128

    def transform_python(self, value: decimal.Decimal) -> Decimal128:
        return Decimal128(value)

    def transform_bson(self, value: Decimal128) -> decimal.Decimal:
        return value.to_decimal()


def _fallback_encoder(value: t.Any) -> t.Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Ref):
        return value.id
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return list(value)
    raise InvalidDocument(f"cannot encode object of type {type(value).__name__} to BSON")


def codec_options(tz_aware: bool = True) -> CodecOptions[dict[str, t.Any]]:
    return CodecOptions(
        tz_aware=tz_aware,
        uuid_representation=UuidRepresentation.STANDARD,
        type_registry=TypeRegistry([_DecimalCodec()], fallback_encoder=_fallback_encoder),
    )


# -- command observability ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandEvent:
    """One driver command, as seen by ``Registry(on_command=...)`` listeners."""

    phase: t.Literal["started", "succeeded", "failed"]
    command_name: str
    database: str
    request_id: int
    model: str | None
    """Name of the model the command was issued for, when it came through mongospecs."""
    op: str | None
    """mongospecs operation: ``find``, ``insert``, ``update`` ..."""
    duration_ms: float | None = None
    command: t.Mapping[str, t.Any] | None = None
    failure: t.Any = None


CommandListener = t.Callable[[CommandEvent], None]


class _Monitor(monitoring.CommandListener):
    def __init__(self, listeners: t.Sequence[CommandListener]) -> None:
        self._listeners = tuple(listeners)

    def _emit(self, event: CommandEvent) -> None:
        for listener in self._listeners:
            listener(event)

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        model, op = _CURRENT_OP.get() or (None, None)
        self._emit(
            CommandEvent(
                "started",
                event.command_name,
                event.database_name,
                event.request_id,
                model,
                op,
                None,
                event.command,
            )
        )

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        model, op = _CURRENT_OP.get() or (None, None)
        self._emit(
            CommandEvent(
                "succeeded",
                event.command_name,
                event.database_name,
                event.request_id,
                model,
                op,
                event.duration_micros / 1000,
            )
        )

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        model, op = _CURRENT_OP.get() or (None, None)
        self._emit(
            CommandEvent(
                "failed",
                event.command_name,
                event.database_name,
                event.request_id,
                model,
                op,
                event.duration_micros / 1000,
                None,
                event.failure,
            )
        )


def logging_listener(logger: logging.Logger | None = None, level: int = logging.DEBUG) -> CommandListener:
    """Log every command (the equivalent of ``mongoose.set('debug', true)``)."""
    log = logger or logging.getLogger("mongospecs.commands")

    def listen(event: CommandEvent) -> None:
        target = f"{event.model}.{event.op}" if event.model else "-"
        if event.phase == "started":
            log.log(level, "%s %s.%s started %s", target, event.database, event.command_name, event.command)
        elif event.phase == "succeeded":
            log.log(
                level, "%s %s.%s ok in %.2fms", target, event.database, event.command_name, event.duration_ms
            )
        else:
            log.log(
                max(level, logging.WARNING),
                "%s %s.%s failed in %.2fms: %s",
                target,
                event.database,
                event.command_name,
                event.duration_ms,
                event.failure,
            )

    return listen


def otel_listener(tracer: t.Any = None) -> CommandListener:
    """Create one OpenTelemetry span per command. Needs ``mongospecs[otel]``."""
    from opentelemetry import trace

    tr = tracer or trace.get_tracer("mongospecs")
    spans: dict[int, t.Any] = {}

    def listen(event: CommandEvent) -> None:
        if event.phase == "started":
            name = f"{event.model}.{event.op}" if event.model else event.command_name
            span = tr.start_span(name, kind=trace.SpanKind.CLIENT)
            span.set_attribute("db.system", "mongodb")
            span.set_attribute("db.name", event.database)
            span.set_attribute("db.operation", event.command_name)
            if event.model:
                span.set_attribute("mongospecs.model", event.model)
            spans[event.request_id] = span
            return
        span = spans.pop(event.request_id, None)
        if span is None:
            return
        if event.phase == "failed":
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(event.failure)))
        span.end()

    return listen


def sentry_listener() -> CommandListener:
    """Record one Sentry span per command, named after the model and operation. Needs ``mongospecs[sentry]``.

    Spans attach to whatever span or transaction is active, so a request traced
    by a Sentry web integration shows its database work as ``Article.find``,
    ``Article.save`` and so on. Works with any Sentry-compatible backend.

    The Sentry SDK's own ``PyMongoIntegration`` records the same commands under
    driver names; disable it to avoid seeing each command twice::

        sentry_sdk.init(dsn=..., disabled_integrations=[PyMongoIntegration()])
    """
    import sentry_sdk

    spans: dict[int, t.Any] = {}

    def listen(event: CommandEvent) -> None:
        if event.phase == "started":
            name = f"{event.model}.{event.op}" if event.model else event.command_name
            span = sentry_sdk.start_span(op="db", name=name, origin="auto.db.mongospecs")
            span.set_data("db.system", "mongodb")
            span.set_data("db.name", event.database)
            span.set_data("db.operation", event.command_name)
            if event.model:
                span.set_data("mongospecs.model", event.model)
                span.set_data("mongospecs.op", event.op)
            spans[event.request_id] = span
            return
        span = spans.pop(event.request_id, None)
        if span is None:
            return
        span.set_status("internal_error" if event.phase == "failed" else "ok")
        span.finish()

    return listen


# -- transactions -------------------------------------------------------------


class Tx:
    """A session passed as ``session=`` to operations.

    Besides carrying the driver session, it journals the in-memory state of
    every document written through it, so that state can be put back if the
    transaction aborts or is retried.
    """

    def __init__(self, session: AsyncClientSession, registry: Registry) -> None:
        self.session = session
        self.registry = registry
        self._journal: list[tuple[t.Any, tracking.DocState | None, dict[str, t.Any]]] = []

    def record(self, doc: t.Any, attrs: t.Iterable[str]) -> None:
        """Remember ``doc`` as it is now. Called before a write changes it."""
        self._journal.append((doc, tracking.state_of(doc), {name: getattr(doc, name) for name in attrs}))

    def rollback_memory(self) -> None:
        """Put journaled documents back to how they were before this transaction touched them."""
        for doc, state, attrs in reversed(self._journal):
            tracking.restore(doc, state)
            for name, value in attrs.items():
                setattr(doc, name, value)
        self._journal.clear()

    @asynccontextmanager
    async def start_transaction(self, **options: t.Any) -> t.AsyncIterator[Tx]:
        """Manual transaction. The block is not re-run on transient errors."""
        try:
            with wrap_errors():
                async with await self.session.start_transaction(**options):
                    yield self
        except BaseException:
            self.rollback_memory()
            raise
        self._journal.clear()

    @property
    def in_transaction(self) -> bool:
        return bool(self.session.in_transaction)


def driver_session(session: t.Any) -> t.Any:
    """Unwrap a ``Tx`` to the driver session; pass driver sessions and ``None`` through."""
    return session.session if isinstance(session, Tx) else session


# -- the registry ---------------------------------------------------------------


class Registry:
    """Owns one ``AsyncMongoClient`` and the models bound to it.

    ::

        async with Registry("mongodb://localhost/app", models=[Article, User]) as registry:
            await Article.find(Q.title == "x").all()
    """

    def __init__(
        self,
        uri: str = "mongodb://localhost:27017",
        *,
        database: str | None = None,
        models: t.Iterable[type] = (),
        plugins: t.Iterable[Plugin] = (),
        max_time_ms: int | None = None,
        on_command: CommandListener | t.Iterable[CommandListener] | None = None,
        tz_aware: bool = True,
        **client_options: t.Any,
    ) -> None:
        self.uri = uri
        self._database = database
        self.plugins: tuple[Plugin, ...] = tuple(plugins)
        self.max_time_ms = max_time_ms
        self._client_options = client_options
        self._codec_options = codec_options(tz_aware)
        if on_command is None:
            self._listeners: tuple[CommandListener, ...] = ()
        elif callable(on_command):
            self._listeners = (on_command,)
        else:
            self._listeners = tuple(on_command)
        self._client: AsyncMongoClient[dict[str, t.Any]] | None = None
        self._state: t.Literal["new", "open", "closed"] = "new"
        self._models: list[type] = []
        self._managers: dict[tuple[type, str | None], Manager[t.Any]] = {}
        self.bind(*models)

    # -- lifecycle -----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._state == "open"

    async def open(self) -> Registry:
        if self._state == "closed":
            raise RegistryClosedError("a closed registry cannot be reopened; create a new one")
        if self._state == "new":
            options = dict(self._client_options)
            if self._listeners:
                options["event_listeners"] = [*options.get("event_listeners", []), _Monitor(self._listeners)]
            options.setdefault("uuidRepresentation", "standard")
            options.setdefault("tz_aware", self._codec_options.tz_aware)
            self._client = AsyncMongoClient(self.uri, **options)
            self._state = "open"
            # Models may have been bound elsewhere since construction; claim them now.
            self.bind(*self._models)
        return self

    async def close(self) -> None:
        if self._state == "closed":
            return
        self._state = "closed"
        for model in self._models:
            if _BINDINGS.get(model) is self:
                del _BINDINGS[model]
        self._managers.clear()
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def __aenter__(self) -> Registry:
        return await self.open()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def client(self) -> AsyncMongoClient[dict[str, t.Any]]:
        if self._client is None or self._state != "open":
            raise RegistryClosedError(
                "the registry is not open; use `async with registry:` or `await registry.open()`"
            )
        return self._client

    async def ping(self) -> None:
        with wrap_errors():
            await self.client.admin.command("ping")

    # -- models ----------------------------------------------------------------

    def bind(self, *models: type) -> None:
        """Bind document classes to this registry. Validates each model's declaration."""
        from mongospecs.meta import meta_of

        if self._state == "closed":
            raise RegistryClosedError("cannot bind models to a closed registry")
        for model in models:
            meta_of(model)  # fails here, not at first write, if the model is misdeclared
            current = _BINDINGS.get(model)
            if current is not None and current is not self and current._state == "open":
                raise ConfigurationError(
                    f"{model.__name__} is already bound to another open registry; "
                    "use registry.manager(Model) for a second binding"
                )
            _BINDINGS[model] = self
            if model not in self._models:
                self._models.append(model)

    @property
    def models(self) -> tuple[type, ...]:
        return tuple(self._models)

    def database(self, name: str | None = None) -> AsyncDatabase[dict[str, t.Any]]:
        client = self.client
        if name is None:
            name = self._database
        if name is None:
            try:
                name = client.get_default_database().name
            except pe.ConfigurationError:
                raise ConfigurationError(
                    "no database: put one in the URI or pass database= to Registry"
                ) from None
        return client.get_database(name, codec_options=self._codec_options)

    def manager(self, model: type[R], *, db: str | None = None) -> Manager[R]:
        """The object that performs operations for ``model`` on this registry."""
        from mongospecs.manager import Manager

        key = (model, db)
        manager = self._managers.get(key)
        if manager is None:
            manager = Manager(model, self, db=db)
            self._managers[key] = manager
        return t.cast("Manager[R]", manager)

    # -- sessions and transactions ---------------------------------------------

    @asynccontextmanager
    async def session(self, **options: t.Any) -> t.AsyncIterator[Tx]:
        """A session for manual control: ``async with registry.session() as s, s.start_transaction():``"""
        with wrap_errors():
            session = self.client.start_session(**options)
        async with session:
            yield Tx(session, self)

    async def transaction(
        self,
        fn: t.Callable[[Tx], t.Awaitable[R]],
        *,
        read_concern: t.Any = None,
        write_concern: t.Any = None,
        read_preference: t.Any = None,
        max_commit_time_ms: int | None = None,
        independent: bool = False,
    ) -> R:
        """Run ``fn(tx)`` in a transaction and commit it.

        Transactions do not nest. MongoDB has no savepoints, and a transaction
        started while another one is running is a separate transaction: it commits
        on its own and stays committed if the outer one aborts. Calling this from
        inside another ``transaction()`` therefore raises
        :class:`TransactionError`; pass the outer ``tx`` down and use it instead.
        If a separate, independently committed transaction is what you want (an
        audit record that must survive a rollback), say so with ``independent=True``.

        ``fn`` may be called more than once: the driver re-runs it on transient
        transaction errors. Before every re-run, and if the transaction finally
        aborts, documents written inside it are restored to their prior
        in-memory state. Pass ``session=tx`` to every operation inside ``fn``.
        """
        if _IN_TRANSACTION.get() and not independent:
            raise TransactionError(
                "transaction() was called inside another transaction. Transactions do not nest: this one "
                "would commit on its own even if the outer one aborts. Pass the outer tx to the code that "
                "needs it, or use transaction(fn, independent=True) if a separate commit is intended."
            )
        async with self.client.start_session() as session:
            tx = Tx(session, self)

            async def attempt(_: t.Any) -> R:
                tx.rollback_memory()
                token = _IN_TRANSACTION.set(True)  # for detection only; sessions are always passed explicitly
                try:
                    return await fn(tx)
                except MongoSpecsError as exc:
                    # Let the driver see its own error so its retry logic applies.
                    cause = exc.__cause__
                    if isinstance(cause, pe.PyMongoError) and cause.has_error_label(
                        "TransientTransactionError"
                    ):
                        raise cause from None
                    raise
                finally:
                    _IN_TRANSACTION.reset(token)

            try:
                result = await session.with_transaction(
                    attempt,
                    read_concern=read_concern,
                    write_concern=write_concern,
                    read_preference=read_preference,
                    max_commit_time_ms=max_commit_time_ms,
                )
            except MongoSpecsError:
                tx.rollback_memory()
                raise
            except pe.PyMongoError as exc:
                tx.rollback_memory()
                raise translate(exc) from exc
            except BaseException:
                tx.rollback_memory()
                raise
            tx._journal.clear()
            return result

    # -- indexes ---------------------------------------------------------------

    async def plan_indexes(self, *models: type) -> t.Any:
        """Compare declared and existing indexes. See :func:`mongospecs.indexes.plan_indexes`."""
        from mongospecs.indexes import plan_indexes

        return await plan_indexes(self, *models)

    async def apply_indexes(self, plan: t.Any = None, *, drop: bool = False) -> t.Any:
        """Create missing indexes; drop or recreate only with ``drop=True``."""
        from mongospecs.indexes import apply_indexes

        return await apply_indexes(self, plan, drop=drop)

    def __repr__(self) -> str:
        return f"<Registry {self._state} models={[m.__name__ for m in self._models]}>"


def registry_of(model: type) -> Registry:
    """The registry a model is bound to."""
    for klass in model.__mro__:
        registry = _BINDINGS.get(klass)
        if registry is not None and registry._state != "closed":
            if registry._state != "open":
                raise RegistryClosedError(
                    f"the registry {model.__name__} is bound to is not open; "
                    "use `async with registry:` or `await registry.open()`"
                )
            return registry
    raise NotBoundError(
        f"{model.__name__} is not bound to an open registry; "
        "pass it in Registry(models=[...]) or call registry.bind()"
    )


class op_scope:
    """Marks the mongospecs operation in progress, for command listeners, and translates errors."""

    __slots__ = ("_token", "_value")

    def __init__(self, registry: Registry, model: str, op: str) -> None:
        self._value = (model, op) if registry._listeners else None
        self._token: t.Any = None

    def __enter__(self) -> None:
        if self._value is not None:
            self._token = _CURRENT_OP.set(self._value)

    def __exit__(self, exc_type: t.Any, exc: BaseException | None, tb: t.Any) -> None:
        if self._token is not None:
            _CURRENT_OP.reset(self._token)
        if exc is not None and not isinstance(exc, MongoSpecsError) and isinstance(exc, pe.PyMongoError):
            raise translate(exc) from exc
        from bson.errors import BSONError

        if isinstance(exc, BSONError):
            raise translate(exc) from exc
