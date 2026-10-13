"""The same models declared on each backend.

No ``from __future__ import annotations`` here: the classes are created inside a
function, so their annotations must be real objects.
"""

import datetime as dt
import enum
import functools
import types
import typing as t

from mongospecs import (
    OID,
    Alias,
    Backref,
    CreatedAt,
    Deferred,
    DeletedAt,
    Immutable,
    Index,
    OnDelete,
    Policy,
    Ref,
    Revision,
    SpecConfig,
    To,
    UpdatedAt,
)

BACKENDS = ("msgspec", "pydantic", "attrs")


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


@functools.cache
def build(backend: str) -> types.SimpleNamespace:
    if backend == "msgspec":
        import msgspec

        from mongospecs.msgspec import Spec, SpecAdapter

        define = lambda cls: cls  # noqa: E731

        class Address(msgspec.Struct, kw_only=True):
            city: str = ""
            zip: str = ""

        class Plain(msgspec.Struct, kw_only=True):
            name: str
            legs: int = 4

    elif backend == "pydantic":
        import pydantic

        from mongospecs.pydantic import Spec, SpecAdapter

        define = lambda cls: cls  # noqa: E731

        class Address(pydantic.BaseModel):
            city: str = ""
            zip: str = ""

        class Plain(pydantic.BaseModel):
            name: str
            legs: int = 4

    else:
        import attrs

        from mongospecs.attrs import Spec, SpecAdapter

        define = attrs.define(kw_only=True)

        @attrs.define(kw_only=True)
        class Address:
            city: str = ""
            zip: str = ""

        @attrs.define(kw_only=True)
        class Plain:
            name: str
            legs: int = 4

    @define
    class User(Spec):
        __spec__ = SpecConfig(collection="users", indexes=[Index("email", unique=True, sparse=True)])
        name: t.Annotated[str, Policy(filter=("eq", "prefix"), sort=True)] = ""
        email: str | None = None
        age: int = 0
        secret: t.Annotated[str | None, Deferred, Policy(private=True)] = None
        address: Address | None = None
        color: Color | None = None

    @define
    class Article(Spec):
        __spec__ = SpecConfig(collection="articles", indexes=[Index("title"), Index("-views", "title")])
        title: t.Annotated[str, Policy(filter=True, sort=True)] = ""
        slug: t.Annotated[str | None, Immutable] = None
        views: int = 0
        tags: list[str] = []
        author: t.Annotated[Ref[User] | None, OnDelete("nullify")] = None
        editors: t.Annotated[list[Ref[User]], OnDelete("pull")] = []
        reviewers: dict[str, Ref[User]] = {}
        created_at: t.Annotated[dt.datetime | None, CreatedAt] = None
        updated_at: t.Annotated[dt.datetime | None, UpdatedAt] = None
        deleted_at: t.Annotated[dt.datetime | None, DeletedAt] = None
        rev: t.Annotated[int, Revision] = 0

    @define
    class Comment(Spec):
        __spec__ = SpecConfig(collection="comments", indexes=[Index("article")])
        body: str = ""
        article: t.Annotated[Ref[Article] | None, OnDelete("cascade")] = None
        author: Ref[User] | None = None
        likes: int = 0

    @define
    class Post(Spec):
        """Like Article, with reverse relations and an aliased field."""

        __spec__ = SpecConfig(collection="posts")
        title: str = ""
        summary: t.Annotated[str, Alias("s")] = ""
        comments: t.Annotated[list[t.Any], Backref("post", model="Note")] = []
        comment_count: t.Annotated[int, Backref("post", model="Note", count=True)] = 0

    @define
    class Note(Spec):
        __spec__ = SpecConfig(collection="notes", indexes=[Index("post")])
        text: str = ""
        post: Ref[Post] | None = None
        rank: int = 0

    @define
    class Category(Spec):
        __spec__ = SpecConfig(collection="categories")
        name: str = ""
        parent: t.Annotated[Ref | None, To("Category")] = None

    @define
    class Animal(Spec):
        __spec__ = SpecConfig(collection="animals", discriminator="kind")
        name: str = ""

    @define
    class Cat(Animal):
        lives: int = 9

    @define
    class Dog(Animal):
        __spec__ = SpecConfig(discriminator_value="doggo")
        good: bool = True

    @define
    class Puppy(Dog):
        age_weeks: int = 8

    @define
    class Video(Spec):
        __spec__ = SpecConfig(collection="videos")
        title: str = ""
        seconds: int = 0

    @define
    class Bookmark(Spec):
        __spec__ = SpecConfig(collection="bookmarks")
        label: str = ""
        target: Ref[Article | Video] | None = None

    @define
    class Tenant(Spec):
        __spec__ = SpecConfig(collection="tenant_docs", shard_key=["tenant"])
        tenant: str = ""
        value: int = 0

    ns = types.SimpleNamespace(
        backend=backend,
        Spec=Spec,
        SpecAdapter=SpecAdapter,
        define=define,
        Address=Address,
        Plain=Plain,
        Color=Color,
        User=User,
        Article=Article,
        Comment=Comment,
        Post=Post,
        Note=Note,
        Category=Category,
        Animal=Animal,
        Cat=Cat,
        Dog=Dog,
        Puppy=Puppy,
        Video=Video,
        Bookmark=Bookmark,
        Tenant=Tenant,
        OID=OID,
    )
    ns.ALL = [User, Article, Comment, Post, Note, Category, Animal, Cat, Dog, Puppy, Video, Bookmark, Tenant]
    return ns
