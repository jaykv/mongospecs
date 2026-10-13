"""Per-model metadata, computed once per class and cached."""

from __future__ import annotations

import dataclasses
import importlib
import typing as t
import weakref

from bson import ObjectId

from mongospecs.codec import Codec, FieldInfo, codec_for, element_type, loosen, strip_optional, to_bson
from mongospecs.errors import ConfigurationError, QueryError, ValidationError
from mongospecs.hooks import DOCUMENT_EVENTS, Hook, Plugin
from mongospecs.markers import (
    CreatedAt,
    Deferred,
    DeletedAt,
    Immutable,
    Marker,
    Revision,
    SpecConfig,
    UpdatedAt,
)
from mongospecs.refs import id_of

__all__ = ["ModelMeta", "meta_of"]

# Built-in marker -> plugin factory ("module:attribute", imported lazily).
_MARKER_PLUGINS: dict[type[Marker], str] = {
    CreatedAt: "mongospecs.plugins.timestamps:Timestamps",
    UpdatedAt: "mongospecs.plugins.timestamps:Timestamps",
    DeletedAt: "mongospecs.plugins.soft_delete:SoftDelete",
    Revision: "mongospecs.plugins.revision:Revisions",
}

_METAS: weakref.WeakKeyDictionary[type, ModelMeta] = weakref.WeakKeyDictionary()
_HOOKS: weakref.WeakKeyDictionary[type, dict[tuple[str, str], list[Hook]]] = weakref.WeakKeyDictionary()
_SCOPES: weakref.WeakKeyDictionary[type, dict[str, t.Callable[..., t.Any]]] = weakref.WeakKeyDictionary()


def _load(path: str) -> t.Any:
    module, _, attr = path.partition(":")
    return getattr(importlib.import_module(module), attr)


def _own_config(cls: type) -> SpecConfig | None:
    cfg = cls.__dict__.get("__spec__")
    return cfg if isinstance(cfg, SpecConfig) else None


def resolve_config(cls: type) -> SpecConfig:
    """Own ``__spec__`` wins. An inherited one keeps its collection only inside a discriminator hierarchy."""
    from mongospecs.model import Document

    own = _own_config(cls)
    parent = next((b for b in cls.__mro__[1:] if issubclass(b, Document) and b is not Document), None)
    inherited = resolve_config(parent) if parent is not None else None
    if own is None:
        if inherited is None:
            return SpecConfig()
        if inherited.discriminator:
            return dataclasses.replace(inherited, discriminator_value=None)
        return dataclasses.replace(inherited, collection=None, discriminator_value=None)
    if inherited is not None and inherited.discriminator and not own.discriminator:
        return dataclasses.replace(
            own, discriminator=inherited.discriminator, collection=own.collection or inherited.collection
        )
    return own


def _all_subclasses(cls: type) -> t.Iterator[type]:
    sub: type
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


