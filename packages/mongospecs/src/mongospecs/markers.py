"""Declarative metadata: field markers, field policy, model config and indexes.

Everything here is plain data. Markers are placed in ``typing.Annotated`` so the
same declaration works on msgspec, pydantic and attrs models::

    title: Annotated[str, Policy(filter=("eq", "prefix"), sort=True)]
    created_at: Annotated[datetime | None, CreatedAt] = None
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass, field

__all__ = [
    "FILTER_OPERATORS",
    "Alias",
    "Backref",
    "CreatedAt",
    "Deferred",
    "DeletedAt",
    "Immutable",
    "Index",
    "Marker",
    "OnDelete",
    "Policy",
    "Revision",
    "SpecConfig",
    "To",
    "UpdatedAt",
    "computed",
]

FILTER_OPERATORS: tuple[str, ...] = ("eq", "ne", "in", "nin", "gt", "gte", "lt", "lte", "exists", "prefix")
"""The bounded operator set a web filter may use."""


class Marker:
    """Base class for field markers. Markers may be used bare (the class) or instantiated."""


class CreatedAt(Marker):
    """Set once, when the document is inserted."""


class UpdatedAt(Marker):
    """Set on insert and on every update, including bulk and filter-based ones."""


class DeletedAt(Marker):
    """Soft delete: ``delete`` sets this field and reads exclude documents where it is set."""


class Revision(Marker):
    """Integer revision for optimistic locking; incremented by every write."""


class Immutable(Marker):
    """The field cannot change after insert."""


class Deferred(Marker):
    """The field is left out of the default projection; ask for it with ``select("+name")``."""


@dataclass(frozen=True, slots=True)
class Alias(Marker):
    """Store a top-level field under a different (usually shorter) name."""

    name: str


@dataclass(frozen=True, slots=True)
class To(Marker):
    """Name the target model of a ``Ref`` when it cannot be imported (resolved by class name)."""

    model: str


@dataclass(frozen=True, slots=True)
class Backref(Marker):
    """A reverse relation: documents of another model whose ``field`` references this one.

    Never stored. Loaded only through ``include()``. With ``count=True`` the
    field holds the number of referencing documents instead of the documents.
    """

    field: str
    model: t.Any = None
    count: bool = False


@dataclass(frozen=True, slots=True)
class OnDelete(Marker):
    """What happens to documents holding this ``Ref`` when the referenced document is deleted.

    ``cascade`` deletes them, ``nullify`` sets the field to ``None``, ``pull``
    removes the id from a list field.
    """

    action: t.Literal["cascade", "nullify", "pull"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Policy(Marker):
    """How a field may be used from outside the application (the web layer reads this).

    ``filter`` lists the operators a client may apply (``True`` allows the whole
    bounded set, see :data:`FILTER_OPERATORS`).
    """

    insert: bool = True
    update: bool = True
    private: bool = False
    filter: tuple[str, ...] | bool = ()
    sort: bool = False

    def __post_init__(self) -> None:
        ops = self.filter_ops
        unknown = [op for op in ops if op not in FILTER_OPERATORS]
        if unknown:
            raise ValueError(f"unknown filter operators {unknown}; allowed: {list(FILTER_OPERATORS)}")

    @property
    def filter_ops(self) -> tuple[str, ...]:
        if self.filter is True:
            return FILTER_OPERATORS
        if self.filter is False:
            return ()
        return tuple(self.filter)


DEFAULT_POLICY = Policy()


class computed(property):
    """A read-only property that the web layer includes in the Read view."""


def is_computed(prop: t.Any) -> bool:
    return isinstance(prop, computed)


IndexKey = tuple[str, int | str]


@dataclass(frozen=True, init=False)
class Index:
    """A declared index.

    Keys are field names (``"-name"`` for descending) or ``(name, direction)``
    pairs where direction is ``1``, ``-1`` or a special type such as ``"text"``
    or ``"2dsphere"``. Field names are attribute names; they are translated to
    stored names when the index is planned.
    """

    keys: tuple[IndexKey, ...]
    name: str | None = None
    unique: bool = False
    sparse: bool = False
    ttl: int | None = None
    partial: t.Mapping[str, t.Any] | None = None
    collation: t.Mapping[str, t.Any] | None = None
    hidden: bool = False
    options: t.Mapping[str, t.Any] = field(default_factory=dict)

    def __init__(
        self,
        *keys: str | IndexKey,
        name: str | None = None,
        unique: bool = False,
        sparse: bool = False,
        ttl: int | None = None,
        partial: t.Mapping[str, t.Any] | None = None,
        collation: t.Mapping[str, t.Any] | None = None,
        hidden: bool = False,
        **options: t.Any,
    ) -> None:
        if not keys:
            raise ValueError("an index needs at least one key")
        parsed: list[IndexKey] = []
        for key in keys:
            if isinstance(key, str):
                parsed.append((key[1:], -1) if key.startswith("-") else (key, 1))
            else:
                parsed.append((key[0], key[1]))
        object.__setattr__(self, "keys", tuple(parsed))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "unique", unique)
        object.__setattr__(self, "sparse", sparse)
        object.__setattr__(self, "ttl", ttl)
        object.__setattr__(self, "partial", partial)
        object.__setattr__(self, "collation", collation)
        object.__setattr__(self, "hidden", hidden)
        object.__setattr__(self, "options", dict(options))

    def __hash__(self) -> int:
        return hash((self.keys, self.name, self.unique))


@dataclass(frozen=True, kw_only=True)
class SpecConfig:
    """Per-model configuration, assigned to the ``__spec__`` class attribute."""

    collection: str | None = None
    """Collection name. Defaults to the class name."""

    indexes: t.Sequence[Index] = ()

    plugins: t.Sequence[t.Any] = ()
    """Extra :class:`~mongospecs.hooks.Plugin` instances for this model."""

    discriminator: str | None = None
    """Stored key that selects the subclass; set on the base of a single-collection hierarchy."""

    discriminator_value: str | None = None
    """Value stored for this class. Defaults to the class name."""

    shard_key: t.Sequence[str] = ()
    """Field names forming the shard key; added to the filter of document-level writes."""

    upcast: t.Callable[[dict[str, t.Any]], dict[str, t.Any]] | None = None
    """Called with each raw document before decoding, to migrate old shapes lazily."""

    trusted: bool = False
    """Skip read validation where the backend allows it."""

    max_time_ms: int | None = None
    read_preference: t.Any = None
    read_concern: t.Any = None
    write_concern: t.Any = None
    collation: t.Mapping[str, t.Any] | None = None

    collection_options: t.Mapping[str, t.Any] = field(default_factory=dict)
    """Options for ``create_collection`` (``timeseries``, ``capped``, ``size``, ...)."""

    json_schema_validation: t.Literal["off", "moderate", "strict"] = "off"
    """Install a derived ``$jsonSchema`` validator when indexes and options are applied."""
