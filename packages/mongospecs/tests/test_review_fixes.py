"""Regression tests for defects reported in review of the v2 pull requests."""

import copy

import pytest

from mongospecs import (
    BulkWriteFailed,
    DeleteMany,
    DeleteOne,
    DuplicateKeyError,
    Inc,
    Q,
    StaleDocumentError,
)


async def test_cursor_pages_with_a_projection_do_not_repeat(m, registry):
    """select() used to drop the sort key from the projection, so the cursor was built from defaults."""
    await m.User.insert_many([m.User(name=f"u{i:02d}", age=i % 5, email=f"{i}@x.io") for i in range(30)])
    for select in (("name",), ("-age",), ("email",)):
        query = m.User.find().select(*select).sort("age", "-name")
        seen, cursor, pages = [], None, 0
        while True:
            page = await query.page(7, after=cursor)
            seen.extend(u.name for u in page)
            pages += 1
            assert pages <= 6, f"select{select}: pagination does not terminate"
            if not page.has_next:
                break
            cursor = page.next_cursor
        assert len(seen) == len(set(seen)) == 30 and pages == 5, (select, pages, len(seen))
    raw = await m.User.find().raw().select("name").sort("-age").page(4)
    assert "age" in raw[0]  # sort keys are always fetched: the cursor is built from them


async def test_bulk_save_keeps_memory_in_sync_after_a_partial_failure(m, registry):
    docs = (await m.Article.insert_many([m.Article(title=f"a{i}") for i in range(3)])).inserted
    fresh = m.Article(title="new")
    await m.Article.collection().delete_one({"_id": docs[1].id})  # this one cannot be updated
    for doc in docs:
        doc.views = 5
    with pytest.raises(StaleDocumentError) as info:
        await m.Article.bulk_save([*docs, fresh])
    assert info.value.failed == [docs[1]]
    # the others were written, and say so
    for doc in (docs[0], docs[2]):
        assert doc.rev == 1 and doc.changes() == {}
        assert (await m.Article.collection().find_one({"_id": doc.id}))["views"] == 5
    assert not fresh.is_new and fresh.changes() == {}
    assert docs[1].rev == 0 and docs[1].changes() == {"$set": {"views": 5}}
    docs[0].views = 6
    assert await m.Article.bulk_save([docs[0], docs[2]]) == 1  # saving again works


async def test_bulk_save_reports_revision_conflicts_per_document(m, registry):
    docs = (await m.Article.insert_many([m.Article(title=f"a{i}") for i in range(3)])).inserted
    await m.Article.update(docs[2].id, Inc(views=1))  # someone else got there first
    for doc in docs:
        doc.title += "!"
    with pytest.raises(StaleDocumentError) as info:
        await m.Article.bulk_save(docs)
    assert info.value.failed == [docs[2]]
    assert [d.rev for d in docs] == [1, 1, 0]


async def test_bulk_save_duplicate_key_does_not_hide_the_other_writes(m, registry):
    await registry.apply_indexes()
    await m.User(name="taken", email="t@x.io").insert()
    existing = await m.User(name="e", email="e@x.io").insert()
    existing.age = 9
    clash, fine = m.User(name="clash", email="t@x.io"), m.User(name="fine", email="f@x.io")
    with pytest.raises((DuplicateKeyError, BulkWriteFailed)) as info:
        await m.User.bulk_save([existing, clash, fine])
    assert info.value.failed == [clash]
    assert existing.changes() == {} and not fine.is_new and clash.is_new
    assert await m.User.count() == 3


async def test_bulk_write_deletes_run_cascades(m, registry):
    a1, a2, a3 = (await m.Article.insert_many([m.Article(title=t) for t in "abc"])).inserted
    await m.Comment.insert_many([m.Comment(body=f"c{i}", article=a) for i, a in enumerate((a1, a1, a2, a3))])
    await m.Article.bulk_write([DeleteOne(a1.id, hard=True), DeleteMany(Q.title == "b", hard=True)])
    assert [c.body for c in await m.Comment.find()] == ["c3"]  # only a3's comment is left
    # a soft delete in a bulk write does not cascade, like a single soft delete
    await m.Article.bulk_write([DeleteOne(a3.id)])
    assert await m.Comment.count() == 1 and await m.Article.count() == 0


async def test_cascade_cycles_terminate(m, make_registry):
    import typing as t

    from mongospecs import OnDelete, Ref, SpecConfig, To

    @m.define
    class Left(m.Spec):
        __spec__ = SpecConfig(collection="cyc_left")
        other: t.Annotated[Ref | None, To("Right"), OnDelete("cascade")] = None

    @m.define
    class Right(m.Spec):
        __spec__ = SpecConfig(collection="cyc_right")
        other: t.Annotated[Ref[Left] | None, OnDelete("cascade")] = None

    await make_registry(Left, Right)
    left = await Left().insert()
    right = await Right(other=left).insert()
    left.other = right
    await left.save()
    await Left().insert()  # unrelated, must survive
    await left.delete()
    assert await Left.count() == 1 and await Right.count() == 0


async def test_large_cascades_are_chunked(m, registry):
    articles = (await m.Article.insert_many([m.Article(title=f"a{i}") for i in range(2500)])).inserted
    await m.Comment.insert_many([m.Comment(body="c", article=a) for a in articles])
    assert await m.Article.find().delete(hard=True) == 2500
    assert await m.Comment.count() == 0


async def test_saving_an_untracked_copy_says_what_is_wrong(m, registry):
    user = await m.User(name="ann").insert()
    for clone in (copy.copy(user), copy.deepcopy(user), await m.User.find().lean().one()):
        clone.age = 3
        with pytest.raises(DuplicateKeyError, match="not tracked"):
            await clone.save()
    assert (await m.User.get(user.id)).age == 0
    adopted = m.User.hydrate(await m.User.collection().find_one({"_id": user.id}))
    adopted.age = 4
    await adopted.save()  # the documented way to get a saveable copy
    assert (await m.User.get(user.id)).age == 4


async def test_reload_respects_deferred_fields(m, registry):
    user = await m.User(name="ann", secret="s3cret").insert()
    loaded = await m.User.get(user.id)
    await m.User.collection().update_one({"_id": user.id}, {"$set": {"age": 7}})
    await loaded.reload()
    assert loaded.age == 7 and loaded.secret is None  # still deferred
    loaded.name = "anna"
    await loaded.save()
    assert (await m.User.collection().find_one({"_id": user.id}))["secret"] == "s3cret"


async def test_filter_update_reports_matched_and_modified(m, registry):
    await m.User.insert_many([m.User(name="a", age=1), m.User(name="b", age=1), m.User(name="c", age=2)])
    result = await m.User.find(Q.age >= 1).update(age=1)
    assert result == 1 and result.modified == 1 and result.matched == 3  # still an int: the number modified
