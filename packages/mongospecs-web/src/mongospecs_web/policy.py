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


class View:
    """What of a model is readable, writable, filterable and sortable from outside."""

    def __init__(self, model: type) -> None:
        self.model = model
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
        self.insertable: frozenset[str] = frozenset(
            f.name
            for f in fields.values()
            if f.policy.can_insert and f.name != "id" and f.name not in managed and not f.transient
        )
        self.updatable: frozenset[str] = frozenset(
            name for name in writable if fields[name].policy.can_update and not fields[name].has(Immutable)
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
        revision = next((f for f in fields.values() if f.has(Revision)), None)
        self.revision: str | None = revision.name if revision else None

    # -- output ----------------------------------------------------------------

    def serialize(self, doc: t.Any, include: t.Collection[str] = ()) -> dict[str, t.Any]:
        """The Read view of a document as JSON types. Private fields never appear."""
        if type(doc) is not self.model:
            return view_of(type(doc)).serialize(doc, include)
        data = self.meta.codec.to_json(doc)
        out = {name: data.get(name) for name in self._plain}
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

    def etag(self, doc: t.Any) -> str | None:
        if self.revision is None:
            return None
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
        return data

    # -- schemas ----------------------------------------------------------------

    def _base(self) -> dict[str, t.Any]:
        return self.meta.codec.json_schema(self.model)

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
        if "$defs" in base:
            schema["$defs"] = base["$defs"]
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


_VIEWS: weakref.WeakKeyDictionary[type, View] = weakref.WeakKeyDictionary()


def view_of(model: type) -> View:
    view = _VIEWS.get(model)
    if view is None:
        view = _VIEWS[model] = View(model)
    return view
