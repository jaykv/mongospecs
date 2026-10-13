import asyncio
import gc

import pytest

from mongospecs import DuplicateKeyError, Q, StaleDocumentError, tracking
from mongospecs.tracking import diff


def test_diff_semantics():
    assert diff({"a": 1}, {"a": 1}) == ({}, [])
    assert diff({"a": 1}, {"a": 2, "b": 3}) == ({"a": 2, "b": 3}, [])
    assert diff({"a": 1, "b": 2}, {"a": 1}) == ({}, ["b"])
    assert diff({"n": {"x": 1, "y": 2}}, {"n": {"x": 1, "y": 3}}) == ({"n.y": 3}, [])
    assert diff({"n": {"x": 1}}, {"n": {}}) == ({"n": {}}, [])
    assert diff({"n": {"x": 1, "y": 1}}, {"n": {"x": 1}}) == ({}, ["n.y"])
    assert diff({"l": [1, 2]}, {"l": [1, 2, 3]}) == ({"l": [1, 2, 3]}, [])
    assert diff({"n": {"a.b": 1}}, {"n": {"a.b": 2}}) == ({"n": {"a.b": 2}}, [])  # undottable key
    assert diff({"n": {"$x": 1}}, {"n": {"$x": 2}}) == ({"n": {"$x": 2}}, [])
    assert diff({"a": None}, {"a": 0}) == ({"a": 0}, [])
    assert diff({"a": 1}, {"a": True}) == ({"a": True}, [])
    assert diff({"a": 1}, {"a": None}) == ({"a": None}, [])
    assert diff({"_id": 1, "a": 1}, {"_id": 2, "a": 1}) == ({}, [])
    assert diff({"a": 1, "b": 1}, {"a": 2, "b": 2}, frozenset({"a"})) == ({"a": 2}, [])
    assert diff({"a": 1}, {"b": 5}, frozenset({"b"})) == ({"b": 5}, [])


async def test_state_is_released_with_the_document(m, registry):
    before = tracking.tracked_count()
    docs = await m.User.insert_many([m.User(name=str(i)) for i in range(50)])
    loaded = await m.User.find().all()
    assert tracking.tracked_count() >= before + 100
    del docs, loaded
    gc.collect()
    assert tracking.tracked_count() <= before + 1


async def test_lean_and_raw_are_not_tracked(m, registry):
    await m.User.insert_many([m.User(name=str(i)) for i in range(10)])
    gc.collect()
    before = tracking.tracked_count()
    lean = await m.User.find().lean().all()
    raw = await m.User.find().raw().all()
    assert tracking.tracked_count() == before
    assert len(lean) == len(raw) == 10


async def test_list_changes_are_sent_whole(m, registry):
    art = await m.Article(title="a", tags=["x"]).insert()
    art.tags.append("y")
    assert art.changes() == {"$set": {"tags": ["x", "y"]}}
    await art.save()
    assert (await m.Article.get(art.id)).tags == ["x", "y"]


async def test_reference_changes(m, registry):
    a, b = await m.User(name="a").insert(), await m.User(name="b").insert()
    art = await m.Article(title="t", author=a).insert()
    loaded = await m.Article.get(art.id)
    assert loaded.changes() == {}
    loaded.author = b
    assert loaded.changes() == {"$set": {"author": b.id}}
    await loaded.save()
    assert (await m.Article.get(art.id)).author.id == b.id


async def test_revision_conflict_between_two_loaded_copies(m, registry):
    art = await m.Article(title="a").insert()
    one, two = await m.Article.get(art.id), await m.Article.get(art.id)
    one.title = "one"
    await one.save()
    assert one.rev == 1
    two.title = "two"
    with pytest.raises(StaleDocumentError):
        await two.save()
    await two.reload()
    two.title = "two"
    await two.save()
    assert two.rev == 2 and (await m.Article.get(art.id)).title == "two"


async def test_transaction_commits(m, registry):
    async def work(tx):
        user = await m.User(name="ann").insert(session=tx)
        await m.Article(title="t", author=user).insert(session=tx)
        assert await m.User.count(session=tx) == 1
        assert await m.User.count() == 0  # not visible outside yet
        return user

    user = await registry.transaction(work)
    assert await m.User.count() == 1 and await m.Article.count() == 1
    assert not user.is_new


