"""Golden tests: expressions compile to exact MongoDB filters. No database needed."""

import re

import pytest
from bson import ObjectId

from mongospecs import (
    All,
    And,
    ElemMatch,
    Exists,
    In,
    Nor,
    Not,
    NotIn,
    Or,
    Q,
    QueryError,
    Size,
    SortBy,
    Type,
    meta_of,
    trusted,
)
from mongospecs.expr import SortKey, and_filters, compile_filter, compile_sort


def c(expr, meta=None):
    return compile_filter(expr, meta)


def test_comparisons():
    assert c(Q.a == 1) == {"a": 1}
    assert c(Q.a != 1) == {"a": {"$ne": 1}}
    assert c(Q.a > 1) == {"a": {"$gt": 1}}
    assert c(Q.a >= 1) == {"a": {"$gte": 1}}
    assert c(Q.a < 1) == {"a": {"$lt": 1}}
    assert c(Q.a <= 1) == {"a": {"$lte": 1}}
    assert c(Q.a.b.c == 1) == {"a.b.c": 1}
    assert c(Q["a"]["b"] == 1) == {"a.b": 1}
    assert c(Q.items[0].name == "x") == {"items.0.name": "x"}


def test_operators_and_helpers():
    assert c(Q.a.in_([1, 2])) == c(In(Q.a, [1, 2])) == {"a": {"$in": [1, 2]}}
    assert c(Q.a.nin([1])) == c(NotIn(Q.a, [1])) == {"a": {"$nin": [1]}}
    assert c(Q.a.all([1, 2])) == c(All(Q.a, [1, 2])) == {"a": {"$all": [1, 2]}}
    assert c(Q.a.exists()) == c(Exists(Q.a, True)) == {"a": {"$exists": True}}
    assert c(Q.a.exists(False)) == {"a": {"$exists": False}}
    assert c(Q.a.size(2)) == c(Size(Q.a, 2)) == {"a": {"$size": 2}}
    assert c(Q.a.type_("string")) == c(Type(Q.a, "string")) == {"a": {"$type": "string"}}
    assert c(Q.a.prefix("x.y")) == {"a": {"$regex": "^" + re.escape("x.y")}}
    assert c(Q.a.contains("a+b", ignore_case=True)) == {"a": {"$regex": re.escape("a+b"), "$options": "i"}}
    assert c(Q.a.regex("^x", "m")) == {"a": {"$regex": "^x", "$options": "m"}}


def test_groups_and_operators():
    assert c(And(Q.a == 1, Q.b == 2)) == {"a": 1, "b": 2}
    assert c((Q.a == 1) & (Q.b == 2)) == {"a": 1, "b": 2}
    assert c((Q.a > 1) & (Q.a < 5)) == {"$and": [{"a": {"$gt": 1}}, {"a": {"$lt": 5}}]}
    assert c(Or(Q.a == 1, Q.b == 2)) == c((Q.a == 1) | (Q.b == 2)) == {"$or": [{"a": 1}, {"b": 2}]}
    assert c(Nor(Q.a == 1, Q.b == 2)) == {"$nor": [{"a": 1}, {"b": 2}]}
    assert c(And(Q.a == 1, Or(Q.b == 2, Q.c == 3))) == {"a": 1, "$or": [{"b": 2}, {"c": 3}]}
    assert c(And(Q.a == 1, And(Q.b == 2, Q.c == 3))) == {"a": 1, "b": 2, "c": 3}


def test_negation():
    assert c(~(Q.a == 1)) == c(Not(Q.a == 1)) == {"a": {"$ne": 1}}
    assert c(~(Q.a != 1)) == {"a": 1}
    assert c(~(Q.a > 1)) == {"a": {"$not": {"$gt": 1}}}
    assert c(~Q.a.in_([1])) == {"a": {"$nin": [1]}}
    assert c(~Q.a.prefix("x")) == {"a": {"$not": {"$regex": "^x"}}}
    assert c(~((Q.a == 1) | (Q.b == 2))) == {"$nor": [{"$or": [{"a": 1}, {"b": 2}]}]}


