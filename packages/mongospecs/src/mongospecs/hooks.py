"""Hooks and plugins.

Four kinds of contribution, matching Mongoose's four kinds of middleware:

========================  ==============  ===========================================
kind                      Mongoose name   here
========================  ==============  ===========================================
document hooks            document        ``before``/``after`` for insert, save, delete
command/filter rewriting  query           ``on_insert``, ``on_update``, ``on_filter``,
                                          ``on_delete``
pipeline rewriting        aggregate       ``on_pipeline``
bulk hooks                model           ``before_bulk``/``after_bulk``
========================  ==============  ===========================================

Document hooks run only for document-level operations. Filter-based writes
(``Model.find(...).update(...)``, ``.delete()``) never load documents, so only
the command, filter and pipeline rewriters apply to them.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

if t.TYPE_CHECKING:
    from mongospecs.markers import Index
    from mongospecs.meta import ModelMeta

__all__ = ["DocumentEvent", "OpCtx", "Plugin"]

DocumentEvent = t.Literal["insert", "save", "delete"]
DOCUMENT_EVENTS: tuple[str, ...] = ("insert", "save", "delete")

Hook = t.Callable[..., t.Awaitable[None]]


@dataclass(slots=True)
class OpCtx:
    """What an operation is doing; passed to every hook and rewriter."""

    meta: ModelMeta
    op: str
    """``find``, ``count``, ``insert``, ``update``, ``delete``, ``aggregate``, ``bulk`` ..."""
    session: t.Any = None
    """The ``Tx`` or driver session the operation runs in, if any."""
    manager: t.Any = None
    many: bool = False
    extra: dict[str, t.Any] = field(default_factory=dict)

    @property
    def model(self) -> type:
        return self.meta.model

    @property
    def registry(self) -> t.Any:
        return self.manager.registry if self.manager is not None else None


class Plugin:
    """Base class for plugins. Override only what you need; every method has a no-op default.

    A plugin attaches to a model through a field marker (for the built-in
    ones), through ``SpecConfig(plugins=[...])``, or to every model of a
    registry through ``Registry(plugins=[...])``.
    """

    name: str = ""
    """Used by ``Query.unscoped("name")`` to switch this plugin's scope off."""

    def setup(self, meta: ModelMeta) -> None:
        """Validate configuration. Called once per model, when its metadata is built."""

    # -- command and filter rewriting (query middleware) ---------------------

    def on_insert(self, ctx: OpCtx, doc: dict[str, t.Any]) -> None:
        """Mutate an encoded document just before it is inserted."""

    def on_update(self, ctx: OpCtx, flt: dict[str, t.Any], update: dict[str, t.Any]) -> None:
        """Mutate the filter and update document of any update."""

    def on_filter(self, ctx: OpCtx, flt: dict[str, t.Any]) -> dict[str, t.Any] | None:
        """Return a scope to AND into every read, update and delete filter."""
        return None

    def on_delete(self, ctx: OpCtx, flt: dict[str, t.Any]) -> dict[str, t.Any] | None:
        """Return an update document to turn a delete into an update (soft delete)."""
        return None

    # -- pipeline rewriting (aggregate middleware) ---------------------------

    def on_pipeline(self, ctx: OpCtx, pipeline: list[dict[str, t.Any]]) -> list[dict[str, t.Any]]:
        return pipeline

    # -- document hooks -------------------------------------------------------

    async def before(self, ctx: OpCtx, event: str, doc: t.Any) -> None:
        """Called before a document-level insert, save or delete."""

    async def after(self, ctx: OpCtx, event: str, doc: t.Any) -> None:
        """Called after a document-level insert, save or delete succeeded."""

    def applied(self, ctx: OpCtx, doc: t.Any, written: dict[str, t.Any]) -> None:
        """Mirror values this plugin wrote (timestamps, revision) onto the in-memory document."""

    # -- bulk hooks (model middleware) ----------------------------------------

    async def before_bulk(self, ctx: OpCtx, operations: list[t.Any]) -> None: ...

    async def after_bulk(self, ctx: OpCtx, operations: list[t.Any]) -> None: ...

    # -- indexes -------------------------------------------------------------

    def indexes(self, meta: ModelMeta) -> list[Index]:
        return []