async def test_transaction_abort_rolls_back_database_and_memory(m, registry):
    existing = await m.Article(title="orig").insert()
    fresh = m.User(name="new")

    async def work(tx):
        await fresh.insert(session=tx)
        existing.title = "changed"
        await existing.save(session=tx)
        assert existing.rev == 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await registry.transaction(work)
    assert await m.User.count() == 0
    assert (await m.Article.get(existing.id)).title == "orig"
    # in-memory state is back to before the transaction
    assert fresh.is_new and fresh.id is None
    assert existing.rev == 0
    assert existing.changes() == {"$set": {"title": "changed"}}
    await existing.save()
    assert (await m.Article.get(existing.id)).title == "changed"
    await fresh.insert()
    assert await m.User.count() == 1


async def test_transaction_retries_transient_errors(m, registry):
    art = await m.Article(title="a").insert()
    attempts = []
    blocker_started = asyncio.Event()
    release = asyncio.Event()

    async def blocker(tx):
        await m.Article.update(art.id, session=tx, title="blocker")
        blocker_started.set()
        await release.wait()

    async def contender(tx):
        attempts.append(1)
        if len(attempts) == 1:
            await blocker_started.wait()
        else:
            release.set()
            await blocker_task
        return await m.Article.update(art.id, session=tx, views=len(attempts))

    blocker_task = asyncio.create_task(registry.transaction(blocker))
    result = await registry.transaction(contender)
    assert len(attempts) >= 2  # the write conflict was retried by re-running the callback
    assert result.views == len(attempts)
    final = await m.Article.get(art.id)
    assert final.title == "blocker" and final.views == len(attempts)


async def test_manual_session_transaction(m, registry):
    user = m.User(name="ann")
    async with registry.session() as s:
        async with s.start_transaction():
            await user.insert(session=s)
        assert await m.User.count() == 1
        with pytest.raises(RuntimeError):
            async with s.start_transaction():
                user.age = 5
                await user.save(session=s)
                raise RuntimeError
    assert (await m.User.get(user.id)).age == 0
    assert user.changes() == {"$set": {"age": 5}}


async def test_errors_inside_transaction_are_translated(m, registry):
    await registry.apply_indexes()
    await m.User(name="a", email="x").insert()

    async def work(tx):
        await m.User(name="b", email="x").insert(session=tx)

    with pytest.raises(DuplicateKeyError):
        await registry.transaction(work)
    assert await m.User.count() == 1


async def test_cancellation_leaves_no_session_behind(m, registry):
    started = asyncio.Event()

    async def slow(tx):
        await m.User(name="x").insert(session=tx)
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(registry.transaction(slow))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await m.User.count() == 0
    assert await m.User.find(Q.name == "x").exists() is False
    await m.User(name="after").insert()  # the client is still usable


async def test_freshly_loaded_documents_report_no_changes(m, registry):
    """The snapshot is the raw stored form; loading must not look like editing, whatever the types."""
    user = await m.User(name="ann", age=3, color=m.Color.BLUE, address=m.Address(city="Oslo")).insert()
    article = await m.Article(
        title="t", author=user, editors=[user], reviewers={"a": user}, tags=["x"]
    ).insert()
    # documents written by other code: an int where the model has a float-like default, missing optionals
    await m.User.collection().insert_one({"name": "sparse"})
    await m.Video.collection().insert_one({"title": "v", "seconds": 60.0})
    for model in (m.User, m.Article, m.Video):
        for doc in await model.find():
            assert doc.changes() == {}, (model.__name__, doc.changes())
            assert not doc.is_new
    loaded = await m.Article.get(article.id)
    loaded.tags.append("y")
    assert loaded.changes() == {"$set": {"tags": ["x", "y"]}}
    sparse = await m.User.find(Q.name == "sparse").one()
    sparse.age = 7
    await sparse.save()
    assert await m.User.collection().find_one({"name": "sparse"}, {"_id": 0}) == {"name": "sparse", "age": 7}


async def test_documents_written_before_a_field_existed(m, registry):
    await m.User.collection().insert_one({"name": "old shape"})
    await m.Article.collection().insert_one({"title": "no revision yet"})
    doc = await m.User.find(Q.name == "old shape").one()
    assert doc.changes() == {} and doc.age == 0  # the default is applied in memory, not written
    doc.name = "renamed"
    await doc.save()
    assert await m.User.collection().find_one({"name": "renamed"}, {"_id": 0}) == {"name": "renamed"}
    # a stored document without the revision field can still be saved, and gets one
    article = await m.Article.find().one()
    article.title = "now revisioned"
    await article.save()
    assert article.rev == 1 and (await m.Article.collection().find_one({}))["rev"] == 1