def test_elem_match():
    assert c(Q.items.elem_match(Q.qty > 1, Q.sku == "x")) == {
        "items": {"$elemMatch": {"qty": {"$gt": 1}, "sku": "x"}}
    }
    assert c(ElemMatch(Q.scores, Q > 80, Q < 90)) == {"scores": {"$elemMatch": {"$gt": 80, "$lt": 90}}}


def test_sorting():
    assert SortBy(Q.a.desc(), Q.b, "-c", "d", ("e", -1)) == [
        SortKey("a", -1),
        SortKey("b", 1),
        SortKey("c", -1),
        SortKey("d", 1),
        SortKey("e", -1),
    ]
    assert compile_sort(["-a", Q.b]) == [("a", -1), ("b", 1)]


def test_untrusted_mappings_and_operator_injection():
    assert c({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}
    assert c({"a": {"nested": 1}}) == {"a": {"$eq": {"nested": 1}}}
    for bad in ({"$where": "1"}, {"a": {"$gt": 1}}, {"a": {"b": {"$ne": 1}}}, {"a": [{"$gt": 1}]}):
        with pytest.raises(QueryError):
            c(bad)
    with pytest.raises(QueryError):
        c(Q.a == {"$gt": ""})
    with pytest.raises(QueryError):
        c(Q.a.in_([{"$ne": 1}]))
    assert c(trusted({"a": {"$gt": 1}})) == {"a": {"$gt": 1}}
    assert c(Q.a == 1) is not None and c(None) == {}
    with pytest.raises(QueryError):
        c(Q.a)
    with pytest.raises(QueryError):
        c(Q.a.in_("abc"))


def test_and_filters_merges_or_wraps():
    assert and_filters({"a": 1}, None, {}, {"b": 2}) == {"a": 1, "b": 2}
    assert and_filters({"a": 1}, {"a": 2}) == {"$and": [{"a": 1}, {"a": 2}]}
    assert and_filters() == {}


def test_fields_are_immutable():
    f = Q.a
    g = f.b
    assert repr(f) == "Q.a" and repr(g) == "Q.a.b"
    with pytest.raises(AttributeError):
        f.x = 1


# -- compiled against a model: names are checked and values are cast ----------------


def test_field_names_are_translated_and_checked(m):
    meta = meta_of(m.User)
    oid = ObjectId()
    assert c(Q.id == oid, meta) == {"_id": oid}
    assert c(Q.address.city == "Oslo", meta) == {"address.city": "Oslo"}
    assert c(Q.summary == "x", meta_of(m.Post)) == {"s": "x"}
    with pytest.raises(QueryError, match="no field 'nope'"):
        c(Q.nope == 1, meta)
    with pytest.raises(QueryError, match="no field"):
        c({"nope": 1}, meta)
    with pytest.raises(QueryError, match="reverse relation"):
        c(Q.comments == 1, meta_of(m.Post))
    assert compile_sort(["-id", Q.name], meta) == [("_id", -1), ("name", 1)]
    with pytest.raises(QueryError):
        compile_sort(["nope"], meta)


def test_values_are_cast_to_field_types(m):
    meta = meta_of(m.User)
    oid = ObjectId()
    assert c(Q.id == str(oid), meta) == {"_id": oid}
    assert c(Q.id.in_([str(oid)]), meta) == {"_id": {"$in": [oid]}}
    assert c(Q.color == "red", meta)["color"] in (m.Color.RED, "red")
    with pytest.raises(QueryError, match="cannot cast"):
        c(Q.age == "abc", meta)
    art = meta_of(m.Article)
    assert c(Q.tags == "a", art) == {"tags": "a"}  # scalar against a list field: "contains"
    assert c(Q.tags == ["a", "b"], art) == {"tags": ["a", "b"]}
    assert c(Q.tags.in_(["a"]), art) == {"tags": {"$in": ["a"]}}
    assert c(Q.email == None, meta) == {"email": None}  # noqa: E711


def test_references_are_cast_to_ids(m):
    art = meta_of(m.Article)
    user = m.User(name="u")
    user.id = ObjectId()
    assert c(Q.author == user, art) == {"author": user.id}
    assert c(Q.author == str(user.id), art) == {"author": user.id}
    assert c(Q.editors == user, art) == {"editors": user.id}
    assert c(Q.author.in_([user, user.id]), art) == {"author": {"$in": [user.id, user.id]}}
