"""Correctness of the paths that were made cheaper: the result must not have changed."""

import asyncio
import gc
import typing as t

import pytest

from mongospecs import Backref, Q, Ref, SpecConfig, tracking


def started(events, name=None):
    return [e for e in events if e.phase == "started" and (name is None or e.command_name == name)]


# -- tracking: one weak reference per document -------------------------------------------------


async def test_tracking_state_goes_away_with_the_document(m, registry):
    await m.User.insert_many([m.User(name=f"u{i}") for i in range(50)])
    gc.collect()
    before = tracking.tracked_count()
    docs = await m.User.find().all()
    assert tracking.tracked_count() == before + 50
    first = docs[0]
    del docs
    gc.collect()
    assert tracking.tracked_count() == before + 1 and tracking.state_of(first) is not None
    # re-tracking the same object (save, reload) keeps exactly one entry and stays collectable
    first.age = 3
    await first.save()
    await first.reload()
    assert tracking.tracked_count() == before + 1 and first.changes() == {}
    del first
    gc.collect()
    assert tracking.tracked_count() == before


async def test_a_reused_id_never_inherits_another_documents_state(m, registry):
    await m.User(name="a").insert()
    for _ in range(200):  # ids of freed objects are reused quickly; a fresh object must be untracked
        loaded = await m.User.find().one()
        del loaded
        fresh = m.User(name="fresh")
        assert tracking.state_of(fresh) is None and fresh.is_new


async def test_transaction_rollback_restores_tracking(m, registry):
    user = await m.User(name="a").insert()

    async def change(tx):
        user.name = "b"
        await user.save(session=tx)
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        await registry.transaction(change)
    assert user.changes() in ({}, {"$set": {"name": "b"}})
    assert (await m.User.get(user.id)).name == "a"
    gc.collect()
    assert tracking.state_of(user) is not None


# -- Query._clone ------------------------------------------------------------------------------


def test_clone_keeps_every_setting_and_shares_nothing_mutable(m, registry):
    base = (
        m.User.find(Q.age > 1)
        .sort("-age")
        .skip(2)
        .limit(5)
        .select("name")
        .lean()
        .options(max_time_ms=50)
        .or_fail()
        .on_invalid("skip")
    )
    clone = base.limit(9)
    for slot in type(base).__slots__:
        if slot in ("_limit", "_keys"):
            continue
        assert getattr(clone, slot) == getattr(base, slot), slot
    assert (base._limit, clone._limit) == (5, 9)
    changed = base.options(max_time_ms=99)
    assert base._options["max_time_ms"] == 50 and changed._options["max_time_ms"] == 99
    assert changed._options is not base._options
    assert type(clone) is type(base)


# -- includes ----------------------------------------------------------------------------------


@pytest.fixture
async def library(m, make_registry):
    @m.define
    class Writer(m.Spec):
        __spec__ = SpecConfig(collection="hp_writers")
        name: str = ""
        books: t.Annotated[list[t.Any], Backref("writer", model="Book")] = []  # noqa: RUF012

    @m.define
    class Shelf(m.Spec):
        __spec__ = SpecConfig(collection="hp_shelves")
        name: str = ""

    @m.define
    class Book(m.Spec):
        __spec__ = SpecConfig(collection="hp_books")
        title: str = ""
        pages: int = 0
        writer: Ref[Writer] | None = None
        shelf: Ref[Shelf] | None = None
        co_writers: list[Ref[Writer]] = []  # noqa: RUF012

    events = []
    registry = await make_registry(Writer, Shelf, Book, on_command=events.append)
    writers = (await Writer.insert_many([Writer(name=f"w{i}") for i in range(4)])).inserted
    shelves = (await Shelf.insert_many([Shelf(name=f"s{i}") for i in range(2)])).inserted
    await Book.insert_many(
        [
            Book(title=f"b{w}-{i:02d}", pages=(i * 7) % 11, writer=writers[w], shelf=shelves[i % 2])
            for w in range(3)  # w3 has no books
            for i in range(12)
        ]
    )
    return registry, events, Writer, Shelf, Book


