"""Regression tests for the second QA review of v2 (issue numbers refer to that review)."""

import typing as t

import pytest

from mongospecs import (
    ConfigurationError,
    DocumentNotFound,
    DuplicateKeyError,
    Inc,
    Index,
    MongoSpecsError,
    Nor,
    Or,
    Q,
    QueryError,
    SpecConfig,
    TransactionError,
    ValidationError,
    meta_of,
)
from mongospecs.manager import UpdateCount

# -- 1: sparse unique index and optional fields ------------------------------------------------


async def test_1_absent_optional_fields_do_not_collide_on_a_sparse_unique_index(m, registry):
    await registry.apply_indexes()  # User has Index("email", unique=True, sparse=True)
    result = await m.User.insert_many([m.User(name="n1"), m.User(name="n2")])
    assert len(result.inserted) == 2
    await m.User(name="n3").insert()
    raw = await m.User.collection().find_one({"name": "n1"})
    assert "email" not in raw  # None means absent: the field is not stored
    # uniqueness still holds where a value is present
    await m.User(name="a", email="a@x.io").insert()
    with pytest.raises(DuplicateKeyError):
        await m.User(name="b", email="a@x.io").insert()
    # clearing a value un-stores it, through save() and through an atomic update alike
    a = await m.User.find(Q.name == "a").one()
    a.email = None
    assert a.changes() == {"$unset": {"email": ""}}
    await a.save()
    await m.User(name="c", email="c@x.io").insert()
    await m.User.update(Q.name == "c", email=None)
    assert await m.User.collection().count_documents({"email": {"$exists": True}}) == 0
    assert await m.User.count(Q.email == None) == 5  # noqa: E711 - None still matches "no value"
    loaded = await m.User.find(Q.name == "c").one()
    assert loaded.email is None and loaded.changes() == {}


# -- 4: field names that shadow the document API -------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["count", "update", "delete", "find", "save", "get", "insert", "changes", "is_new", "scope", "watch"],
)
def test_4_fields_may_not_shadow_the_document_api(m, name):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the backends themselves only warn, if at all
        try:
            if m.backend == "attrs":
                import attrs

                Shadow = attrs.make_class(
                    "Shadow", {name: attrs.field(default=0, type=int)}, bases=(m.Spec,), kw_only=True
                )
            elif m.backend == "pydantic":
                import pydantic

                Shadow = pydantic.create_model("Shadow", __base__=m.Spec, **{name: (int, 0)})
            else:
                import msgspec

                Shadow = msgspec.defstruct("Shadow", [(name, int, 0)], bases=(m.Spec,), kw_only=True)
        except Exception as exc:  # a backend may refuse the class outright, which is fine too
            assert name in str(exc)
            return
    with pytest.raises(ConfigurationError, match=f"'{name}'.*document API"):
        meta_of(Shadow)


def test_4_ordinary_and_id_fields_are_fine(m):
    assert "id" in meta_of(m.User).fields and "name" in meta_of(m.User).fields


# -- 6: nested transactions ----------------------------------------------------------------------


async def test_6_a_nested_transaction_is_refused_unless_declared_independent(m, registry):
    async def inner(tx):
        await m.User(name="inner").insert(session=tx)

    async def outer(tx):
        await m.User(name="outer").insert(session=tx)
        await registry.transaction(inner)  # would commit on its own, whatever happens to the outer one
        raise RuntimeError("outer aborts")

    with pytest.raises(TransactionError, match="inside another transaction"):
        await registry.transaction(outer)
    assert await m.User.count() == 0  # nothing slipped through

    async def outer_explicit(tx):
        await m.User(name="outer").insert(session=tx)
        await registry.transaction(inner, independent=True)  # stated on purpose
        raise RuntimeError("outer aborts")

    with pytest.raises(RuntimeError):
        await registry.transaction(outer_explicit)
    assert [u.name for u in await m.User.find()] == ["inner"]
    # sequential transactions and a transaction after a failed one are unaffected
    await registry.transaction(inner)
    assert await m.User.count() == 2


