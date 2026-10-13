"""Views of a model derived from its field policy.

One model declaration drives four views. No view class is generated: the policy
is data, input is filtered by it and then validated against the real model, and
JSON Schema for each view is produced by transforming the model's own schema.
"""

from __future__ import annotations

import typing as t
import weakref

from mongospecs import CreatedAt, Deferred, DeletedAt, Immutable, Ref, Revision, UpdatedAt, meta_of
from mongospecs.codec import FieldInfo
from mongospecs.jsonable import jsonable
from mongospecs.markers import is_computed
from mongospecs.meta import ModelMeta

__all__ = ["View", "jsonable", "view_of"]

_MANAGED = (CreatedAt, UpdatedAt, DeletedAt, Revision)

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
"""MongoDB's largest integer type is 64 bits; a larger integer cannot be stored as one."""


class Nested:
    """Where the private fields are inside the value of one field.

    ``private`` and ``children`` describe an embedded model (by every key it may appear
    under: attribute name, stored name, the backend's own name); ``elem`` describes the
    items of a list or the values of a dict.
    """

    __slots__ = ("children", "elem", "model", "private")

    def __init__(self) -> None:
        self.model = False
        self.private: frozenset[str] = frozenset()
        self.children: dict[str, Nested] = {}
        self.elem: Nested | None = None

    def hides_something(self, seen: set[int] | None = None) -> bool:
        seen = set() if seen is None else seen
        if id(self) in seen:
            return False
        seen.add(id(self))
        return (
            bool(self.private)
            or any(child.hides_something(seen) for child in self.children.values())
            or (self.elem is not None and self.elem.hides_something(seen))
        )


def embedded_models(tp: t.Any, found: dict[type, None] | None = None) -> dict[type, None]:
    """Every embedded (non-document) model class reachable from an annotation, at any depth."""
    from mongospecs import Document
    from mongospecs.codec import _codec_or_none

    found = {} if found is None else found
    while t.get_origin(tp) is t.Annotated:
        tp = t.get_args(tp)[0]
    if isinstance(tp, type) and not issubclass(tp, Document) and t.get_origin(tp) is None:
        codec = _codec_or_none(tp)
        if codec is not None and tp not in found:
            found[tp] = None
            for info in codec.fields(tp).values():
                embedded_models(info.type, found)
        return found
    if t.get_origin(tp) is Ref:
        return found  # another document: its own view applies
    for arg in t.get_args(tp):
        embedded_models(arg, found)
    return found


def _model_keys(cls: type) -> dict[str, tuple[FieldInfo, frozenset[str]]]:
    from mongospecs.codec import codec_for

    codec = codec_for(cls)
    natives = {name: native for name, native, _, _ in codec.raw_fields(cls)}
    return {
        name: (info, frozenset({name, info.stored, natives.get(name, name)}))
        for name, info in codec.fields(cls).items()
    }


def nested_plan(tp: t.Any, cache: dict[type, Nested]) -> Nested | None:
    """The :class:`Nested` plan for an annotation, or ``None`` when it holds no embedded model."""
    from mongospecs import Document
    from mongospecs.codec import _UNION_ORIGINS, _codec_or_none

    while t.get_origin(tp) is t.Annotated:
        tp = t.get_args(tp)[0]
    origin = t.get_origin(tp)
    if origin is Ref:
        return None
    if origin is None:
        if not isinstance(tp, type) or issubclass(tp, Document) or _codec_or_none(tp) is None:
            return None
        node = cache.get(tp)
        if node is None:
            node = cache[tp] = Nested()
            node.model = True
            private: set[str] = set()
            for info, keys in _model_keys(tp).values():
                if info.policy.private:
                    private |= keys
                    continue
                child = nested_plan(info.type, cache)
                if child is not None:
                    for key in keys:
                        node.children[key] = child
            node.private = frozenset(private)
        return node
    parts = [plan for arg in t.get_args(tp) if (plan := nested_plan(arg, cache)) is not None]
    if not parts:
        return None
    if origin in _UNION_ORIGINS:
        if len(parts) == 1:
            return parts[0]
        merged = Nested()  # a union: hide whatever any member hides, so nothing can leak
        for part in parts:
            merged.model = merged.model or part.model
            merged.private |= part.private
            for key, child in part.children.items():
                merged.children.setdefault(key, child)
            merged.elem = merged.elem or part.elem
        return merged
    container = Nested()
    container.elem = parts[-1]  # list[X] -> X, dict[str, X] -> X
    return container


