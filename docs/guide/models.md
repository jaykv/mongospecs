# Models

A model is a class from the library you already use, with the `Spec` base of that backend.
Everything else in mongospecs is imported from `mongospecs` and is the same for all three.

```python
from datetime import datetime
from typing import Annotated

from mongospecs import OID, CreatedAt, Deferred, Immutable, Index, Policy, Registry, SpecConfig, UpdatedAt
from mongospecs.msgspec import Spec  # or: from mongospecs.pydantic import Spec / mongospecs.attrs


class User(Spec):
    __spec__ = SpecConfig(collection="users", indexes=[Index("email", unique=True)])

    email: Annotated[str, Immutable]
    name: str = ""
    password_hash: Annotated[str | None, Deferred, Policy(private=True)] = None
    created_at: Annotated[datetime | None, CreatedAt] = None
    updated_at: Annotated[datetime | None, UpdatedAt] = None
```

- Every document has an `id` attribute, stored as `_id`. Its type is `OID | None` unless you
  redeclare it (`id: str | None = None` for string ids).
- `__spec__` is assigned **without an annotation**. It holds the collection name (default: the
  class name), indexes, and per-model options.
- With attrs, decorate each model with `@attrs.define(kw_only=True)`.
- A field whose default is `None` is **not stored while it is `None`**: the key is left out of the
  document, and a document without the key reads back as `None`. Setting such a field to `None`
  removes the key (`$unset`). This is what makes `Index("email", unique=True, sparse=True)` work
  for documents without an email: MongoDB indexes an explicit `null`, so two stored nulls would
  collide. `Q.email == None` still finds them (it matches a missing key), and `Exists(Q.email)`
  means "has a value". A field with any other default stores `None` as `null`.
- A field may not be named after part of the document API (`count`, `find`, `get`, `update`,
  `delete`, `save`, `insert`, `scope`, `watch`, `changes`, `is_new`, ... the full list is
  `mongospecs.meta.RESERVED_FIELD_NAMES`); it would replace that method on the class. Doing so
  raises `ConfigurationError` when the model is first used. Rename the attribute and keep the
  stored key: `total: Annotated[int, Alias("count")] = 0`.

## Binding and lifecycle

Models do nothing until they are bound to an open registry. Constructing a registry does no I/O;
`async with` opens it; closing is final.

```python
registry = Registry("mongodb://localhost/app", models=[User])
await registry.open()
await registry.apply_indexes()

ann = await User(email="ann@example.com", name="Ann").insert()
assert isinstance(ann.id, OID.__origin__) and ann.created_at is not None
```

Operations on a registry that is not open fail immediately with `RegistryClosedError`; nothing is
queued.

## Field markers

Markers go in `typing.Annotated`, so one declaration works on every backend.

| Marker | Effect |
|---|---|
| `CreatedAt`, `UpdatedAt` | Timestamps on every write path, including bulk and filter-based updates |
| `DeletedAt` | Soft delete: `delete()` sets it, reads exclude documents where it is set |
| `Revision` | Optimistic locking; the value doubles as an HTTP ETag |
| `Immutable` | Cannot change after insert |
| `Deferred` | Not loaded unless asked for with `select("+name")` |
| `Alias("x")` | Stored under a shorter name (top-level fields) |
| `Policy(...)` | How the field may be used from outside (read by `mongospecs-web`) |
| `Backref(...)`, `OnDelete(...)`, `To(...)` | Relations, see [Relations](relations.md) |

```python
loaded = await User.get(ann.id)
assert loaded.password_hash is None  # Deferred: not fetched

with_hash = await User.find().select("+password_hash").one()
assert with_hash.email == "ann@example.com"
```

## Existing classes

`SpecAdapter` turns a class you already have into a document class without touching it.

```python
import msgspec

from mongospecs.msgspec import SpecAdapter


class Dragon(msgspec.Struct, kw_only=True):
    name: str
    breed: str = "unknown"


DragonDoc = SpecAdapter(Dragon, collection="dragons")
registry.bind(DragonDoc)
burt = await DragonDoc(name="Burt").insert()
assert isinstance(burt, Dragon) and burt.id is not None
```

## Single-collection inheritance

Set `discriminator` on the base; subclasses share the collection and are picked on read.

```python
class Animal(Spec):
    __spec__ = SpecConfig(collection="animals", discriminator="kind")
    name: str = ""


class Cat(Animal):
    lives: int = 9


registry.bind(Animal, Cat)
await Cat(name="Tom").insert()
(animal,) = await Animal.find()
assert type(animal) is Cat
```

## Documents written by older code

`upcast` runs on each raw document before it is decoded, so renamed or added fields can be
migrated lazily. Fields the model does not know are ignored on read and left alone on write.

```python
def upcast(doc):
    if "full_name" in doc:
        doc["name"] = doc.pop("full_name")
    return doc


class Person(Spec):
    __spec__ = SpecConfig(collection="people", upcast=upcast)
    name: str


registry.bind(Person)
await Person.collection().insert_one({"full_name": "Old Shape", "legacy_flag": True})
person = await Person.find().one()
assert person.name == "Old Shape"

await registry.close()
```