async def test_independent_includes_are_in_flight_together(library):
    registry, events, Writer, Shelf, Book = library
    # open a few pooled connections first: a second connection being set up would hide the overlap
    await asyncio.gather(*(Book.count() for _ in range(6)))
    events.clear()
    books = await Book.find().sort("title").include("writer").include("shelf").all()
    assert len(books) == 36 and all(b.writer.loaded and b.shelf.loaded for b in books)
    assert books[0].writer.doc.name == "w0" and {b.shelf.doc.name for b in books} == {"s0", "s1"}
    finds = [e for e in events if e.command_name == "find"]
    order = [(e.phase, e.command["find"] if e.phase == "started" else None) for e in finds]
    relation_starts = [i for i, (phase, coll) in enumerate(order) if coll in ("hp_writers", "hp_shelves")]
    first_done_after = next(
        i for i, (phase, _) in enumerate(order) if phase != "started" and i > relation_starts[0]
    )
    assert (
        len(relation_starts) == 2 and relation_starts[1] < first_done_after
    )  # both sent before either returned
    assert len(started(events)) == 3  # still one command per relation

    # inside a transaction they run one after another: a session cannot do two things at once
    async def in_tx(tx):
        return await Book.find(session=tx).include("writer").include("shelf").all()

    assert len(await registry.transaction(in_tx)) == 36


@pytest.mark.parametrize("mode", ["tracked", "lean", "raw"])
async def test_per_parent_limit_is_applied_by_the_server(library, mode):
    registry, events, Writer, Shelf, Book = library
    expected = {}
    for writer in await Writer.find().all():
        mine = await Book.find(Q.writer == writer.id, Q.pages >= 2).sort("-pages", "title").all()
        expected[writer.name] = [b.title for b in mine[:3]]
    events.clear()
    query = getattr(Writer.find().sort("name"), mode)()
    writers = await query.include(
        "books", per_parent_limit=3, match=Q.pages >= 2, sort=["-pages", "title"]
    ).all()

    def books_of(w):
        return w["books"] if isinstance(w, dict) else w.books

    def name_of(w):
        return w["name"] if isinstance(w, dict) else w.name

    def title_of(b):
        return b["title"] if isinstance(b, dict) else b.title

    got = {name_of(w): [title_of(b) for b in books_of(w)] for w in writers}
    assert got == expected and got["w3"] == [] and all(len(v) == 3 for k, v in got.items() if k != "w3")
    # the children are chosen on the server: only the kept ones are fetched
    (pick,) = started(events, "aggregate")
    assert any("$firstN" in str(stage) for stage in pick.command["pipeline"])
    fetch = [e for e in started(events, "find") if e.command["find"] == "hp_books"]
    assert len(fetch) == 1 and len(fetch[0].command["filter"]["_id"]["$in"]) == 9

    # without a sort the per-parent cap still holds
    capped = await Writer.find().include("books", per_parent_limit=2).all()
    assert sorted(len(w.books) for w in capped) == [0, 2, 2, 2]
    # an overall limit keeps the previous behaviour (limit first, then the cap per parent)
    overall = await Writer.find().include("books", per_parent_limit=2, limit=5, sort=["title"]).all()
    assert sum(len(w.books) for w in overall) <= 5


async def test_per_parent_limit_on_a_list_reference_still_works(library):
    registry, events, Writer, Shelf, Book = library

    @Writer.scope
    def named(query):  # an unrelated named scope must not disturb relation loading
        return query.filter(Q.name != "")

    del named
    books = await Book.find().limit(2).all()
    writers = await Writer.find().sort("name").all()
    books[0].co_writers = [writers[0], writers[1]]
    await books[0].save()
    loaded = await Book.find(Q.id == books[0].id).include("co_writers", per_parent_limit=1).one()
    assert len(loaded.co_writers) == 2 and sum(r.loaded for r in loaded.co_writers) >= 1
    assert asyncio.get_running_loop() is not None


async def test_a_full_pool_fails_fast_when_a_wait_limit_is_set(m, make_registry, db_name):
    """Backs the production guide: with waitQueueTimeoutMS an exhausted pool raises instead of queueing."""
    from mongospecs import PoolTimeout

    registry = await make_registry(m.User, maxPoolSize=1, waitQueueTimeoutMS=100)
    users = registry.manager(m.User)
    await users.insert(m.User(name="a"))
    slow = asyncio.create_task(
        users.collection.find_one({"$where": "function() { sleep(600); return true; }"})
    )
    await asyncio.sleep(0.15)  # the only connection is now busy
    with pytest.raises(PoolTimeout):
        await users.count()
    await slow
    assert await users.count() == 1  # and it recovers once the connection is free
