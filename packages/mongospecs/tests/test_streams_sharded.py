import asyncio
import uuid

import pytest
import pytest_asyncio

from mongospecs import Q, Registry, SpecConfig

from .conftest import SHARDED_URI


async def test_change_stream_events(m, registry):
    await m.User(name="before").insert()
    async with m.User.watch(max_await_time_ms=200) as stream:
        events = []
        user = await m.User(name="ann").insert()
        events.append(await asyncio.wait_for(stream.__anext__(), 10))
        user.age = 5
        await user.save()
        events.append(await asyncio.wait_for(stream.__anext__(), 10))
        await user.delete()
        events.append(await asyncio.wait_for(stream.__anext__(), 10))
        assert [e.operation for e in events] == ["insert", "update", "delete"]
        assert type(events[0].document) is m.User and events[0].document.name == "ann"
        assert events[0].id == user.id == events[2].id
        assert events[1].updated == {"age": 5} and events[1].document.age == 5
        assert events[2].document is None
        assert stream.resume_token is not None and events[2].resume_token is not None
        assert await stream.try_next() is None
        token = events[0].resume_token
    async with m.User.watch(resume_after=token, max_await_time_ms=200) as resumed:
        again = [await asyncio.wait_for(resumed.__anext__(), 10) for _ in range(2)]
        assert [e.operation for e in again] == ["update", "delete"]


async def test_change_stream_pipeline_and_async_for(m, registry):
    stream = m.User.watch([{"$match": {"operationType": "insert"}}], max_await_time_ms=200)
    await stream._open()
    await m.User(name="a").insert()
    await m.User.find().update(age=1)
    await m.User(name="b").insert()
    names = []
    async for event in stream:
        names.append(event.document.name)
        if len(names) == 2:
            break
    await stream.close()
    assert names == ["a", "b"]


# -- sharded cluster ---------------------------------------------------------------


@pytest_asyncio.fixture
async def sharded(m):
    name = f"ms_sh_{uuid.uuid4().hex[:10]}"
    async with Registry(SHARDED_URI, database=name, models=m.ALL) as reg:
        await reg.client.admin.command("enableSharding", name)
        yield reg, name
        await reg.client.drop_database(name)


@pytest.mark.sharded
async def test_shard_key_is_added_to_document_writes(m, sharded):
    reg, name = sharded
    events = []
    await reg.client.admin.command("shardCollection", f"{name}.tenant_docs", key={"tenant": 1})
    doc = await m.Tenant(tenant="t1", value=1).insert()
    await m.Tenant(tenant="t2", value=1).insert()
    async with Registry(SHARDED_URI, database=name, on_command=events.append) as observer:
        manager = observer.manager(m.Tenant)
        loaded = await manager.find(Q.id == doc.id).one()
        loaded.value = 2
        await manager.save(loaded)
        await manager.delete_document(loaded)
    writes = {
        e.command_name: e.command
        for e in events
        if e.phase == "started" and e.command_name in ("update", "delete")
    }
    assert writes["update"]["updates"][0]["q"] == {"_id": doc.id, "tenant": "t1"}
    assert writes["delete"]["deletes"][0]["q"] == {"_id": doc.id, "tenant": "t1"}
    assert await m.Tenant.count() == 1


@pytest.mark.sharded
async def test_relations_on_a_sharded_cluster(m, sharded):
    reg, name = sharded
    await reg.client.admin.command("shardCollection", f"{name}.users", key={"_id": "hashed"})
    await reg.client.admin.command("shardCollection", f"{name}.categories", key={"_id": "hashed"})
    users = (await m.User.insert_many([m.User(name=f"u{i}") for i in range(20)])).inserted
    await m.Article.insert_many(
        [m.Article(title=f"a{i}", author=users[i], editors=users[:3]) for i in range(20)]
    )
    for strategy in ("batch", "lookup"):
        arts = (
            await m.Article.find()
            .sort("title")
            .include("author", strategy=strategy)
            .include("editors", strategy=strategy)
        )
        assert [a.author.doc.name for a in arts] == sorted(u.name for u in users)
        assert all(len([e for e in a.editors if e.loaded]) == 3 for a in arts)
    # $unionWith and transactions work through mongos
    await m.Video(title="v").insert()
    assert len(await m.Article.find().union(m.Video.find())) == 21

    async def work(tx):
        await m.User(name="in tx").insert(session=tx)
        return await m.User.count(session=tx)

    assert await reg.transaction(work) == 21
    # $graphLookup cannot read from a sharded collection inside a transaction... outside it can
    root = await m.Category(name="root").insert()
    leaf = await m.Category(name="leaf", parent=root).insert()
    (res,) = await m.Category.find(Q.id == leaf.id).graph("parent")
    assert [n.name for n in res.nodes] == ["root"]


@pytest.mark.sharded
async def test_lookup_from_sharded_collection_in_transaction_is_a_typed_error(m, sharded):
    from mongospecs import MongoSpecsError

    reg, name = sharded
    await reg.client.admin.command("shardCollection", f"{name}.users", key={"_id": "hashed"})
    user = await m.User(name="u").insert()
    await m.Article(title="a", author=user).insert()

    async def work(tx):
        return await m.Article.find(session=tx).include("author", strategy="lookup").all()

    try:
        result = await reg.transaction(work)
    except MongoSpecsError as exc:
        assert "pymongo" not in type(exc).__module__
    else:  # newer servers allow it
        assert result[0].author.doc.name == "u"

    async def batch(tx):
        return await m.Article.find(session=tx).include("author", strategy="batch").all()

    assert (await reg.transaction(batch))[0].author.doc.name == "u"


_ = SpecConfig
