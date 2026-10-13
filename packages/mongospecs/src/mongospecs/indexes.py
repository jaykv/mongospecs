"""Index reconciliation.

Nothing is built implicitly. ``plan_indexes`` compares declared indexes with
what exists and returns typed actions; ``apply_indexes`` carries them out, and
only performs destructive ones (drop, recreate) when ``drop=True``.
"""

from __future__ import annotations

import enum
import typing as t
from dataclasses import dataclass, field

from mongospecs.errors import wrap_errors
from mongospecs.markers import Index
from mongospecs.meta import meta_of
from mongospecs.refs import OID_PATTERN

if t.TYPE_CHECKING:
    from mongospecs.manager import Manager
    from mongospecs.registry import Registry

__all__ = ["Action", "IndexAction", "IndexPlan", "apply_indexes", "mongo_json_schema", "plan_indexes"]

_IGNORED_OPTIONS = frozenset(
    {
        "v",
        "key",
        "ns",
        "background",
        "weights",
        "default_language",
        "language_override",
        "textIndexVersion",
        "2dsphereIndexVersion",
    }
)


class Action(enum.StrEnum):
    CREATE = "create"
    """Declared, missing in the database."""
    RETAIN = "retain"
    """Declared and present with the same definition."""
    RECREATE = "recreate"
    """Same name, different definition. Destructive."""
    CONFLICT = "conflict"
    """An index on the same keys exists under another name; it must be dropped first. Destructive."""
    DROP = "drop"
    """Present in the database, not declared. Destructive."""


@dataclass(frozen=True, slots=True)
class IndexAction:
    action: Action
    model: type
    collection: str
    name: str
    keys: tuple[tuple[str, t.Any], ...] = ()
    options: t.Mapping[str, t.Any] = field(default_factory=dict)
    existing: str | None = None
    """Name of the existing index involved, for CONFLICT."""

    @property
    def destructive(self) -> bool:
        return self.action in (Action.RECREATE, Action.CONFLICT, Action.DROP)

    def __str__(self) -> str:
        keys = ", ".join(f"{k}:{d}" for k, d in self.keys)
        return f"{self.action.value.upper():8} {self.collection}.{self.name} ({keys})"


@dataclass(slots=True)
class IndexPlan:
    actions: list[IndexAction] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    """Non-blocking findings, such as a reverse relation whose foreign key has no index."""

    def __iter__(self) -> t.Iterator[IndexAction]:
        return iter(self.actions)

    def of(self, *kinds: Action) -> list[IndexAction]:
        return [a for a in self.actions if a.action in kinds]

    @property
    def in_sync(self) -> bool:
        return all(a.action is Action.RETAIN for a in self.actions)


def _default_name(keys: t.Sequence[tuple[str, t.Any]]) -> str:
    return "_".join(f"{key}_{direction}" for key, direction in keys)


def _declared(manager: Manager[t.Any]) -> list[tuple[str, tuple[tuple[str, t.Any], ...], dict[str, t.Any]]]:
    meta = manager.meta
    indexes: list[Index] = [*meta.config.indexes]
    for plugin in manager.plugins:
        indexes.extend(plugin.indexes(meta))
    out = []
    for index in indexes:
        keys = tuple((meta.stored_path(name), direction) for name, direction in index.keys)
        options: dict[str, t.Any] = dict(index.options)
        if index.unique:
            options["unique"] = True
        if index.sparse:
            options["sparse"] = True
        if index.hidden:
            options["hidden"] = True
        if index.ttl is not None:
            options["expireAfterSeconds"] = index.ttl
        if index.partial is not None:
            options["partialFilterExpression"] = dict(index.partial)
        if index.collation is not None:
            options["collation"] = dict(index.collation)
        out.append((index.name or _default_name(keys), keys, options))
    return out


def _key_signature(
    keys: t.Iterable[tuple[str, t.Any]], weights: t.Mapping[str, t.Any] | None = None
) -> t.Any:
    """Comparable form of an index key. A materialised text index (``_fts``/``_ftsx``) is
    compared by its weighted fields, so it equals the declaration that created it."""
    plain: list[tuple[str, t.Any]] = []
    text: set[str] = set(weights or ())
    for key, direction in keys:
        if key in ("_fts", "_ftsx"):
            continue
        if direction == "text":
            text.add(key)
        else:
            plain.append((key, int(direction) if isinstance(direction, int | float) else direction))
    return (tuple(plain), frozenset(text))


