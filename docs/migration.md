# Migrating from 0.1.x

0.2 is a rewrite. It is async-only, needs Python 3.11+ and MongoDB 7.0+, and its API is not
compatible with 0.1.x. The synchronous line stays available on the
[`0.1.x` branch](https://github.com/jaykv/mongospecs/tree/0.1.x) and as `mongospecs<0.2`.

What stayed: models on msgspec, pydantic or attrs; `from mongospecs.<backend> import Spec`; `Q`
expressions and `And`/`Or`/`Nor`/`In`/...; `SpecAdapter`.

## The shape of the change

```py
# 0.1.x
Spec._client = MongoClient("mongodb://localhost/app")
burt = Dragon(name="Burt")
burt.insert()
doc = Dragon.one(Q.name == "Burt")

# 0.2
async with Registry("mongodb://localhost/app", models=[Dragon]):
    burt = await Dragon(name="Burt").insert()
    doc = await Dragon.find(Q.name == "Burt").first()
```

## API map

| 0.1.x | 0.2 | Notes |
|---|---|---|
| `Spec._client = client` | `Registry(uri, models=[...])` | explicit lifecycle, no class-level client |
| `_collection = "dragons"` | `__spec__ = SpecConfig(collection="dragons")` | assigned without an annotation |
| `_id` / `id` depending on backend | `id` everywhere, stored as `_id` | |
| `Model.by_id(id)` | `await Model.get(id)` | |
| `Model.one(filter)` | `await Model.find(filter).first()` or `.one()` | `.one()` raises when nothing matches |
| `Model.many(filter)` | `await Model.find(filter)` | also `async for` |
| `Model.find(...)`, `find_one(...)` (raw dicts) | `Model.find(...).raw()` | |
| `Model.ids(filter)` | `await Model.find(filter).ids()` | |
| `Model.count(filter)` | `await Model.count(filter)` | always exact; `estimated_count()` is separate |
| `doc.update()` / `update("field")` | `await doc.save()` | sends only what changed |
| `doc.upsert()` | `await Model.upsert(filter, ...)` | one round trip |
| `doc.unset("field")` | `await Model.update(id, Unset("field"))` | |
| `Model.update_many(docs)` | `await Model.bulk_save(docs)` or `Model.find(...).update(...)` | |
| `Model.delete_many(docs)` | `await Model.find(Q.id.in_(ids)).delete()` | |
| projection with `$ref` | `.include("field")` | `Ref[T]` fields |
| projection with `$sub` / `SubSpec` | a nested model as the field type | decoded by the backend |
| `Model.cascade` / `nullify` / `pull` | `OnDelete("cascade" \| "nullify" \| "pull")` on the reference | |
| `Model.listen("insert", fn)` | `@Model.before("insert")` / `@Model.after(...)` | async, typed, no global signals |
| `soft_delete()` / `find_active()` | a `DeletedAt` field | scope applied everywhere |
| `with Model.transaction() as session` | `await registry.transaction(fn)` | retried, restores document state |
| `Model.with_options(...)` | `Model.using(read_preference=..., db=...)` | returns a manager, changes nothing globally |
| `Paginator(Model, ...)` | `await Model.find(...).page(size, after=...)` | cursor by default; `number=` for offsets |
| `create_index` / `drop_index` | `SpecConfig(indexes=[Index(...)])` + `registry.apply_indexes()` | |
| `to_json_type()` | your backend's own serialisation, or `mongospecs_web.view_of(Model).serialize(doc)` | |
| `Empty` | `None`, plus `Unset(...)` for removing a field | |

## Behaviour differences worth knowing

- Reads are validated. A stored document that does not fit the model raises `ValidationError`
  instead of producing a half-typed object. `SpecConfig(upcast=...)` migrates old shapes on read.
- Writes are validated too: `insert()` and `save()` refuse a document that does not fit its
  model. `validate=False` or `SpecConfig(validate_writes=False)` skips the check.
- Unknown field names in conditions and updates raise `QueryError`.
- Raw dict filters may not contain operators unless wrapped in `trusted(...)`.
- Reference fields hold a `Ref` object (`.id`, `.doc`), not a bare id or a document.
- msgspec is no longer installed implicitly: pick an extra (`mongospecs[msgspec]`, `[pydantic]`,
  `[attrs]`).
- Documents compare with their backend's own equality, not by id.

Every 0.1.x test is accounted for in the [v1 test inventory](v1-test-inventory.md).
