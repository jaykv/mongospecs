import typing as t

from pymongo import ASCENDING, DESCENDING

from mongospecs.helpers.query import Condition, Group, Q
from mongospecs.utils import deep_merge, to_refs

__all__ = [
    # Operators
    "All",
    "ElemMatch",
    "Exists",
    "In",
    "Not",
    "NotIn",
    "Size",
    "Type",
    # Groups
    "And",
    "Or",
    "Nor",
    # Sorting
    "SortBy",
]

# Operators


def All(q: Q, value: list[t.Any]) -> Condition:
    """
    The All operator selects documents where the value of the field is an list
    that contains all the specified elements.
    """
    return Condition(q._path, to_refs(value), "$all")


def ElemMatch(q: Q, *conditions: t.Union[Condition, Group, dict[str, t.Any]]) -> Condition:
    """
    The ElemMatch operator matches documents that contain an array field with at
    least one element that matches all the specified query criteria.
    """
    new_condition: dict[str, t.Any] = {}
    for condition in conditions:
        if isinstance(condition, (Condition, Group)):
            condition = condition.to_dict()

        deep_merge(condition, new_condition)

    return Condition(q._path, new_condition, "$elemMatch")


def Exists(q: Q, value: bool) -> Condition:
    """
    When exists is True, Exists matches the documents that contain the field,
    including documents where the field value is null. If exists is False, the
    query returns only the documents that do not contain the field.
    """
    return Condition(q._path, value, "$exists")


def In(q: Q, value: list[t.Any]) -> Condition:
    """
    The In operator selects the documents where the value of a field equals any
    value in the specified list.
    """
    return Condition(q._path, to_refs(value), "$in")


def Not(condition: Condition) -> Condition:
    """
    Not performs a logical NOT operation on the specified condition and selects
    the documents that do not match. This includes documents that do not contain
    the field.
    """
    return Condition(condition.q, {condition.operator: condition.value}, "$not")


def NotIn(q: Q, value: list[t.Any]) -> Condition:
    """
    The NotIn operator selects documents where the field value is not in the
    specified list or the field does not exists.
    """
    return Condition(q._path, to_refs(value), "$nin")


def Size(q: Q, value: int) -> Condition:
    """
    The Size operator matches any list with the number of elements specified by
    size.
    """
    return Condition(q._path, value, "$size")


def Type(q: Q, value: int) -> Condition:
    """
    Type selects documents where the value of the field is an instance of the
    specified BSON type.
    """
    return Condition(q._path, value, "$type")


# Groups


class And(Group):
    """
    And performs a logical AND operation on a list of two or more conditions and
    selects the documents that satisfy all the conditions.
    """

    operator = "$and"


class Or(Group):
    """
    The Or operator performs a logical OR operation on a list of two or more
    conditions and selects the documents that satisfy at least one of the
    conditions.
    """

    operator = "$or"


class Nor(Group):
    """
    Nor performs a logical NOR operation on a list of one or more conditions and
    selects the documents that fail all the conditions.
    """

    operator = "$nor"


# Sorting


def SortBy(*qs: Q) -> list[tuple[str, int]]:
    """Convert a list of Q objects into list of sort instructions"""

    sort = []
    for q in qs:
        if q._path.endswith(".desc"):
            sort.append((q._path[:-5], DESCENDING))
        else:
            sort.append((q._path, ASCENDING))
    return sort