class ModelMeta:
    """Everything mongospecs derives from a model class."""

    def __init__(self, model: type) -> None:
        from mongospecs.model import Document

        if not (isinstance(model, type) and issubclass(model, Document)):
            raise ConfigurationError(f"{model!r} is not a mongospecs document class")
        self.model = model
        self.codec: Codec = codec_for(model)
        self.config = resolve_config(model)
        self.fields: dict[str, FieldInfo] = self.codec.fields(model)
        if "id" not in self.fields:
            raise ConfigurationError(f"{model.__name__} has no `id` field")
        self.id = self.fields["id"]
        self.by_stored: dict[str, FieldInfo] = {f.stored: f for f in self.fields.values()}
        self.deferred: tuple[str, ...] = tuple(f.stored for f in self.fields.values() if f.has(Deferred))
        self.immutable: frozenset[str] = frozenset(f.name for f in self.fields.values() if f.has(Immutable))
        self.refs: dict[str, FieldInfo] = {f.name: f for f in self.fields.values() if f.ref is not None}
        self.backrefs: dict[str, FieldInfo] = {
            f.name: f for f in self.fields.values() if f.backref is not None
        }
        self.stored_keys: frozenset[str] = frozenset(
            f.stored for f in self.fields.values() if not f.transient
        )

        for f in self.fields.values():
            if f.has(Deferred) and f.required:
                raise ConfigurationError(f"{model.__name__}.{f.name}: a Deferred field needs a default")

        # Discriminator hierarchy.
        self.disc_key: str | None = self.config.discriminator
        self.root: type = model
        if self.disc_key:
            for base in model.__mro__:
                cfg = _own_config(base)
                if cfg is not None and cfg.discriminator == self.disc_key:
                    self.root = base
            if self.disc_key in self.by_stored:
                raise ConfigurationError(
                    f"{model.__name__}: discriminator key {self.disc_key!r} collides with a field"
                )
        self.disc_value: str = self.config.discriminator_value or model.__name__
        self.collection: str = self.config.collection or self.root.__name__
        self._subclass_by_value: dict[str, type] = {}
        self._targets: dict[str, tuple[ModelMeta, ...]] = {}
        self._loose: dict[str, t.Any] = {}
        self._adapters: dict[t.Any, t.Any] = {}

        # Plugins: marker-activated first (in a stable order), then configured ones.
        plugins: list[Plugin] = []
        seen: set[t.Any] = set()
        for f in self.fields.values():
            for marker in f.markers:
                marker_cls = marker if isinstance(marker, type) else type(marker)
                factory = _MARKER_PLUGINS.get(marker_cls) or getattr(marker_cls, "plugin_factory", None)
                if factory is None or factory in seen:
                    continue
                seen.add(factory)
                plugin = (_load(factory) if isinstance(factory, str) else factory)(self)
                if plugin is not None:
                    plugins.append(plugin)
        plugins.extend(self.config.plugins)
        self.plugins: tuple[Plugin, ...] = tuple(plugins)
        for plugin in self.plugins:
            plugin.setup(self)

    # -- naming --------------------------------------------------------------

    def stored_path(self, path: str) -> str:
        """Translate an attribute path to a stored path; the first segment must be a field."""
        head, dot, rest = path.partition(".")
        info = self.fields.get(head)
        if info is None:
            if head in self.by_stored:
                return path
            if self.disc_key and head == self.disc_key:
                return path
            raise QueryError(f"{self.model.__name__} has no field {head!r}")
        if info.transient:
            raise QueryError(f"{self.model.__name__}.{head} is a reverse relation and is not stored")
        return info.stored + dot + rest

    def field_for(self, path: str) -> FieldInfo | None:
        head = path.partition(".")[0]
        return self.fields.get(head) or self.by_stored.get(head)

    # -- casting ---------------------------------------------------------------

    def cast(self, path: str, value: t.Any) -> t.Any:
        """Coerce a filter value to the type of the field at ``path`` and make it BSON-ready."""
        if value is None:
            return None
        info = self.fields.get(path) or self.by_stored.get(path)
        if info is None:
            # Nested path (or unknown): no type to cast against.
            self.stored_path(path)
            return to_bson(id_of(value))
        if info.ref is not None:
            return self._cast_ref(info, value)
        loose = self._loose.get(info.name)
        if loose is None:
            loose = self._loose[info.name] = loosen(info.type)
        try:
            return to_bson(self.validate_type(loose, value))
        except ValidationError as exc:
            elem = element_type(loose)
            if elem is not None and not isinstance(value, list | tuple | set | frozenset):
                try:
                    return to_bson(self.validate_type(elem, value))
                except ValidationError:
                    pass
            raise QueryError(f"cannot cast {value!r} to {self.model.__name__}.{info.name}: {exc}") from exc

    def _cast_ref(self, info: FieldInfo, value: t.Any) -> t.Any:
        if isinstance(value, list | tuple | set | frozenset):
            return [self._cast_ref(info, v) for v in value]
        value = id_of(value)
        if isinstance(value, str) and ObjectId.is_valid(value):
            targets = self.ref_targets(info.name)
            if all(id_python_type(m.model) is ObjectId for m in targets):
                return ObjectId(value)
        return to_bson(value)

    def validate_type(self, tp: t.Any, value: t.Any) -> t.Any:
        """Validate a value against a bare type with this model's backend."""
        if self.codec.name == "pydantic":
            import pydantic

            try:
                adapter = self._adapters.get(tp)
            except TypeError:  # unhashable annotation
                adapter = None
            if adapter is None:
                adapter = pydantic.TypeAdapter(tp, config=pydantic.ConfigDict(arbitrary_types_allowed=True))
                try:
                    self._adapters[tp] = adapter
                except TypeError:
                    pass
            try:
                return adapter.validate_python(value)
            except pydantic.ValidationError as exc:
                raise ValidationError(str(exc)) from exc
        import msgspec

        from mongospecs.msgspec import dec_hook

        try:
            return msgspec.convert(value, tp, dec_hook=dec_hook)
        except (msgspec.ValidationError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc

    # -- encode / decode -------------------------------------------------------

    def decode(self, doc: t.Any, *, trusted: bool | None = None) -> t.Any:
        if self.config.upcast is not None:
            doc = self.config.upcast(dict(doc))
        cls = self.model
        if self.disc_key:
            cls = self.subclass_for(doc.get(self.disc_key))
        return self.codec.decode(cls, doc, trusted=self.config.trusted if trusted is None else trusted)

    def encode(self, obj: t.Any) -> dict[str, t.Any]:
        doc = self.codec.encode(obj)
        if self.disc_key:
            doc[self.disc_key] = meta_of(type(obj)).disc_value
        return doc

    def subclass_for(self, value: t.Any) -> type:
        if value is None:
            return self.model
        hit = self._subclass_by_value.get(value)
        if hit is None:
            table = {meta_of(c).disc_value: c for c in (self.root, *_all_subclasses(self.root))}
            self._subclass_by_value = table
            hit = table.get(value)
        if hit is None or not issubclass(hit, self.model):
            return self.model
        return hit

    def discriminator_filter(self) -> dict[str, t.Any]:
        """Restrict a query on a subclass to documents of that subclass (and its own subclasses)."""
        if not self.disc_key or self.model is self.root:
            return {}
        values = [self.disc_value, *(meta_of(c).disc_value for c in _all_subclasses(self.model))]
        return {self.disc_key: values[0] if len(values) == 1 else {"$in": values}}

    # -- relations -------------------------------------------------------------

    def ref_targets(self, name: str) -> tuple[ModelMeta, ...]:
        hit = self._targets.get(name)
        if hit is None:
            info = self.fields[name]
            if info.ref is not None:
                raw: tuple[t.Any, ...] = info.ref.targets
            elif info.backref is not None:
                raw = (info.backref.model or _backref_model(info),)
            else:
                raise ConfigurationError(f"{self.model.__name__}.{name} is not a relation")
            hit = tuple(meta_of(resolve_model(target, self.model)) for target in raw)
            self._targets[name] = hit
        return hit

    # -- hooks and scopes ------------------------------------------------------

    def hooks(self, when: str, event: str) -> list[Hook]:
        out: list[Hook] = []
        for klass in reversed(self.model.__mro__):
            table = _HOOKS.get(klass)
            if table:
                out.extend(table.get((when, event), ()))
        return out

    def scope(self, name: str) -> t.Callable[..., t.Any] | None:
        for klass in self.model.__mro__:
            table = _SCOPES.get(klass)
            if table and name in table:
                return table[name]
        return None

    def __repr__(self) -> str:
        return f"<ModelMeta {self.model.__name__} -> {self.collection!r} ({self.codec.name})>"


def _backref_model(info: FieldInfo) -> t.Any:
    tp = strip_optional(info.type)
    elem = element_type(tp)
    target = strip_optional(elem) if elem is not None else tp
    if isinstance(target, t.ForwardRef):
        return target.__forward_arg__
    if isinstance(target, type) and target not in (int, list):
        return target
    raise ConfigurationError(f"Backref field {info.name!r}: name the model with Backref(..., model=Model)")


def resolve_model(target: t.Any, near: type) -> type:
    """Resolve a model class or class name to a document class."""
    from mongospecs.model import Document

    if isinstance(target, type):
        return target
    matches = [c for c in _all_subclasses(Document) if c.__name__ == target]
    if not matches:
        raise ConfigurationError(f"{near.__name__}: no document class named {target!r} has been defined")
    if len(matches) > 1 and near.__name__ == target:
        return near
    if len(matches) > 1:
        # Names are resolved against the registry the referring model is bound to.
        from mongospecs.registry import _BINDINGS

        registry = next((_BINDINGS[k] for k in near.__mro__ if k in _BINDINGS), None)
        bound = [c for c in matches if registry is not None and _BINDINGS.get(c) is registry]
        if len(bound) == 1:
            return bound[0]
        same_module = [c for c in (bound or matches) if c.__module__ == near.__module__]
        if len(same_module) == 1:
            return same_module[0]
        raise ConfigurationError(f"{near.__name__}: more than one document class is named {target!r}")
    return matches[0]


def meta_of(model: type) -> ModelMeta:
    """Return (building if needed) the metadata of a document class."""
    try:
        return _METAS[model]
    except KeyError:
        meta = ModelMeta(model)
        _METAS[model] = meta
        return meta
    except TypeError:
        raise ConfigurationError(f"{model!r} is not a mongospecs document class") from None


def id_python_type(model: type) -> t.Any:
    return strip_optional(meta_of(model).id.type)


def check_event(event: str) -> None:
    if event not in DOCUMENT_EVENTS:
        raise ConfigurationError(f"unknown document event {event!r}; expected one of {DOCUMENT_EVENTS}")


def add_hook(model: type, when: str, event: str, fn: Hook) -> None:
    check_event(event)
    _HOOKS.setdefault(model, {}).setdefault((when, event), []).append(fn)


def remove_hook(model: type, when: str, event: str, fn: Hook) -> None:
    hooks = _HOOKS.get(model, {}).get((when, event), [])
    if fn in hooks:
        hooks.remove(fn)


def add_scope(model: type, name: str, fn: t.Callable[..., t.Any]) -> None:
    _SCOPES.setdefault(model, {})[name] = fn
