"""Change streams: ``async for event in Article.watch(): ...``"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from mongospecs.registry import driver_session

if t.TYPE_CHECKING:
    from mongospecs.manager import Manager

__all__ = ["ChangeEvent", "ChangeStream"]


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    operation: str
    """``insert``, ``update``, ``replace``, ``delete``, ``invalidate`` ..."""
    id: t.Any
    document: t.Any
    """The decoded document after the change, when the server sent one."""
    updated: dict[str, t.Any] | None
    removed: list[str] | None
    resume_token: t.Any
    """Pass as ``resume_after=`` to continue after this event."""
    raw: dict[str, t.Any]


class ChangeStream:
    """An async iterator of :class:`ChangeEvent`. Also usable as ``async with`` for prompt cleanup.

    Plugin scopes do not apply to change streams; pass a ``$match`` pipeline to filter.
    The driver resumes automatically after transient errors such as a primary step-down.
    """

    def __init__(
        self, manager: Manager[t.Any], pipeline: list[t.Mapping[str, t.Any]], options: dict[str, t.Any]
    ) -> None:
        self._manager = manager
        self._pipeline = pipeline
        self._options = dict(options)
        self._options.setdefault("full_document", "updateLookup")
        session = self._options.pop("session", None)
        if session is not None:
            self._options["session"] = driver_session(session)
        self._stream: t.Any = None

    async def _open(self) -> t.Any:
        if self._stream is None:
            with self._manager._op("watch"):
                self._stream = await self._manager.collection.watch(self._pipeline, **self._options)
        return self._stream

    async def __aenter__(self) -> ChangeStream:
        await self._open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            await stream.close()

    @property
    def resume_token(self) -> t.Any:
        return self._stream.resume_token if self._stream is not None else None

    def __aiter__(self) -> ChangeStream:
        return self

    def _event(self, change: dict[str, t.Any]) -> ChangeEvent:
        full = change.get("fullDocument")
        description = change.get("updateDescription") or {}
        return ChangeEvent(
            operation=change.get("operationType", ""),
            id=(change.get("documentKey") or {}).get("_id"),
            document=self._manager.meta.decode(full) if full is not None else None,
            updated=description.get("updatedFields"),
            removed=description.get("removedFields"),
            resume_token=change.get("_id"),
            raw=change,
        )

    async def __anext__(self) -> ChangeEvent:
        stream = await self._open()
        with self._manager._op("watch"):
            change = await stream.next()
        return self._event(change)

    async def try_next(self) -> ChangeEvent | None:
        """The next event if one is ready within the server's await time, else ``None``."""
        stream = await self._open()
        with self._manager._op("watch"):
            change = await stream.try_next()
        return self._event(change) if change is not None else None
