# Relations

```python
from typing import Annotated, Any

from mongospecs import Backref, Index, NotLoadedError, OnDelete, Q, Ref, Registry, SpecConfig
from mongospecs.msgspec import Spec


class Author(Spec):
    __spec__ = SpecConfig(collection="authors")
    name: str = ""
    books: Annotated[list[Any], Backref("author", model="Book")] = []
    book_count: Annotated[int, Backref("author", model="Book", count=True)] = 0


class Book(Spec):
    __spec__ = SpecConfig(collection="books", indexes=[Index("author")])
    title: str = ""
    author: Annotated[Ref[Author] | None, OnDelete("cascade")] = None
    editors: list[Ref[Author]] = []


registry = await Registry("mongodb://localhost/app", models=[Author, Book]).open()
ann = await Author(name="Ann").insert()
bob = await Author(name="Bob").insert()
await Book.insert_many([Book(title="One", author=ann, editors=[bob]), Book(title="Two", author=ann)])
```

## References

`Ref[T]` is stored as the target's id. It always has `.id`; the document is available after
`include()` or `fetch()`.

```python
book = await Book.find(Q.title == "One").one()
assert book.author.id == ann.id and not book.author.loaded
try:
    book.author.doc
except NotLoadedError:
    pass
assert (await book.author.fetch()).name == "Ann"

assert await Book.count(Q.author == ann) == 2  # documents and ids both work in conditions
```

## include()

```python
books = await Book.find().sort("title").include("author").include("editors")
assert books[0].author.doc.name == "Ann"
assert [e.doc.name for e in books[0].editors] == ["Bob"]
```

Options per relation: `match`, `select`, `sort`, `limit` (overall), `per_parent_limit`, and nested
paths such as `include("author.books")`.

`include()` can be served by one extra query per relation (`strategy="batch"`, the default) or by a
`$lookup` stage in a single pipeline (`strategy="lookup"`). Both return the same result; which is
faster depends on fan-out and topology, see [Benchmarks](../benchmarks.md).

```python
same = await Book.find().sort("title").include("author", strategy="lookup")
assert same[0].author.doc.name == "Ann"
```

## Reverse relations

A `Backref` field is never stored. It is filled only when included; with `count=True` it holds the
number of referencing documents.

```python
author = await Author.find(Q.name == "Ann").include("books", sort=["title"]).include("book_count").one()
assert [b.title for b in author.books] == ["One", "Two"] and author.book_count == 2
```

## Cascades

`OnDelete` on a reference says what happens to the documents holding it when the target is
deleted: `"cascade"` deletes them, `"nullify"` clears the field, `"pull"` removes the id from a
list. The delete and its cascades run in one transaction.

```python
await ann.delete()
assert await Book.count() == 0

await registry.client.drop_database("app")
await registry.close()
```

Also available: polymorphic references (`Ref[Post | Video]`), `dict[str, Ref[T]]`,
`Model.find(...).graph("parent")` for `$graphLookup` over a self-reference, and
`a.find(...).union(b.find(...))` for `$unionWith` across collections.
