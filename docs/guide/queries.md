# Queries

Queries are immutable and chainable. Nothing is sent until you await a terminal method or iterate.

```python
from typing import Annotated

from mongospecs import Deferred, Q, QueryError, Registry, SpecConfig, trusted
from mongospecs.msgspec import Spec


class Article(Spec):
    __spec__ = SpecConfig(collection="articles")
    title: str = ""
    views: int = 0
    tags: list[str] = []
    body: Annotated[str, Deferred] = ""


registry = await Registry("mongodb://localhost/app", models=[Article]).open()
await Article.insert_many([Article(title=f"post {i}", views=i * 10, tags=["a"] if i % 2 else ["b"]) for i in range(10)])
```

## Conditions

`Q.field` builds conditions. Field names are checked against the model and values are cast to the
field's type, so a typo is an error instead of a query that silently matches nothing.

```python
popular = await Article.find(Q.views >= 50, Q.title.prefix("post")).sort("-views").limit(3)
assert [a.views for a in popular] == [90, 80, 70]

either = await Article.find((Q.views < 10) | (Q.views > 80)).sort("views").all()
assert [a.views for a in either] == [0, 90]

assert await Article.count(Q.tags == "a") == 5  # a scalar against a list field means "contains"
assert await Article.exists(~Q.title.prefix("post")) is False

try:
    Article.find(Q.titel == "x").compile()
except QueryError as exc:
    assert "no field 'titel'" in str(exc)
```

Operators: `==  !=  >  >=  <  <=`, `.in_()`, `.nin()`, `.all()`, `.exists()`, `.size()`,
`.prefix()`, `.contains()`, `.regex()`, `.elem_match()`, combined with `&`, `|`, `~` or
`And`/`Or`/`Nor`.

Plain dicts work for equality. Anything containing a MongoDB operator must be wrapped in
`trusted()`, which marks it as written by you rather than built from user input.

```python
assert await Article.count({"title": "post 3"}) == 1
assert await Article.count(trusted({"views": {"$mod": [20, 0]}})) == 5
```

## Results

```python
first = await Article.find(Q.views > 0).sort("views").first()  # a document, or None
one = await Article.find(Q.title == "post 4").one()  # raises DocumentNotFound if missing
by_id = await Article.get(one.id)
assert first.views == 10 and by_id.title == "post 4"

async for article in Article.find().sort("-views").limit(2):
    assert article.views >= 80

titles = [a["title"] for a in await Article.find().raw().limit(2)]  # plain dicts
```

| Mode | Returns | Tracked for `save()` |
|---|---|---|
| default | model instances | yes |
| `.lean()` | model instances | no (read-only) |
| `.raw()` | dicts as stored | no |

## Projection

`select("a", "b")` includes, `select("-a")` excludes, `select("+a")` adds a `Deferred` field.
A document loaded with a projection only saves the fields that were loaded.

```python
partial = await Article.find().select("title").first()
assert partial.title and partial.views == 0  # not loaded: the default, not the stored value
```

## Pagination

Cursor pagination is the default and stays stable while documents are inserted. Offset pagination
and totals are available when you need them.

Cursor pages are sorted by your keys and then by `_id`, so that the order is total. Give the query
an index that ends in the id (`Index("views", "id")`); with an index on the key alone MongoDB has
to sort in memory.

```python
page = await Article.find().sort("-views").page(4)
assert [a.views for a in page] == [90, 80, 70, 60] and page.has_next

following = await Article.find().sort("-views").page(4, after=page.next_cursor)
assert [a.views for a in following] == [50, 40, 30, 20]

third = await Article.find().sort("views").page(4, number=3, total=True)
assert [a.views for a in third] == [80, 90] and third.total == 10
```

## Named scopes

```python
@Article.scope
def trending(query, minimum=50):
    return query.filter(Q.views >= minimum)


assert await Article.find().trending(80).count() == 2
```

## Escape hatches

`Model.aggregate(pipeline, as_=...)` runs a raw pipeline (scopes still apply) and
`Model.collection()` returns the driver collection.

```python
rows = await Article.aggregate([{"$group": {"_id": None, "views": {"$sum": "$views"}}}])
assert rows[0]["views"] == 450

await registry.client.drop_database("app")
await registry.close()
```
