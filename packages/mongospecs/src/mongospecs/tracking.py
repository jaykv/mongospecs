"""Change tracking by snapshot.

When a document is loaded (or written), its encoded form is kept in a side table
keyed by object identity. ``save()`` diffs the current encoding against that
snapshot and sends only the difference. No attribute interception is involved,
so the mechanism is the same for msgspec, pydantic and attrs.

The table entry is removed by a weakref finaliser when the document is
collected, which is why document classes must be weak-referenceable.
"""

from __future__ import annotations

import copy
import typing as t
import weakref

__all__ = ["DocState", "diff", "forget", "state_of", "track"]


class DocState:
    """What mongospecs remembers about a tracked document."""

    __slots__ = ("manager", "partial", "snapshot")

    def __init__(self, snapshot: dict[str, t.Any], manager: t.Any, partial: frozenset[str] | None) -> None:
        self.snapshot = snapshot
        self.manager = manager
        self.partial = partial
        """Stored keys that were loaded, when the document was read with a projection."""

    def copy(self) -> DocState:
        return DocState(copy.deepcopy(self.snapshot), self.manager, self.partial)


_states: dict[int, DocState] = {}


def state_of(obj: t.Any) -> DocState | None:
    return _states.get(id(obj))


def track(
    obj: t.Any,
    snapshot: dict[str, t.Any],
    manager: t.Any,
    partial: frozenset[str] | None = None,
) -> DocState:
    key = id(obj)
    state = DocState(snapshot, manager, partial)
    if key not in _states:
        weakref.finalize(obj, _states.pop, key, None)
    _states[key] = state
    return state


def restore(obj: t.Any, state: DocState | None) -> None:
    """Put back a previous state (used when a transaction aborts)."""
    key = id(obj)
    if state is None:
        _states.pop(key, None)
    else:
        if key not in _states:
            weakref.finalize(obj, _states.pop, key, None)
        _states[key] = state


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
