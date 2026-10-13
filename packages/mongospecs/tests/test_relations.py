import pytest

from mongospecs import DocumentNotFound, NotLoadedError, Q, Ref, relations

STRATEGIES = ("batch", "lookup")


@pytest.fixture
async def data(m, registry):
    ann = await m.User(name="ann", age=30).insert()
    bob = await m.User(name="bob", age=40).insert()
    cy = await m.User(name="cy", age=50).insert()
    a1 = await m.Article(
        title="a1", views=1, author=ann, editors=[bob, cy], reviewers={"lead": cy, "second": ann}
    ).insert()
    a2 = await m.Article(title="a2", views=2, author=bob, editors=[]).insert()
    a3 = await m.Article(title="a3", views=3).insert()
    return {"ann": ann, "bob": bob, "cy": cy, "a1": a1, "a2": a2, "a3": a3}


async def test_refs_are_unloaded_until_included_or_fetched(m, registry, data):
    art = await m.Article.get(data["a1"].id)
    assert isinstance(art.author, Ref) and art.author.id == data["ann"].id and not art.author.loaded
    assert art.author == data["ann"].id and art.author == Ref(data["ann"].id)
    with pytest.raises(NotLoadedError):
        _ = art.author.doc
    assert (await art.author.fetch()).name == "ann"
    assert art.author.loaded and art.author.doc.name == "ann"
    assert art.changes() == {}  # loading a reference is not a change


@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_include_one_and_list(m, registry, data, strategy):
    arts = (
        await m.Article.find()
        .sort("title")
        .include("author", strategy=strategy)
        .include("editors", strategy=strategy)
    )
    assert [a.author.doc.name if a.author else None for a in arts] == ["ann", "bob", None]
    assert [e.doc.name for e in arts[0].editors] == ["bob", "cy"]  # order follows the stored ids
    assert arts[1].editors == []
    assert all(a.changes() == {} for a in arts)
    assert not arts[0].author.doc.is_new  # related documents are tracked like their parents
    lean = await m.Article.find(Q.title == "a1").lean().include("author", strategy=strategy).one()
    assert lean.is_new and lean.author.doc.is_new


@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_include_options(m, registry, data, strategy):
    arts = await m.Article.find(Q.title == "a1").include(
        "editors", strategy=strategy, match=Q.age > 40, select=["name"]
    )
    (art,) = arts
    assert [(e.id, e.loaded) for e in art.editors] == [(data["bob"].id, False), (data["cy"].id, True)]
    assert art.editors[1].doc.name == "cy" and art.editors[1].doc.age == 0  # not selected
    limited = (
        await m.Article.find(Q.title == "a1")
        .include("editors", strategy=strategy, per_parent_limit=1, sort=["name"])
        .one()
    )
    assert sum(e.loaded for e in limited.editors) == 1


async def test_dict_of_refs_and_overall_limit_use_batch(m, registry, data):
    art = await m.Article.find(Q.title == "a1").include("reviewers").one()
    assert {k: r.doc.name for k, r in art.reviewers.items()} == {"lead": "cy", "second": "ann"}
    capped = await m.Article.find().sort("title").include("author", limit=1, sort=["name"])
    assert [a.author.loaded if a.author else None for a in capped] == [True, False, None]


@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_nested_include(m, registry, data, strategy):
    await m.Comment(body="c1", article=data["a1"], author=data["bob"]).insert()
    await m.Comment(body="c2", article=data["a2"]).insert()
    comments = await m.Comment.find().sort("body").include("article.author", strategy=strategy)
    assert comments[0].article.doc.author.doc.name == "ann"
    assert comments[1].article.doc.author.doc.name == "bob"
    both = (
        await m.Comment.find(Q.body == "c1")
        .include("article.author", strategy=strategy)
        .include("author", strategy=strategy)
    )
    assert both[0].author.doc.name == "bob" and both[0].article.doc.author.doc.name == "ann"


@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_included_targets_respect_their_scope(m, registry, data, strategy):
    await m.Comment(body="c", article=data["a1"]).insert()
    await data["a1"].delete()  # soft delete
    comment = await m.Comment.find().include("article", strategy=strategy).one()
    assert comment.article.id == data["a1"].id and not comment.article.loaded


@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_backrefs_and_counts(m, registry, strategy):
    p1, p2 = await m.Post(title="p1").insert(), await m.Post(title="p2").insert()
    await m.Note.insert_many(
        [
            m.Note(text="n1", post=p1, rank=2),
            m.Note(text="n2", post=p1, rank=1),
            m.Note(text="n3", post=p2, rank=5),
        ]
    )
    posts = (
        await m.Post.find()
        .sort("title")
        .include("comments", strategy=strategy, sort=["rank"])
        .include("comment_count", strategy=strategy)
    )
    assert [[n.text for n in p.comments] for p in posts] == [["n2", "n1"], ["n3"]]
    assert [type(n) for n in posts[0].comments] == [m.Note, m.Note]
    assert [p.comment_count for p in posts] == [2, 1]
    assert all(p.changes() == {} for p in posts)
    plain = await m.Post.find().sort("title").all()
    assert plain[0].comments == [] and plain[0].comment_count == 0
    one = (
        await m.Post.find(Q.title == "p1")
        .include("comments", strategy=strategy, per_parent_limit=1, sort=["-rank"])
        .one()
    )
    assert [n.text for n in one.comments] == ["n1"]
    matched = (
        await m.Post.find(Q.title == "p1").include("comments", strategy=strategy, match=Q.rank > 1).one()
    )
    assert [n.text for n in matched.comments] == ["n1"]
    # a reverse relation is never written
    posts[0].title = "renamed"
    assert posts[0].changes() == {"$set": {"title": "renamed"}}
    await posts[0].save()
    raw = await m.Post.collection().find_one({"_id": p1.id})
    assert "comments" not in raw and "comment_count" not in raw


