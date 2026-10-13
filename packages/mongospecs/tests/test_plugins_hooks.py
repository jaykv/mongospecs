import datetime as dt

import pytest

from mongospecs import ConfigurationError, DocumentNotFound, Inc, OpCtx, Plugin, Q, StaleDocumentError


async def test_timestamps_on_every_write_path(m, registry):
    art = await m.Article(title="a").insert()
    assert art.created_at == art.updated_at and art.created_at.microsecond % 1000 == 0
    created = art.created_at
    await _tick()
    art.title = "b"
    await art.save()
    assert art.created_at == created and art.updated_at > created
    saved_at = art.updated_at
    await _tick()
    updated = await m.Article.update(art.id, Inc(views=1))
    assert updated.updated_at > saved_at and updated.created_at == created
    await _tick()
    await m.Article.find(Q.id == art.id).update(Inc(views=1))
    assert (await m.Article.get(art.id)).updated_at > updated.updated_at
    many = await m.Article.insert_many([m.Article(title="x"), m.Article(title="y")])
    assert all(d.created_at and d.updated_at for d in many)
    explicit = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    kept = await m.Article(title="old", created_at=explicit).insert()
    assert kept.created_at == explicit and kept.updated_at > explicit


async def _tick():
    import asyncio

    await asyncio.sleep(0.003)


async def test_soft_delete_scope(m, registry):
    a = await m.Article(title="a").insert()
    b = await m.Article(title="b").insert()
    await a.delete()
    assert a.deleted_at is not None and not a.is_new
    assert await m.Article.count() == 1
    assert await m.Article.get(a.id) is None
    assert [x.title for x in await m.Article.find()] == ["b"]
    assert await m.Article.find().unscoped("soft_delete").count() == 2
    assert await m.Article.exists(Q.title == "a") is False
    # updates and deletes do not reach soft-deleted documents
    with pytest.raises(DocumentNotFound):
        await m.Article.update(a.id, title="zombie")
    assert await m.Article.find().update(Inc(views=1)) == 1
    assert await m.Article.find(Q.title == "a").delete() == 0
    # aggregate is scoped too
    assert [r["title"] for r in await m.Article.aggregate([{"$project": {"title": 1}}])] == ["b"]
    assert len(await m.Article.aggregate([], unscoped=True)) == 2
    # estimated_count is documented as unscoped
    assert await m.Article.estimated_count() == 2
    # filter-based soft delete, restore, hard delete
    assert await m.Article.find(Q.title == "b").delete() == 1
    assert await m.Article.count() == 0
    assert await m.Article.find(Q.id == b.id).unscoped("soft_delete").update(deleted_at=None) == 1
    assert await m.Article.count() == 1
    await (await m.Article.get(b.id)).delete(hard=True)
    assert await m.Article.find().unscoped().count() == 1
    assert await m.Article.find().unscoped().delete(hard=True) == 1
    assert await m.Article.collection().count_documents({}) == 0


async def test_soft_deleted_document_can_still_be_saved_by_its_holder(m, registry):
    a = await m.Article(title="a").insert()
    await a.delete()
    a.title = "edited after delete"
    await a.save()
    raw = await m.Article.collection().find_one({"_id": a.id})
    assert raw["title"] == "edited after delete" and raw["deleted_at"] is not None


async def test_revision_increments_everywhere(m, registry):
    art = await m.Article(title="a").insert()
    assert art.rev == 0
    art.title = "b"
    await art.save()
    assert art.rev == 1
    assert (await m.Article.update(art.id, Inc(views=1))).rev == 2
    await m.Article.find().update(Inc(views=1))
    assert (await m.Article.get(art.id)).rev == 3
    art.title = "stale"
    with pytest.raises(StaleDocumentError):
        await art.save()
    with pytest.raises(StaleDocumentError):
        await art.delete()
    art.rev = 99  # tampering with the revision field does not bypass the check
    with pytest.raises(StaleDocumentError):
        await art.save()