def strip_nested(value: t.Any, node: Nested) -> t.Any:
    """Remove private keys from a JSON-shaped value, in place."""
    if isinstance(value, dict):
        if node.model:
            for key in node.private:
                value.pop(key, None)
            for key, child in node.children.items():
                if key in value:
                    strip_nested(value[key], child)
        elif node.elem is not None:
            for item in value.values():
                strip_nested(item, node.elem)
    elif isinstance(value, list) and node.elem is not None:
        for item in value:
            strip_nested(item, node.elem)
    return value


def nested_violations(value: t.Any, node: Nested, path: str, out: list[str]) -> None:
    """Collect the dotted paths of private keys a client supplied."""
    if isinstance(value, dict):
        if node.model:
            out.extend(f"{path}.{key}" for key in value if key in node.private)
            for key, child in node.children.items():
                if key in value:
                    nested_violations(value[key], child, f"{path}.{key}", out)
        elif node.elem is not None:
            for key, item in value.items():
                nested_violations(item, node.elem, f"{path}.{key}", out)
    elif isinstance(value, list) and node.elem is not None:
        for index, item in enumerate(value):
            nested_violations(item, node.elem, f"{path}.{index}", out)


def _refs(node: t.Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                out.add(value.rsplit("/", 1)[1])
            else:
                _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)


def _reachable(schema: dict[str, t.Any], defs: dict[str, t.Any]) -> dict[str, t.Any]:
    """The definitions ``schema`` actually refers to, directly or through one another."""
    found: set[str] = set()
    _refs(schema, found)
    pending = list(found)
    while pending:
        inner: set[str] = set()
        _refs(defs.get(pending.pop(), {}), inner)
        pending.extend(inner - found)
        found |= inner
    return {name: definition for name, definition in defs.items() if name in found}


def _has_oversized_int(value: t.Any) -> bool:
    if isinstance(value, int):
        return not INT64_MIN <= value <= INT64_MAX
    if isinstance(value, list):
        return any(_has_oversized_int(v) for v in value)
    if isinstance(value, dict):
        return any(_has_oversized_int(v) for v in value.values())
    return False


def _takes_any_number(tp: t.Any) -> bool:
    """A float or Decimal field: a large JSON integer becomes one of those and fits."""
    import decimal

    from mongospecs.codec import strip_optional

    return strip_optional(tp) in (float, decimal.Decimal)


