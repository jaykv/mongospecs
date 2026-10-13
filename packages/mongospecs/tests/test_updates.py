import pytest
from bson import ObjectId

from mongospecs import (
    AddToSet,
    CurrentDate,
    DocumentNotFound,
    Each,
    Inc,
    Max,
    Min,
    Mul,
    Pop,
    Pull,
    Push,
    Q,
    QueryError,
    Set,
    SetOnInsert,
    StaleDocumentError,
    Unset,
    ValidationError,
    meta_of,
)
from mongospecs.updates import compile_update


def test_compile_operators(m):
    meta = meta_of(m.Article)
    uid = ObjectId()
    update = compile_update(
        meta,
        [Inc(views=2), Push(tags="a"), AddToSet(editors=uid), Unset("author"), Min({"created_at": None})],
        {"title": "t"},
    )
    assert update == {
        "$inc": {"views": 2},
        "$push": {"tags": "a"},
        "$addToSet": {"editors": uid},
        "$unset": {"author": ""},
        "$min": {"created_at": None},
        "$set": {"title": "t"},
    }
    assert compile_update(meta, [Push(tags=Each(["a", "b"], position=0, slice=5))], {}) == {
        "$push": {"tags": {"$each": ["a", "b"], "$position": 0, "$slice": 5}}
    }
    assert compile_update(meta, [Pull(tags="a"), Pop(editors=-1), Mul(views=3), Max(rev=9)], {}) == {
        "$pull": {"tags": "a"},
        "$pop": {"editors": -1},
        "$mul": {"views": 3},
        "$max": {"rev": 9},
    }
    assert compile_update(meta, [CurrentDate("updated_at"), SetOnInsert(title="x")], {}) == {
        "$currentDate": {"updated_at": True},
        "$setOnInsert": {"title": "x"},
    }
    assert compile_update(meta_of(m.Post), [], {"summary": "x"}) == {"$set": {"s": "x"}}
    assert compile_update(meta_of(m.User), [Set({"address.city": "Oslo"})], {}) == {
        "$set": {"address.city": "Oslo"}
    }
    assert compile_update(meta_of(m.User), [Pull(name=Q.x > 1)], {}) == {"$pull": {"name": {"x": {"$gt": 1}}}}


def test_updates_are_validated(m):
    meta = meta_of(m.Article)
    with pytest.raises(QueryError, match="no field"):
        compile_update(meta, [], {"nope": 1})
    with pytest.raises(ValidationError):
        compile_update(meta, [], {"views": "many"})
    with pytest.raises(QueryError, match="number"):
        compile_update(meta, [Inc(views="1")], {})
    with pytest.raises(QueryError, match="id"):
        compile_update(meta, [], {"id": ObjectId()})
    with pytest.raises(QueryError, match="both"):
        compile_update(meta, [Inc(views=1)], {"views": 2})
    with pytest.raises(QueryError, match="not a list"):
        compile_update(meta, [Push(title="x")], {})
    with pytest.raises(ValidationError):
        compile_update(meta, [Push(tags=5)], {})
    with pytest.raises(QueryError, match="Pop"):
        compile_update(meta, [Pop(tags=2)], {})
    with pytest.raises(QueryError, match="operator"):
        compile_update(meta, ["nope"], {})


def test_immutable_and_required_fields_are_protected(m):
    import typing as t

    meta = meta_of(m.Article)
    with pytest.raises(QueryError, match="immutable"):
        compile_update(meta, [], {"slug": "x"})
    with pytest.raises(QueryError, match="immutable"):
        compile_update(meta, [Unset("slug")], {})
    assert compile_update(meta, [SetOnInsert(slug="x")], {}) == {"$setOnInsert": {"slug": "x"}}

    @m.define
    class Strict(m.Spec):
        name: str

    with pytest.raises(QueryError, match="required"):
        compile_update(meta_of(Strict), [Unset("name")], {})
    _ = t


async def test_update_by_id_returns_tracked_document(m, registry):
    art = await m.Article(title="a", tags=["x"]).insert()
    updated = await m.Article.update(art.id, Inc(views=3), Push(tags="y"), title="b")
    assert (updated.title, updated.views, updated.tags, updated.rev) == ("b", 3, ["x", "y"], 1)
    assert updated.updated_at >= art.updated_at
    assert not updated.is_new and updated.changes() == {}
    before = await m.Article.update(art.id, returning="before", views=10)
    assert before.views == 3