async def test_polymorphic_reference(m, registry, data):
    video = await m.Video(title="v", seconds=60).insert()
    await m.Bookmark(label="art", target=data["a2"]).insert()
    await m.Bookmark(label="vid", target=video).insert()
    marks = await m.Bookmark.find().sort("label").include("target")
    assert type(marks[0].target.doc) is m.Article and marks[0].target.doc.title == "a2"
    assert type(marks[1].target.doc) is m.Video and marks[1].target.doc.seconds == 60


async def test_raw_mode_include_replaces_ids_with_documents(m, registry, data):
    raw = await m.Article.find(Q.title == "a1").raw().include("author").include("editors").one()
    assert raw["author"]["name"] == "ann"
    assert [e["name"] for e in raw["editors"]] == ["bob", "cy"]


async def test_both_strategies_give_identical_results(m, registry, data):
    def shape(arts):
        return [
            (
                a.title,
                a.author.doc.name if a.author and a.author.loaded else None,
                [e.doc.name for e in a.editors if e.loaded],
            )
            for a in arts
        ]

    batch = (
        await m.Article.find()
        .sort("title")
        .include("author", strategy="batch")
        .include("editors", strategy="batch")
    )
    lookup = (
        await m.Article.find()
        .sort("title")
        .include("author", strategy="lookup")
        .include("editors", strategy="lookup")
    )
    assert shape(batch) == shape(lookup)
    assert relations.DEFAULT_STRATEGY in ("batch", "lookup")


async def test_cascade_rules(m, registry, data):
    await m.Comment.insert_many(
        [m.Comment(body="c1", article=data["a1"]), m.Comment(body="c2", article=data["a2"])]
    )
    # nullify + pull: deleting a user clears author and removes them from editors
    await data["bob"].delete()
    a1, a2 = await m.Article.get(data["a1"].id), await m.Article.get(data["a2"].id)
    assert [e.id for e in a1.editors] == [data["cy"].id]
    assert a2.author is None and a1.author.id == data["ann"].id
    # cascade: hard-deleting an article deletes its comments
    await a1.delete(hard=True)
    assert [c.body for c in await m.Comment.find()] == ["c2"]
    # soft delete does not cascade
    await a2.delete()
    assert await m.Comment.count() == 1
    # filter-based hard delete cascades as well
    assert await m.Article.find().unscoped().delete(hard=True) == 2  # a2 (soft-deleted) and a3
    assert await m.Comment.count() == 0


async def test_cascade_is_atomic(m, registry, data):
    await m.Comment(body="c1", article=data["a3"]).insert()

    @m.Article.before("delete")
    async def noop(ctx, doc):
        pass

    from mongospecs.meta import remove_hook

    remove_hook(m.Article, "before", "delete", noop)
    # make the final delete fail after the cascade ran: the article disappears mid-flight
    art = await m.Article.get(data["a3"].id)
    await m.Article.collection().delete_one({"_id": art.id})
    with pytest.raises(DocumentNotFound):
        await art.delete(hard=True)
    assert await m.Comment.count() == 1  # the cascade was rolled back with the failed delete


async def test_graph_lookup(m, registry):
    root = await m.Category(name="root").insert()
    mid = await m.Category(name="mid", parent=root).insert()
    leaf = await m.Category(name="leaf", parent=mid).insert()
    await m.Category(name="other").insert()
    (result,) = await m.Category.find(Q.id == leaf.id).graph("parent")
    assert result.doc.name == "leaf"
    assert [n.name for n in result.nodes] == ["mid", "root"] and result.depths == [0, 1]
    (shallow,) = await m.Category.find(Q.id == leaf.id).graph("parent", max_depth=0)
    assert [n.name for n in shallow.nodes] == ["mid"]
    (restricted,) = await m.Category.find(Q.id == leaf.id).graph("parent", match=Q.name != "root")
    assert [n.name for n in restricted.nodes] == ["mid"]
    assert type(result.nodes[0]) is m.Category


async def test_union_with(m, registry, data):
    await m.Video.insert_many([m.Video(title="a0", seconds=1), m.Video(title="z9", seconds=2)])
    mixed = await m.Article.find(Q.views >= 2).union(m.Video.find()).sort("title")
    assert [(type(d).__name__, d.title) for d in mixed] == [
        ("Video", "a0"),
        ("Article", "a2"),
        ("Article", "a3"),
        ("Video", "z9"),
    ]
    page = await m.Article.find().union(m.Video.find(Q.seconds > 1)).sort("-title").skip(1).limit(2)
    assert [d.title for d in page] == ["a3", "a2"]
    await data["a3"].delete()  # scopes apply per branch
    assert "a3" not in [d.title for d in await m.Article.find().union(m.Video.find())]
