"""Change tracking by snapshot.

A tracked document remembers how it looked when it was loaded. ``save()``
compares the current encoding with that snapshot and sends only the difference.
No attribute interception is involved, so the mechanism is the same for
msgspec, pydantic and attrs.

For documents read by a query, what is kept is the raw BSON the driver received,
as bytes: nothing is re-encoded at load time and the bytes are smaller than the
equivalent Python objects. The snapshot proper (the document decoded into the
model and encoded again, so that defaults and type coercions are already
applied) is only built if the document is actually saved.

State lives in a side table keyed by object identity and is removed by a weakref
finaliser when the document is collected, which is why document classes must be
weak-referenceable.
"""

from __future__ import annotations

import typing as t
import weakref

import bson

__all__ = ["DocState", "diff", "forget", "state_of", "track"]


def _nothing() -> None:
    return None


class DocState:
    """What mongospecs remembers about a tracked document."""

    __slots__ = ("_doc", "_raw", "_ref", "manager", "partial")

    def __init__(
        self,
        snapshot: dict[str, t.Any] | bytes,
        manager: t.Any,
        partial: frozenset[str] | None,
    ) -> None:
        self._raw: bytes | None = snapshot if isinstance(snapshot, bytes) else None
        self._doc: dict[str, t.Any] | None = None if isinstance(snapshot, bytes) else snapshot
        self.manager = manager
        self.partial = partial
        self._ref: t.Any = _nothing  # the weak reference to the tracked object, set by track()
        """Stored keys that were loaded, when the document was read with a projection."""

    @property
    def snapshot(self) -> dict[str, t.Any]:
        """The document as the model encoded it when it was loaded.

        Built on first use from the raw bytes by decoding into the model and
        encoding again. Going through the model means a field the stored
        document lacks already has its default here, so loading a document never
        looks like editing it.
        """
        if self._doc is None:
            assert self._raw is not None
            from mongospecs.meta import meta_of

            meta = self.manager.meta
            loaded = meta.decode(bson.decode(self._raw, self.manager.registry._codec_options))
            self._doc = meta_of(type(loaded)).encode(loaded)
            self._raw = None
        return self._doc


_states: dict[int, DocState] = {}


def state_of(obj: t.Any) -> DocState | None:
    state = _states.get(id(obj))
    if state is not None and state._ref() is not obj:
        return None  # the entry belongs to an object that is being collected; its id was reused
    return state


class _Watch(weakref.ref):  # type: ignore[type-arg]
    """A weak reference that remembers which entry of the side table it guards.

    One plain weak reference per tracked document is several times cheaper than a
    ``weakref.finalize`` (which also registers itself in a global table).
    """

    __slots__ = ("key",)
    key: int


def _gone(watch: _Watch) -> None:
    # ids are reused: only drop the entry if it still belongs to the object that died
    state = _states.get(watch.key)
    if state is not None and state._ref is watch:
        del _states[watch.key]


def _install(obj: t.Any, state: DocState) -> None:
    key = id(obj)
    current = _states.get(key)
    if current is not None and current._ref() is obj:
        state._ref = current._ref
    else:
        watch = _Watch(obj, _gone)
        watch.key = key
        state._ref = watch
    _states[key] = state


def track(
    obj: t.Any,
    snapshot: dict[str, t.Any] | bytes,
    manager: t.Any,
    partial: frozenset[str] | None = None,
) -> DocState:
    state = DocState(snapshot, manager, partial)
    _install(obj, state)
    return state


def restore(obj: t.Any, state: DocState | None) -> None:
    """Put back a previous state (used when a transaction aborts)."""
    if state is None:
        _states.pop(id(obj), None)
    else:
        _install(obj, state)


def forget(obj: t.Any) -> None:
    _states.pop(id(obj), None)


def tracked_count() -> int:
    return len(_states)


def _safe_keys(d: dict[str, t.Any]) -> bool:
    return all(isinstance(k, str) and k and "." not in k and not k.startswith("$") for k in d)


def diff(
    old: dict[str, t.Any],
    new: dict[str, t.Any],
    partial: frozenset[str] | None = None,
) -> tuple[dict[str, t.Any], list[str]]:
    """Return ``($set, $unset)`` turning ``old`` into ``new``.

    Nested documents are diffed into dotted paths. Lists, and dicts whose keys
    cannot be used in a dotted path, are replaced whole.
    """
    to_set: dict[str, t.Any] = {}
    to_unset: list[str] = []
    _diff(old, new, "", to_set, to_unset, partial)
    return to_set, to_unset


def _diff(
    old: dict[str, t.Any],
    new: dict[str, t.Any],
    prefix: str,
    to_set: dict[str, t.Any],
    to_unset: list[str],
    partial: frozenset[str] | None,
) -> None:
    for key, value in new.items():
        if partial is not None and key not in partial:
            continue
        if key == "_id" and not prefix:
            continue
        path = prefix + key
        if key not in old:
            to_set[path] = value
            continue
        before = old[key]
        if before == value and type(before) is type(value):
            continue
        if (
            isinstance(before, dict)
            and isinstance(value, dict)
            and value
            and _safe_keys(before)
            and _safe_keys(value)
        ):
            _diff(before, value, path + ".", to_set, to_unset, None)
        else:
            to_set[path] = value
    for key in old:
        if key not in new and (partial is None or key in partial) and not (key == "_id" and not prefix):
            to_unset.append(prefix + key)