async def test_update_by_filter_and_not_found(m, registry):
    await m.User(name="ann", age=1).insert()
    assert (await m.User.update(Q.name == "ann", age=2)).age == 2
    with pytest.raises(DocumentNotFound):
        await m.User.update(Q.name == "nobody", age=2)
    with pytest.raises(DocumentNotFound):
        await m.User.update(ObjectId(), age=2)
    with pytest.raises(QueryError):
        await m.User.update(Q.name == "ann")


async def test_immutable_field_cannot_be_saved(m, registry):
    art = await m.Article(title="a", slug="first").insert()
    art.slug = "second"
    with pytest.raises(QueryError, match="immutable"):
        await art.save()
    assert (await m.Article.get(art.id)).slug == "first"


async def test_upsert_single_round_trip(m, registry):
    created = await m.User.upsert(Q.email == "a@b.c", SetOnInsert(name="new"), Inc(age=1))
    assert created.name == "new" and created.age == 1 and created.email == "a@b.c"
    again = await m.User.upsert(Q.email == "a@b.c", SetOnInsert(name="ignored"), Inc(age=1))
    assert again.id == created.id and again.name == "new" and again.age == 2
    assert await m.User.count() == 1
    art = await m.Article.upsert(Q.title == "up", Inc(views=1))
    assert art.created_at is not None and art.updated_at is not None and art.rev == 1
    cat = await m.Cat.upsert(Q.name == "tom", lives=3)
    assert (await m.Cat.collection().find_one({"_id": cat.id}))["kind"] == "Cat"


async def test_filter_based_update_and_delete(m, registry):
    await m.User.insert_many([m.User(name=n, age=i) for i, n in enumerate("abcd")])
    assert await m.User.find(Q.age >= 2).update(Inc(age=10)) == 2
    assert sorted(u.age for u in await m.User.find()) == [0, 1, 12, 13]
    assert await m.User.find(Q.age > 5).delete() == 2
    assert await m.User.count() == 2
    assert await m.User.find().update(name="same") == 2


async def test_if_rev_on_atomic_update(m, registry):
    art = await m.Article(title="a").insert()
    ok = await m.Article.update(art.id, if_rev=0, title="b")
    assert ok.rev == 1
    with pytest.raises(StaleDocumentError):
        await m.Article.update(art.id, if_rev=0, title="c")
    with pytest.raises(DocumentNotFound):
        await m.Article.update(ObjectId(), if_rev=0, title="c")


async def test_upsert_gives_the_new_document_the_model_defaults(m, registry):
    """A document created by an upsert must look like one created by insert()."""
    created = await m.Article.upsert(Q.title == "via upsert", Inc(views=5))
    inserted = await m.Article(title="via insert", views=5).insert()
    raw_upsert = await m.Article.collection().find_one({"_id": created.id}, {"_id": 0, "title": 0, "rev": 0})
    raw_insert = await m.Article.collection().find_one({"_id": inserted.id}, {"_id": 0, "title": 0, "rev": 0})
    for doc in (raw_upsert, raw_insert):
        doc.pop("created_at"), doc.pop("updated_at")

    def present(doc):
        return {k: v for k, v in doc.items() if v is not None}

    assert present(raw_insert) == present(raw_upsert)
    assert raw_upsert["tags"] == [] and raw_upsert["editors"] == [] and raw_upsert["reviewers"] == {}
    # so a query on a defaulted value finds it
    assert await m.Article.count(Q.tags == []) == 2
    # the filter and the update keep precedence over defaults, and an existing document is not reset
    again = await m.Article.upsert(Q.title == "via upsert", Push(tags="x"))
    assert again.tags == ["x"] and again.views == 5
    with_default = await m.User.upsert((Q.name == "u") & (Q.age == 7), email="u@x.io")
    assert with_default.age == 7
    assert await m.Cat.upsert(Q.name == "tom", Inc(lives=-1)) is not None
    assert (await m.Cat.collection().find_one({"name": "tom"}))["lives"] == -1  # $inc owns the field