def _same_options(declared: t.Mapping[str, t.Any], existing: t.Mapping[str, t.Any]) -> bool:
    have = {k: v for k, v in existing.items() if k not in _IGNORED_OPTIONS}
    want = dict(declared)
    if set(have) != set(want):
        return False
    for key, value in want.items():
        other = have[key]
        if key == "collation":
            # The server fills in collation defaults; compare what was declared.
            if any(other.get(k) != v for k, v in value.items()):
                return False
        elif isinstance(value, int | float) and isinstance(other, int | float):
            if float(value) != float(other):
                return False
        elif value != other:
            return False
    return True


async def plan_indexes(registry: Registry, *models: type) -> IndexPlan:
    """Compare declared and existing indexes for the given models (default: every bound model)."""
    plan = IndexPlan()
    targets = models or registry.models
    seen: set[tuple[str | None, str]] = set()
    for model in targets:
        manager: Manager[t.Any] = registry.manager(model)
        key = (manager.db_name, manager.meta.collection)
        declared = _declared(manager)
        with wrap_errors():
            existing = await manager.collection.index_information()
        existing.pop("_id_", None)
        signatures = {
            name: _key_signature(info["key"], info.get("weights")) for name, info in existing.items()
        }
        matched: set[str] = set()
        for name, keys, options in declared:
            sig = _key_signature(keys)
            if name in existing:
                matched.add(name)
                same = signatures[name] == sig and _same_options(options, existing[name])
                plan.actions.append(
                    IndexAction(
                        Action.RETAIN if same else Action.RECREATE, model, key[1], name, keys, options
                    )
                )
                continue
            twin = next(
                (
                    n
                    for n, s in signatures.items()
                    if s == sig and n not in matched and _same_options(options, existing[n])
                ),
                None,
            )
            if twin is not None and not sig[1]:
                matched.add(twin)
                plan.actions.append(
                    IndexAction(Action.CONFLICT, model, key[1], name, keys, options, existing=twin)
                )
            elif twin is not None:
                # A text index named by the server: same definition, keep it.
                matched.add(twin)
                plan.actions.append(IndexAction(Action.RETAIN, model, key[1], twin, keys, options))
            else:
                plan.actions.append(IndexAction(Action.CREATE, model, key[1], name, keys, options))
        if key not in seen:
            # Indexes of other models sharing the collection (discriminators) are theirs to declare.
            shared = {
                n
                for other in targets
                if other is not model and registry.manager(other).meta.collection == key[1]
                for n, _, _ in _declared(registry.manager(other))
            }
            for name, info in existing.items():
                if name not in matched and name not in shared:
                    plan.actions.append(
                        IndexAction(Action.DROP, model, key[1], name, tuple(tuple(k) for k in info["key"]))
                    )
        seen.add(key)

        # Reverse relations are served by a query on the foreign key: it should be indexed.
        for name, info in manager.meta.backrefs.items():
            assert info.backref is not None
            (target_meta,) = manager.meta.ref_targets(name)
            fk = target_meta.stored_path(info.backref.field)
            target_declared = _declared(registry.manager(target_meta.model))
            if not any(keys and keys[0][0] == fk for _, keys, _ in target_declared):
                plan.advisories.append(
                    f"{model.__name__}.{name} is loaded by querying {target_meta.model.__name__}.{info.backref.field}, "
                    f"which has no index; declare Index({info.backref.field!r}) on {target_meta.model.__name__}"
                )
    return plan


async def apply_indexes(
    registry: Registry,
    plan: IndexPlan | None = None,
    *,
    drop: bool = False,
) -> list[IndexAction]:
    """Carry out a plan. Destructive actions run only with ``drop=True``. Returns what was done.

    Also creates collections that declare ``collection_options`` and installs
    ``$jsonSchema`` validators for models that opted in.
    """
    await _ensure_collections(registry)
    if plan is None:
        plan = await plan_indexes(registry)
    done: list[IndexAction] = []
    for action in plan.actions:
        if action.action is Action.RETAIN or (action.destructive and not drop):
            continue
        collection = registry.manager(action.model).collection
        with wrap_errors():
            if action.action is Action.DROP:
                await collection.drop_index(action.name)
            else:
                if action.action is Action.RECREATE:
                    await collection.drop_index(action.name)
                elif action.action is Action.CONFLICT and action.existing:
                    await collection.drop_index(action.existing)
                await collection.create_index(list(action.keys), name=action.name, **action.options)
        done.append(action)
    return done


