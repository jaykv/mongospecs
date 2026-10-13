"""Golden tests: queries compile to exact find arguments and pipelines."""

import pytest
from bson import ObjectId

from mongospecs import Q, QueryError


async def test_plain_find_arguments(m, registry):
    q = m.User.find(Q.age > 1, Q.name.prefix("a")).sort("-age", Q.name).skip(5).limit(10)
    assert q.compile() == {
        "filter": {"age": {"$gt": 1}, "name": {"$regex": "^a"}},
        "projection": {"secret": 0},
        "sort": [("age", -1), ("name", 1)],
        "skip": 5,
        "limit": 10,
    }


async def test_queries_are_immutable(m, registry):
    base = m.User.find(Q.age > 1)
    narrowed = base.filter(Q.name == "a").sort("name").limit(3).lean()
    assert base.compile()["filter"] == {"age": {"$gt": 1}}
    assert base.compile()["limit"] == 0 and base.compile()["sort"] == []
    assert narrowed.compile()["filter"] == {"age": {"$gt": 1}, "name": "a"}
    assert base.where is not None and base._mode == "tracked" and narrowed._mode == "lean"


async def test_projection_rules(m, registry):
    assert m.User.find().select("+secret").compile()["projection"] is None
    assert m.User.find().select("name", "age").compile()["projection"] == {"name": 1, "age": 1}
    assert m.User.find().select("-age").compile()["projection"] == {"secret": 0, "age": 0}
    assert m.Animal.find().select("name").compile()["projection"] == {"name": 1, "kind": 1}
    assert m.Post.find().select("summary").compile()["projection"] == {"s": 1}


async def test_scopes_are_in_every_filter(m, registry):
    assert m.Article.find().compile()["filter"] == {"deleted_at": None}
    assert m.Article.find(Q.title == "x").compile()["filter"] == {"title": "x", "deleted_at": None}
    assert m.Article.find().unscoped().compile()["filter"] == {}
    assert m.Article.find().unscoped("soft_delete").compile()["filter"] == {}
    assert m.Article.find().unscoped("other").compile()["filter"] == {"deleted_at": None}
    explicit = m.Article.find(Q.deleted_at != None).compile()["filter"]  # noqa: E711
    assert explicit == {"deleted_at": {"$ne": None}}
    assert m.Cat.find().compile()["filter"] == {"kind": "Cat"}
    assert m.Dog.find().compile()["filter"] == {"kind": {"$in": ["doggo", "Puppy"]}}
    assert m.Animal.find().compile()["filter"] == {}


async def test_lookup_pipeline_orders_page_stages_before_joins(m, registry):
    q = (
        m.Article.find(Q.views > 1)
        .sort("-views")
        .skip(2)
        .limit(3)
        .include("author", strategy="lookup", select=["name"])
    )
    assert q.compile() == {
        "pipeline": [
            {"$match": {"views": {"$gt": 1}, "deleted_at": None}},
            {"$sort": {"views": -1}},
            {"$skip": 2},
            {"$limit": 3},
            {
                "$lookup": {
                    "from": "users",
                    "localField": "author",
                    "foreignField": "_id",
                    "as": "__inc_author",
                    "pipeline": [{"$project": {"name": 1}}],
                }
            },
        ]
    }


async def test_lookup_applies_target_scope_and_options(m, registry):
    q = m.Comment.find().include(
        "article", strategy="lookup", match=Q.views > 5, sort=["-views"], per_parent_limit=1
    )
    (stage,) = [s for s in q.compile()["pipeline"] if "$lookup" in s]
    assert stage["$lookup"]["pipeline"] == [
        {"$match": {"views": {"$gt": 5}, "deleted_at": None}},
        {"$sort": {"views": -1}},
        {"$limit": 1},
    ]


async def test_backref_and_nested_lookup(m, registry):
    q = m.Post.find().include("comments", strategy="lookup").include("comment_count", strategy="lookup")
    lookups = [s["$lookup"] for s in q.compile()["pipeline"]]
    assert lookups[0] == {
        "from": "notes",
        "localField": "_id",
        "foreignField": "post",
        "as": "__inc_comments",
    }
    assert lookups[1]["pipeline"] == [{"$count": "n"}] and lookups[1]["foreignField"] == "post"
    nested = m.Comment.find().include("article.author", strategy="lookup").compile()["pipeline"]
    inner = nested[-1]["$lookup"]["pipeline"]
    assert inner[0] == {"$match": {"deleted_at": None}}
    assert inner[-1]["$lookup"]["from"] == "users" and inner[-1]["$lookup"]["localField"] == "author"


async def test_batch_strategy_keeps_a_plain_find(m, registry):
    q = m.Article.find().include("author")
    assert "pipeline" not in q.compile()
    assert "pipeline" not in m.Article.find().include("author", strategy="batch").compile()


async def test_lookup_refused_where_it_cannot_work(m, registry):
    with pytest.raises(QueryError, match="lookup"):
        m.Article.find().include("reviewers", strategy="lookup").compile()
    with pytest.raises(QueryError, match="lookup"):
        m.Bookmark.find().include("target", strategy="lookup").compile()
    with pytest.raises(QueryError, match="lookup"):
        m.Article.find().include("author", strategy="lookup", limit=3).compile()
    with pytest.raises(QueryError, match="not a relation"):
        m.Article.find().include("title").compile()


async def test_option_validation_and_write_guards(m, registry):
    with pytest.raises(QueryError):
        m.User.find().options(nonsense=1)
    with pytest.raises(QueryError):
        m.User.find().skip(-1)
    with pytest.raises(QueryError):
        await m.User.find().limit(1).update(age=1)
    with pytest.raises(QueryError):
        await m.User.find().sort("age").delete()
    assert m.User.find().options(max_time_ms=500, comment="x")._options == {
        "max_time_ms": 500,
        "comment": "x",
    }


async def test_get_casts_id(m, registry):
    oid = ObjectId()
    assert m.User._manager().compile(str(oid)) == {"_id": oid}
    assert m.User._manager().compile(Q.age > 1) == {"age": {"$gt": 1}}