async def test_document_hooks_order_session_and_fail_fast(m, registry):
    calls = []

    @m.User.before("insert")
    async def first(ctx, doc):
        assert isinstance(ctx, OpCtx) and ctx.model is m.User and ctx.registry is registry
        calls.append(("before insert", doc.name, ctx.session is not None))
        doc.name = doc.name.strip()

    @m.User.before("save")
    async def second(ctx, doc):
        calls.append(("before save", doc.name))

    @m.User.after("save")
    async def third(ctx, doc):
        calls.append(("after save", doc.id is not None))

    @m.User.after("delete")
    async def fourth(ctx, doc):
        calls.append(("after delete", doc.name))

    try:
        user = await m.User(name="  ann ").insert()
        assert user.name == "ann" and (await m.User.get(user.id)).name == "ann"
        assert calls == [("before insert", "  ann ", False), ("before save", "ann"), ("after save", True)]
        calls.clear()
        user.age = 2
        await user.save()
        assert calls == [("before save", "ann"), ("after save", True)]
        calls.clear()

        async def work(tx):
            await m.User(name="tx").insert(session=tx)

        await registry.transaction(work)
        assert calls[0] == ("before insert", "tx", True)
        calls.clear()
        # filter-based writes do not run document hooks
        await m.User.find().update(age=9)
        await m.User.find(Q.name == "tx").delete()
        assert calls == []
        await user.delete()
        assert calls == [("after delete", "ann")]

        @m.User.before("insert")
        async def veto(ctx, doc):
            raise PermissionError("no")

        try:
            with pytest.raises(PermissionError):
                await m.User(name="blocked").insert()
            assert await m.User.count() == 0
        finally:
            _unhook(m.User, "before", "insert", veto)
    finally:
        for when, event, fn in (
            ("before", "insert", first),
            ("before", "save", second),
            ("after", "save", third),
            ("after", "delete", fourth),
        ):
            _unhook(m.User, when, event, fn)


def _unhook(model, when, event, fn):
    from mongospecs.meta import remove_hook

    remove_hook(model, when, event, fn)


async def test_hooks_are_inherited_by_subclasses(m, registry):
    seen = []

    @m.Animal.before("insert")
    async def note(ctx, doc):
        seen.append(type(doc).__name__)

    try:
        await m.Cat(name="tom").insert()
        await m.Animal(name="generic").insert()
        assert seen == ["Cat", "Animal"]
    finally:
        _unhook(m.Animal, "before", "insert", note)
    with pytest.raises(ConfigurationError):
        m.Animal.before("explode")


class TenantScope(Plugin):
    """A registry-level plugin: every model is confined to one tenant."""

    name = "tenant"

    def __init__(self, tenant):
        self.tenant = tenant
        self.bulk = []

    def on_filter(self, ctx, flt):
        return {"tenant_id": self.tenant}

    def on_insert(self, ctx, doc):
        doc["tenant_id"] = self.tenant

    def on_update(self, ctx, flt, update):
        if ctx.extra.get("upsert"):
            update.setdefault("$setOnInsert", {})["tenant_id"] = self.tenant

    def on_pipeline(self, ctx, pipeline):
        return [*pipeline, {"$addFields": {"seen_by": self.tenant}}]

    async def before_bulk(self, ctx, operations):
        self.bulk.append(("before", len(operations)))

    async def after_bulk(self, ctx, operations):
        self.bulk.append(("after", len(operations)))


async def test_registry_level_plugin_applies_to_every_model(m, make_registry):
    scope = TenantScope("t1")
    await make_registry(m.User, m.Article, plugins=[scope])
    await m.User(name="mine").insert()
    await m.User.collection().insert_one({"name": "theirs", "tenant_id": "t2"})
    assert [u.name for u in await m.User.find()] == ["mine"]
    assert await m.User.count() == 1
    assert await m.User.find().update(age=5) == 1
    assert (await m.User.collection().find_one({"name": "theirs"})).get("age") is None
    assert await m.User.find().unscoped("tenant").count() == 2
    rows = await m.User.aggregate([{"$project": {"name": 1}}])
    assert [(r["name"], r["seen_by"]) for r in rows] == [("mine", "t1")]
    await m.User.insert_many([m.User(name="x"), m.User(name="y")])
    assert scope.bulk == [("before", 2), ("after", 2)]
    assert (await m.User.upsert(Q.name == "up", age=1)).name == "up"
    assert (await m.User.collection().find_one({"name": "up"}))["tenant_id"] == "t1"
    # both scopes combine on a model that also soft-deletes
    assert m.Article.find().compile()["filter"] == {"tenant_id": "t1", "deleted_at": None}


async def test_named_scopes(m, registry):
    @m.User.scope
    def adults(query, minimum=18):
        return query.filter(Q.age >= minimum)

    @m.User.scope
    def named(query, name):
        return Q.name == name  # a condition is accepted too

    await m.User.insert_many(
        [m.User(name="kid", age=5), m.User(name="ann", age=30), m.User(name="bob", age=70)]
    )
    assert sorted(u.name for u in await m.User.find().adults()) == ["ann", "bob"]
    assert [u.name for u in await m.User.find().adults(65)] == ["bob"]
    assert [u.name for u in await m.User.find().adults().named("ann").lean()] == ["ann"]
    with pytest.raises(AttributeError, match="named scope"):
        m.User.find().nonexistent()