async def _ensure_collections(registry: Registry) -> None:
    for model in registry.models:
        meta = meta_of(model)
        options = dict(meta.config.collection_options)
        level = meta.config.json_schema_validation
        if not options and level == "off":
            continue
        manager: Manager[t.Any] = registry.manager(model)
        db = registry.database(manager.db_name)
        with wrap_errors():
            exists = meta.collection in await db.list_collection_names()
            if not exists:
                await db.create_collection(meta.collection, **options)
            if level != "off":
                await db.command(
                    "collMod",
                    meta.collection,
                    validator={"$jsonSchema": mongo_json_schema(model)},
                    validationLevel=level,
                )


# -- JSON Schema -> MongoDB $jsonSchema ---------------------------------------------

_BSON_TYPES: dict[str, t.Any] = {
    "string": "string",
    "boolean": "bool",
    "object": "object",
    "array": "array",
    "null": "null",
    "integer": ["int", "long"],
    "number": ["double", "int", "long", "decimal"],
}
_KEPT = frozenset(
    {
        "enum",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "required",
        "title",
        "description",
        "additionalProperties",
    }
)


def _convert(node: t.Any, defs: t.Mapping[str, t.Any], depth: int = 0) -> t.Any:
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if depth > 8:
            return {}
        return _convert(defs.get(ref.rsplit("/", 1)[-1], {}), defs, depth + 1)
    if node.get("type") == "string":
        if node.get("pattern") == OID_PATTERN:
            return {"bsonType": "objectId"}
        fmt = node.get("format")
        if fmt == "date-time":
            return {"bsonType": "date"}
        if fmt == "uuid" or node.get("contentEncoding") == "base64" or fmt == "binary":
            return {"bsonType": "binData"}
    out: dict[str, t.Any] = {}
    for key, value in node.items():
        if key == "type":
            types = value if isinstance(value, list) else [value]
            flat: list[t.Any] = []
            for item in types:
                mapped = _BSON_TYPES.get(item, item)
                flat.extend(mapped if isinstance(mapped, list) else [mapped])
            out["bsonType"] = flat[0] if len(flat) == 1 else flat
        elif key == "const":
            out["enum"] = [value]
        elif key in ("anyOf", "oneOf", "allOf"):
            out[key] = [_convert(v, defs, depth + 1) for v in value]
        elif key == "not" or key == "items":
            out[key] = _convert(value, defs, depth + 1)
        elif key == "properties":
            out[key] = {name: _convert(v, defs, depth + 1) for name, v in value.items()}
        elif key in _KEPT:
            if key == "additionalProperties" and isinstance(value, dict):
                out[key] = _convert(value, defs, depth + 1)
            elif key in ("exclusiveMinimum", "exclusiveMaximum") and not isinstance(value, bool):
                # Draft 4 (which MongoDB implements) spells these as a bound plus a flag.
                out["minimum" if key == "exclusiveMinimum" else "maximum"] = value
                out[key] = True
            else:
                out[key] = value
    return out


def mongo_json_schema(model: type) -> dict[str, t.Any]:
    """A MongoDB ``$jsonSchema`` validator derived from the model, keyed by stored names."""
    meta = meta_of(model)
    schema = meta.codec.json_schema(model)
    defs = schema.get("$defs", {})
    converted = _convert({k: v for k, v in schema.items() if k != "$defs"}, defs)
    stored = {f.name: f.stored for f in meta.fields.values()}
    transient = {f.name for f in meta.fields.values() if f.transient}
    props = {stored.get(k, k): v for k, v in converted.get("properties", {}).items() if k not in transient}
    props.pop("_id", None)
    converted["properties"] = props
    required = [stored.get(k, k) for k in converted.get("required", []) if k not in transient and k != "id"]
    if required:
        converted["required"] = required
    else:
        converted.pop("required", None)
    converted["bsonType"] = "object"
    converted.pop("additionalProperties", None)
    return t.cast("dict[str, t.Any]", converted)