# -- 7: UpdateCount formats like an int -----------------------------------------------------------


def test_7_update_count_prints_like_an_int():
    import json
    import pickle

    n = UpdateCount(5, 3)
    assert str(n) == "3" and f"{n}" == "3" and "%s" % n == "3" and "%d" % n == "3"  # noqa: UP031 and f"{n:>4}" == "   3"
    assert repr(n) == "UpdateCount(matched=5, modified=3)"
    assert n == 3 and n + 1 == 4 and json.dumps({"n": n}) == '{"n": 3}'
    restored = pickle.loads(pickle.dumps(n))
    assert restored == 3 and restored.matched == 5


# -- smaller findings ------------------------------------------------------------------------------


async def test_casting_follows_one_rule_on_every_backend(m, registry):
    """Coercion is lax everywhere: text that reads as the field's type is accepted."""
    user = await m.User(name="ann", age=12).insert()
    assert await m.User.count(Q.age == "12") == 1
    assert await m.User.count(Q.age.in_(["12", 13])) == 1
    with pytest.raises(QueryError):
        await m.User.count(Q.age == "twelve")
    assert await m.User.count(Q.age == 12) == 1 and await m.User.count(Q.name == "ann") == 1
    assert user.age == 12


async def test_malformed_queries_are_query_errors_not_server_errors(m, registry):
    await m.User(name="ann").insert()
    for bad in (lambda: Or(), lambda: Nor(), lambda: Q.name.prefix({"$gt": ""}), lambda: Q.name.contains(5)):
        with pytest.raises(QueryError):
            await m.User.find(bad()).all()
    with pytest.raises(QueryError):
        await m.User.find().sort("address.$").all()
    with pytest.raises(QueryError):
        await m.User.find().sort("$natural").all()
    # the server refusing an update that does not fit the stored value is the caller's error
    await m.User.collection().update_one({"name": "ann"}, {"$set": {"address": {"zip": "abc"}}})
    with pytest.raises(QueryError):
        await m.User.update(Q.name == "ann", Inc({"address.zip": 1}))


async def test_include_is_validated_when_the_query_is_built(m, registry):
    with pytest.raises(QueryError, match="not a relation"):
        m.Article.find().include("nope")  # an empty collection used to hide this
    with pytest.raises(QueryError, match="not a relation"):
        m.Article.find().include("author.nope")
    with pytest.raises(QueryError, match="strategy"):
        m.Article.find().include("author", strategy="weird")
    assert await m.Article.find().include("author").all() == []


async def test_invalid_stored_documents_can_be_skipped_or_returned_raw(m, registry):
    await m.User.insert_many([m.User(name="ok1", age=1), m.User(name="ok2", age=2)])
    await m.User.collection().insert_one({"name": "corrupt", "age": "not a number"})
    with pytest.raises(ValidationError):
        await m.User.find().all()  # the default: a corrupt document is an error
    skipped = await m.User.find().sort("name").on_invalid("skip").all()
    assert [u.name for u in skipped] == ["ok1", "ok2"]
    mixed = await m.User.find().sort("name").on_invalid("raw").all()
    assert [u["name"] if isinstance(u, dict) else u.name for u in mixed] == ["corrupt", "ok1", "ok2"]
    assert isinstance(mixed[0], dict) and mixed[0]["age"] == "not a number"
    lean = await m.User.find().lean().on_invalid("skip").all()
    assert len(lean) == 2
    with pytest.raises(QueryError):
        m.User.find().on_invalid("ignore")


async def test_upsert_revision_is_documented_behaviour(m, registry):
    """An upsert is a write, so the document it creates starts at revision 1; insert() starts at 0."""
    assert (await m.Article(title="i").insert()).rev == 0
    assert (await m.Article.upsert(Q.title == "u", Inc(views=1))).rev == 1


def test_unused_names_keep_imports_honest():
    assert Index and SpecConfig and DocumentNotFound and MongoSpecsError and t