class View:
    """What of a model is readable, writable, filterable and sortable from outside."""

    def __init__(self, model: type, *, explicit_writes: bool = False) -> None:
        self.model = model
        self.explicit_writes = explicit_writes
        self.meta: ModelMeta = meta_of(model)
        fields = self.meta.fields
        managed = {f.name for f in fields.values() if any(f.has(m) for m in _MANAGED)}
        self.private: frozenset[str] = frozenset(f.name for f in fields.values() if f.policy.private)
        self.readable: tuple[FieldInfo, ...] = tuple(
            f for f in fields.values() if not f.policy.private and not f.has(Deferred)
        )
        writable = frozenset(
            f.name for f in fields.values() if f.name != "id" and f.name not in managed and not f.transient
        )

        def may(flag: bool | None, default: bool) -> bool:
            # explicit mode: only what the policy turns on; otherwise the policy's own default
            return flag is True if explicit_writes else default

        self.insertable: frozenset[str] = frozenset(
            name for name in writable if may(fields[name].policy.insert, fields[name].policy.can_insert)
        )
        self.updatable: frozenset[str] = frozenset(
            name
            for name in writable
            if may(fields[name].policy.update, fields[name].policy.can_update)
            and not fields[name].has(Immutable)
        )
        self.filterable: dict[str, tuple[str, ...]] = {
            f.name: f.policy.filter_ops
            for f in fields.values()
            if f.policy.filter_ops and not f.policy.private and not f.transient
        }
        self.sortable: frozenset[str] = frozenset(
            f.name for f in fields.values() if f.policy.sort and not f.policy.private and not f.transient
        )
        self.relations: frozenset[str] = frozenset(
            f.name for f in fields.values() if (f.ref is not None or f.transient) and not f.policy.private
        )
        self.computed: tuple[str, ...] = tuple(
            name
            for klass in reversed(model.__mro__)
            for name, attr in vars(klass).items()
            if is_computed(attr)
        )
        # Serialisation plan: plain readable fields, and the relations that may be embedded.
        self._plain: tuple[str, ...] = tuple(f.name for f in self.readable if not f.transient)
        self._relations: tuple[FieldInfo, ...] = tuple(
            f for f in self.readable if f.transient or f.ref is not None
        )
        # Private fields of embedded models, at any depth below a field of this model.
        cache: dict[type, Nested] = {}
        self._nested: dict[str, Nested] = {
            f.name: plan
            for f in fields.values()
            if not f.transient and (plan := nested_plan(f.type, cache)) is not None and plan.hides_something()
        }
        self._nested_read: tuple[tuple[str, Nested], ...] = tuple(
            (name, plan) for name, plan in self._nested.items() if name in self._plain
        )
        revision = next((f for f in fields.values() if f.has(Revision)), None)
        self.revision: str | None = revision.name if revision else None
        self._revision_stored: str | None = revision.stored if revision else None
        from mongospecs_web.fastread import compile_plan

        self.fast = compile_plan(model, self.computed)
        """Plan for serialising straight from a stored document, or ``None`` when this model
        needs the model path (see :mod:`mongospecs_web.fastread`)."""

    # -- output ----------------------------------------------------------------

    def serialize(self, doc: t.Any, include: t.Collection[str] = ()) -> dict[str, t.Any]:
        """The Read view of a document as JSON types. Private fields never appear.

        ``doc`` is a model instance, or a stored document (a ``dict``) read for this purpose.
        """
        if type(doc) is dict:
            return self.serialize_raw(doc, include)
        if type(doc) is not self.model:
            return view_of(type(doc)).serialize(doc, include)
        data = self.meta.codec.to_json(doc)
        out = {name: data.get(name) for name in self._plain}
        for name, plan in self._nested_read:
            strip_nested(out[name], plan)
        if include:
            for f in self._relations:
                if f.name not in include:
                    continue
                nested = {p.partition(".")[2] for p in include if p.partition(".")[0] == f.name and "." in p}
                value = getattr(doc, f.name)
                if f.transient:
                    out[f.name] = (
                        [_child(v, nested) for v in value] if isinstance(value, list) else jsonable(value)
                    )
                else:
                    out[f.name] = _embed(value, out.get(f.name), nested)
        if self.meta.disc_key:
            out[self.meta.disc_key] = self.meta.disc_value
        for name in self.computed:
            out[name] = jsonable(getattr(doc, name))
        return out

    def serialize_raw(self, raw: dict[str, t.Any], include: t.Collection[str] = ()) -> dict[str, t.Any]:
        """The Read view of a stored document; identical to decoding it and calling ``serialize``."""
        from mongospecs_web.fastread import Slow

        if self.fast is not None and not include:
            try:
                return self.fast.serialize(raw)
            except Slow:
                pass  # a value that does not have its declared type: take the careful path
        return self.serialize(self.meta.decode(raw), include)

    def etag(self, doc: t.Any) -> str | None:
        if self.revision is None:
            return None
        if type(doc) is dict:
            revision = doc.get(self._revision_stored, 0)
            if type(revision) is not int:  # stored as a double by another writer: let the model read it
                revision = getattr(self.meta.decode(doc), self.revision)
            return f'"{revision}"'
        return f'"{getattr(doc, self.revision)}"'

    # -- input ---------------------------------------------------------------

    def check_input(self, data: t.Any, allowed: frozenset[str], action: str) -> dict[str, t.Any]:
        """Reject bodies that are not objects, name unknown fields or name read-only ones."""
        from mongospecs_web.errors import Unprocessable

        if not isinstance(data, dict):
            raise Unprocessable("The request body must be a JSON object.")
        errors: list[dict[str, t.Any]] = []
        for key in data:
            if key in allowed:
                continue
            known = key in self.meta.fields and key not in self.private
            errors.append(
                {"field": key, "message": f"cannot be set on {action}" if known else "unknown field"}
            )
        if errors:
            raise Unprocessable("The request body names fields that cannot be written.", errors=errors)
        for key, value in data.items():
            plan = self._nested.get(key)
            if plan is not None:
                hidden: list[str] = []
                nested_violations(value, plan, key, hidden)
                errors.extend({"field": path, "message": "unknown field"} for path in hidden)
            if _has_oversized_int(value) and not _takes_any_number(self.meta.fields[key].type):
                errors.append(
                    {"field": key, "message": f"integers must be between {INT64_MIN} and {INT64_MAX}"}
                )
        if errors:
            raise Unprocessable("The request body failed validation.", errors=errors)
        return data

    # -- schemas ----------------------------------------------------------------

    def _base(self) -> dict[str, t.Any]:
        base = self.meta.codec.json_schema(self.model)
        defs = dict(base.get("$defs", {}))
        hidden_by_name: dict[str, frozenset[str]] = {}
        if self.private:
            # The model's own definition survives only where the model refers to itself;
            # it must not describe more than the Read view does.
            hidden_by_name[self.model.__name__] = self.private | {
                self.meta.fields[name].stored for name in self.private
            }
        for info in self.meta.fields.values():
            for cls in embedded_models(info.type):
                keys = frozenset().union(*(k for f, k in _model_keys(cls).values() if f.policy.private))
                if keys:
                    hidden_by_name[cls.__name__] = hidden_by_name.get(cls.__name__, frozenset()) | keys
        changed = False
        for name, hidden in hidden_by_name.items():
            own = defs.get(name)
            if isinstance(own, dict):
                own = dict(own)
                own["properties"] = {k: v for k, v in own.get("properties", {}).items() if k not in hidden}
                own["required"] = [k for k in own.get("required", []) if k not in hidden]
                if not own["required"]:
                    del own["required"]
                defs[name] = own
                changed = True
        if changed:
            base = {**base, "$defs": defs}
        return base

    def schema(self, kind: t.Literal["read", "create", "patch"]) -> dict[str, t.Any]:
        """JSON Schema of one view. ``$defs`` of nested models are kept alongside."""
        base = self._base()
        props: dict[str, t.Any] = base.get("properties", {})
        required: list[str] = list(base.get("required", []))
        if kind == "read":
            keep = [f.name for f in self.readable]
            out_props = {name: props.get(name, {}) for name in keep if name in props}
            for name in self.computed:
                out_props[name] = {}
            schema: dict[str, t.Any] = {
                "type": "object",
                "properties": out_props,
                "required": [n for n in keep if n == "id" or n in required],
            }
        else:
            allowed = self.insertable if kind == "create" else self.updatable
            schema = {
                "type": "object",
                "properties": {name: props[name] for name in props if name in allowed},
                "additionalProperties": False,
            }
            if kind == "create":
                schema["required"] = [n for n in required if n in allowed]
        if not schema.get("required"):
            schema.pop("required", None)
        defs = _reachable(schema, base.get("$defs", {}))
        if defs:
            schema["$defs"] = defs
        return schema


def _child(value: t.Any, include: t.Collection[str]) -> t.Any:
    if value is None:
        return None
    return view_of(type(value)).serialize(value, include)


def _embed(value: t.Any, encoded: t.Any, include: t.Collection[str]) -> t.Any:
    """A reference field in the output: the related document when it is loaded, else its id."""

    def one(ref: t.Any, raw: t.Any) -> t.Any:
        if isinstance(ref, Ref) and ref.loaded:
            return _child(ref.doc, include)
        return jsonable(raw)

    if value is None:
        return None
    if isinstance(value, list):
        return [one(r, e) for r, e in zip(value, encoded or [], strict=False)]
    if isinstance(value, dict):
        return {k: one(r, (encoded or {}).get(k)) for k, r in value.items()}
    return one(value, encoded)


_VIEWS: weakref.WeakKeyDictionary[type, dict[bool, View]] = weakref.WeakKeyDictionary()


def view_of(model: type, *, explicit_writes: bool = False) -> View:
    views = _VIEWS.setdefault(model, {})
    view = views.get(explicit_writes)
    if view is None:
        view = views[explicit_writes] = View(model, explicit_writes=explicit_writes)
    return view
