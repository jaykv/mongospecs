# Writes

```python
from datetime import datetime
from typing import Annotated

from mongospecs import (
    CreatedAt,
    DeletedAt,
    DocumentNotFound,
    Inc,
    Push,
    Q,
    Registry,
    Revision,
    SetOnInsert,
    SpecConfig,
    StaleDocumentError,
    UpdatedAt,
    UpdateOne,
)
from mongospecs.msgspec import Spec


class Article(Spec):
    __spec__ = SpecConfig(collection="articles")
    title: str = ""
    views: int = 0
    tags: list[str] = []
    created_at: Annotated[datetime | None, CreatedAt] = None
    updated_at: Annotated[datetime | None, UpdatedAt] = None
    deleted_at: Annotated[datetime | None, DeletedAt] = None
    rev: Annotated[int, Revision] = 0


registry = await Registry("mongodb://localhost/app", models=[Article]).open()
```

## Change tracking

A document remembers what it looked like when it was loaded. `save()` sends only the difference;
an unchanged document sends nothing.

```python
article = await Article(title="Draft").insert()
article.title = "Final"
article.tags.append("news")
assert article.changes() == {"$set": {"title": "Final", "tags": ["news"]}}
await article.save()
assert article.changes() == {} and article.rev == 1
```

A document you did not get from a tracked read has no snapshot: a `lean()` result, a
`copy.copy()` or a deep copy of a tracked document, or one that was unpickled. Saving one tries to
insert it and fails with a `DuplicateKeyError` that says so. Use a tracked read, or
`Model.hydrate(raw)` to adopt a raw document.

Nested documents are diffed into dotted paths. A changed list is sent whole; for element-level
changes that must not overwrite concurrent writers, use the atomic operators below.

## Atomic updates

`Model.update` changes one document in a single round trip and returns it. Every supplied field
is validated; unknown and immutable fields raise.

```python
updated = await Article.update(article.id, Inc(views=1), Push(tags="featured"), title="Final v2")
assert updated.views == 1 and updated.tags == ["news", "featured"] and updated.rev == 2

assert await Article.find(Q.views >= 1).update(Inc(views=10)) == 1  # every match, no documents loaded

created = await Article.upsert(Q.title == "Landing", Inc(views=1), SetOnInsert(tags=["page"]))
assert created.views == 1 and created.created_at is not None
```

Operators: `Set`, `Unset`, `Inc`, `Mul`, `Min`, `Max`, `Push`, `AddToSet`, `Pull`, `Pop`,
`SetOnInsert`, `CurrentDate`; `Each([...])` for several values.

## Optimistic locking

With a `Revision` field, saving a stale copy fails instead of overwriting.

```python
stale = article  # loaded at rev 1, the database is now at rev 3
stale.title = "Overwrite?"
try:
    await stale.save()
except StaleDocumentError:
    await stale.reload()
assert stale.title == "Final v2"
```

A document created by `upsert()` starts at revision 1, one created by `insert()` at 0: an upsert
is an update, and every update increments the revision.

## Soft delete

With a `DeletedAt` field, `delete()` hides the document instead of removing it.

```python
await stale.delete()
assert await Article.get(stale.id) is None
assert await Article.find().unscoped("soft_delete").count() == 2
await Article.find(Q.id == stale.id).unscoped("soft_delete").update(deleted_at=None)  # restore
await (await Article.get(stale.id)).delete(hard=True)  # really remove
```

## Bulk

```python
result = await Article.insert_many([Article(title=f"bulk {i}") for i in range(3)])
assert len(result.inserted) == 3 and not result.errors

docs = await Article.find(Q.title.prefix("bulk")).all()
for doc in docs:
    doc.views += 5
assert await Article.bulk_save(docs) == 3  # one round trip, deltas only

await Article.bulk_write([UpdateOne(Q.title == "bulk 0", Inc(views=1))])
assert (await Article.find(Q.title == "bulk 0").one()).views == 6
```

`insert_many(..., ordered=False)` attempts every document and reports failures per item instead of
raising on the first.

The writes in a `bulk_save` are independent. If some fail (a duplicate key, a revision that moved
on), the rest are still written and are in sync in memory; the error raised afterwards lists the
documents that were not written in `.failed`.

`Model.find(...).update(...)` returns the number of documents modified, as an `int` that also
carries `.matched` and `.modified`.

## Transactions

`registry.transaction(fn)` commits when `fn` returns. The driver re-runs `fn` on transient errors,
so it must be safe to call twice. If the transaction aborts, documents written inside it are put
back to their previous in-memory state.

**Transactions do not nest.** MongoDB has no savepoints: a transaction started inside another is
a separate one that commits on its own and stays committed if the outer one aborts. Calling
`registry.transaction()` inside a running one raises `TransactionError`; pass the outer `tx` to
the code that needs it. When a separate commit is what you want (an audit record that must
survive a rollback), say so: `registry.transaction(fn, independent=True)`.

```python
landing = await Article.find(Q.title == "Landing").one()


async def promote(tx):
    landing.tags.append("promoted")
    await landing.save(session=tx)
    raise RuntimeError("changed my mind")


try:
    await registry.transaction(promote)
except RuntimeError:
    pass

assert (await Article.get(landing.id)).tags == ["page"]  # rolled back in the database
assert landing.rev == 1 and landing.changes() == {"$set": {"tags": ["page", "promoted"]}}  # and in memory
```

Sessions are always explicit: pass `session=tx` to every operation inside the function. For manual
control use `async with registry.session() as s, s.start_transaction(): ...` (no automatic retry).

```python
try:
    await Article.update(landing.id, title=123)
except Exception as exc:
    assert type(exc).__name__ == "ValidationError"

try:
    await Article.update("0" * 24, title="nobody")
except DocumentNotFound:
    pass

await registry.client.drop_database("app")
await registry.close()
```
