"""PATCH on embedded objects: JSON Merge Patch semantics, bounded by field policy.

A ``PATCH`` body that gives an object for an embedded model does not replace the
stored object. Each key it names is written on its own (``$set`` on a dotted
path, all in one atomic update), ``null`` clears a key, and keys it does not name
are left alone. That is RFC 7396 for objects; lists and dicts are still replaced
whole, as the RFC says.

The reason is not convenience. Replacing the object would write the model's
defaults over every value the client did not send, including the ones it is not
allowed to send: private fields, fields with ``Policy(update=False)``, immutable
ones. A client must not be able to clear what it cannot set, so:

* a nested key the client may not write is rejected, with its dotted path;
* an embedded object that holds such values cannot be set to ``null``;
* a list or dict of embedded objects that hold such values cannot be replaced
  through ``PATCH`` at all (there is no way to merge into a list item).

``PUT`` (:func:`plan_put`) is the same machinery asked a different question: every
field the client may write takes the value sent or goes back to its default, and
what the client cannot write stays as it is stored. ``Resource(merge_patch=False)``
turns the merge off: ``PATCH`` then replaces embedded objects whole, and the values
a client cannot send are reset to their defaults.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

from mongospecs import Document, Immutable, Ref, ValidationError
from mongospecs.codec import _UNION_ORIGINS, FieldInfo, _codec_or_none, codec_for, to_bson

__all__ = [
    "PatchPlan",
    "holds_protected",
    "nested_write_errors",
    "plan_patch",
    "plan_put",
    "single_model",
]


@dataclass
class PatchPlan:
    """A validated PATCH, split into whole-field values and writes inside embedded objects."""

    fields: dict[str, t.Any] = field(default_factory=dict)
    """Top-level fields given a whole new value, by attribute name."""
    set: dict[str, t.Any] = field(default_factory=dict)
    """Dotted path (attribute name, then stored keys) -> BSON-ready value."""
    unset: list[str] = field(default_factory=list)
    parents: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    """Dotted paths that must already hold an object, because the patch does not supply every
    required field of it: path -> (path as the client wrote it, required field names)."""

    @property
    def nested(self) -> bool:
        return bool(self.set or self.unset or self.parents)


def single_model(tp: t.Any) -> type | None:
    """The embedded model class when ``tp`` is exactly one (optionally ``| None``), else ``None``."""
    while t.get_origin(tp) is t.Annotated:
        tp = t.get_args(tp)[0]
    if t.get_origin(tp) in _UNION_ORIGINS:
        members = [a for a in t.get_args(tp) if a is not type(None)]
        return single_model(members[0]) if len(members) == 1 else None
    if isinstance(tp, type) and t.get_origin(tp) is None and not issubclass(tp, Document):
        return tp if _codec_or_none(tp) is not None else None
    return None


def _may(info: FieldInfo, action: str, explicit: bool) -> bool:
    if action == "create":
        return info.policy.insert is True if explicit else info.policy.can_insert
    if info.has(Immutable):
        return False
    return info.policy.update is True if explicit else info.policy.can_update


def _fields(cls: type) -> dict[str, tuple[str, FieldInfo]]:
    """Every key a client may use for a field of ``cls`` -> (attribute name, field)."""
    codec = codec_for(cls)
    out: dict[str, tuple[str, FieldInfo]] = {}
    for name, native, _, _ in codec.raw_fields(cls):
        info = codec.fields(cls)[name]
        for key in (name, native, info.stored):
            out.setdefault(key, (name, info))
    return out


def _protected(cls: type, explicit: bool, seen: set[type]) -> bool:
    """True when ``cls`` (or a model embedded in it) has a field a client cannot update."""
    if cls in seen:
        return False
    seen.add(cls)
    for info in codec_for(cls).fields(cls).values():
        if info.policy.private or not _may(info, "update", explicit):
            return True
        if holds_protected(info.type, explicit, seen):
            return True
    return False


def holds_protected(tp: t.Any, explicit: bool, seen: set[type] | None = None) -> bool:
    """True when a value of type ``tp`` can contain something a client cannot update."""
    seen = set() if seen is None else seen
    while t.get_origin(tp) is t.Annotated:
        tp = t.get_args(tp)[0]
    if t.get_origin(tp) is Ref:
        return False
    if isinstance(tp, type) and t.get_origin(tp) is None:
        if issubclass(tp, Document) or _codec_or_none(tp) is None:
            return False
        return _protected(tp, explicit, seen)
    return any(holds_protected(arg, explicit, seen) for arg in t.get_args(tp))


def nested_write_errors(
    tp: t.Any, value: t.Any, path: str, explicit: bool, out: list[dict[str, t.Any]], action: str = "create"
) -> None:
    """Keys inside embedded objects, given whole, that the client may not set on ``action``."""
    while t.get_origin(tp) is t.Annotated:
        tp = t.get_args(tp)[0]
    origin = t.get_origin(tp)
    if origin is Ref:
        return
    if origin is None:
        cls = single_model(tp)
        if cls is None or not isinstance(value, dict):
            return
        fields = _fields(cls)
        for key, item in value.items():
            hit = fields.get(key)
            if hit is None or hit[1].policy.private:
                continue  # unknown keys are the model's to reject; private ones are reported already
            if not _may(hit[1], action, explicit):
                out.append({"field": f"{path}.{key}", "message": f"cannot be set on {action}"})
            else:
                nested_write_errors(hit[1].type, item, f"{path}.{key}", explicit, out, action)
        return
    args = [a for a in t.get_args(tp) if a is not type(None)]
    if origin in _UNION_ORIGINS:
        for arg in args:
            nested_write_errors(arg, value, path, explicit, out, action)
    elif isinstance(value, list) and args:
        for index, item in enumerate(value):
            nested_write_errors(args[0], item, f"{path}.{index}", explicit, out, action)
    elif isinstance(value, dict) and len(args) == 2:
        for key, item in value.items():
            nested_write_errors(args[1], item, f"{path}.{key}", explicit, out, action)


def plan_patch(
    fields: t.Mapping[str, FieldInfo], data: t.Mapping[str, t.Any], explicit: bool, *, merge: bool = True
) -> tuple[PatchPlan, list[dict[str, t.Any]]]:
    """Split a PATCH body (already checked at the top level) into a :class:`PatchPlan`.

    Returns the plan and the list of field errors; the plan is only meaningful when
    there are none. With ``merge=False`` every value replaces the stored one whole; the
    only check left is that the body does not name a nested key the client may not write.
    """
    plan = PatchPlan()
    errors: list[dict[str, t.Any]] = []
    if not merge:
        for key, value in data.items():
            nested_write_errors(fields[key].type, value, key, explicit, errors, "update")
            plan.fields[key] = value
        return plan, errors
    for key, value in data.items():
        info = fields[key]
        cls = single_model(info.type)
        if cls is not None and isinstance(value, dict):
            _merge(cls, value, key, key, plan, errors, explicit)
        elif cls is not None and value is None and _protected(cls, explicit, set()):
            errors.append({"field": key, "message": _CANNOT_REMOVE})
        elif cls is None and holds_protected(info.type, explicit):
            errors.append({"field": key, "message": _CANNOT_REPLACE})
        else:
            plan.fields[key] = value
    return plan, errors


def plan_put(
    model: type,
    fields: t.Mapping[str, FieldInfo],
    updatable: t.Collection[str],
    data: t.Mapping[str, t.Any],
    explicit: bool,
) -> tuple[PatchPlan, list[dict[str, t.Any]]]:
    """Turn a PUT body (already checked at the top level) into a :class:`PatchPlan`.

    Every field the client may update is written: with the value sent, or with its default
    when the body leaves it out (a required field has to be sent). Fields the client may not
    update are not part of the plan, so they keep their stored values. The same holds inside
    an embedded object that has such fields; one that has none is replaced whole.
    """
    plan = PatchPlan()
    errors: list[dict[str, t.Any]] = []
    defaults = codec_for(model).defaults(model)
    for key, info in fields.items():
        if key not in updatable:
            continue
        cls = single_model(info.type)
        guarded = holds_protected(info.type, explicit)
        if key not in data:
            if guarded:
                continue  # resetting it would clear what only the server sets
            if info.required:
                errors.append({"field": key, "message": "is required"})
            else:
                plan.fields[key] = defaults.get(key)
        elif not guarded:
            plan.fields[key] = data[key]
        elif cls is not None and isinstance(data[key], dict):
            _merge(cls, data[key], key, key, plan, errors, explicit, replace=True)
        elif cls is not None and data[key] is None:
            errors.append({"field": key, "message": _CANNOT_REMOVE})
        elif cls is None:
            errors.append({"field": key, "message": _CANNOT_REPLACE})
        else:
            plan.fields[key] = data[key]  # not an object: the model's own validation rejects it
    return plan, errors


_CANNOT_REMOVE = "cannot be removed: it holds values only the server sets"
_CANNOT_REPLACE = (
    "cannot be replaced: its items hold values only the server sets, and replacing them would clear those"
)


def _merge(
    cls: type,
    value: dict[str, t.Any],
    client_path: str,
    path: str,
    plan: PatchPlan,
    errors: list[dict[str, t.Any]],
    explicit: bool,
    replace: bool = False,
) -> None:
    """Plan the writes for one embedded object.

    With ``replace`` (PUT) the keys the client may write and did not send go back to their
    defaults, and an object nested in this one is only walked into when it has values to keep.
    """
    codec = codec_for(cls)
    fields = _fields(cls)
    defaults = codec.defaults(cls)
    absent = codec.absent_when_none(cls)
    supplied: set[str] = set()
    for key, item in value.items():
        where = f"{client_path}.{key}"
        hit = fields.get(key)
        if hit is None or hit[1].policy.private:
            errors.append({"field": where, "message": "unknown field"})
            continue
        name, info = hit
        if not _may(info, "update", explicit):
            errors.append({"field": where, "message": "cannot be set on update"})
            continue
        supplied.add(name)
        target = f"{path}.{info.stored}"
        inner = single_model(info.type)
        if replace and not holds_protected(info.type, explicit):
            inner = None  # nothing in it to keep: validated and written whole below
        if inner is not None and isinstance(item, dict):
            _merge(inner, item, where, target, plan, errors, explicit, replace)
        elif item is None:
            if inner is not None and _protected(inner, explicit, set()):
                errors.append({"field": where, "message": _CANNOT_REMOVE})
            elif name in defaults and defaults[name] is None:
                if info.stored in absent:
                    plan.unset.append(target)  # not stored while None
                else:
                    plan.set[target] = None  # StoreNone: an explicit null
            else:
                errors.append({"field": where, "message": "may not be null"})
        elif inner is None and holds_protected(info.type, explicit):
            errors.append({"field": where, "message": _CANNOT_REPLACE})
        else:
            try:
                plan.set[target] = to_bson(codec.validate_field(cls, name, item))
            except ValidationError as exc:
                message = exc.errors[0]["message"] if exc.errors else str(exc)
                errors.append({"field": where, "message": message})
    if replace:
        for name, info in codec.fields(cls).items():
            if name in supplied or info.policy.private or not _may(info, "update", explicit):
                continue
            if holds_protected(info.type, explicit):
                continue
            target = f"{path}.{info.stored}"
            if info.required:
                errors.append({"field": f"{client_path}.{name}", "message": "is required"})
                supplied.add(name)  # reported here, not again as a missing parent
            elif defaults.get(name) is None:
                if info.stored in absent:
                    plan.unset.append(target)
                else:
                    plan.set[target] = None
            else:
                plan.set[target] = to_bson(defaults[name])
    required = tuple(
        name for name, info in codec.fields(cls).items() if info.required and name not in supplied
    )
    if required:
        # Without these the object cannot be created by this patch, so it has to exist already.
        plan.parents[path] = (client_path, required)
