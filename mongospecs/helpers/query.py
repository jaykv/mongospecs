"""
A set of helpers to simplify the creation of MongoDB queries.
"""

import typing as t

__all__ = [
    # Queries
    "Q",
]


# Queries
class Condition:
    """
    A query condition of the form `{path: {operator: value}}`.
    """

    def __init__(self, q: t.Optional[str], value: t.Any, operator: str):
        self.q = q
        self.value = value
        self.operator = operator

    def to_dict(self) -> dict[str, dict[str, t.Any]]:
        """Return a dictionary suitable for use with pymongo as a filter"""
        if self.q is None:
            return {self.operator: self.value}
        if self.operator == "$eq":
            return {self.q: self.value}
        return {self.q: {self.operator: self.value}}


class QMeta(type):
    """
    Meta-class for query builder.
    """

    def __getattr__(self, name: str) -> "Q":
        return Q(name)

    def __getitem__(self, name: str) -> "Q":
        return Q(name)

    def __eq__(self, other: t.Any) -> Condition:  # type: ignore[override]
        return Condition(None, other, "$eq")

    def __ge__(self, other: t.Any) -> Condition:
        return Condition(None, other, "$gte")

    def __gt__(self, other: t.Any) -> Condition:
        return Condition(None, other, "$gt")

    def __le__(self, other: t.Any) -> Condition:
        return Condition(None, other, "$lte")

    def __lt__(self, other: t.Any) -> Condition:
        return Condition(None, other, "$lt")

    def __ne__(self, other: t.Any) -> Condition:  # type: ignore[override]
        return Condition(None, other, "$ne")


class Q(metaclass=QMeta):
    """
    Start point for the query creation, the Q class is a special type of class
    that's typically initialized by appending an attribute, for example:

        Q.hit_points > 100

    """

    def __init__(self, path: str):
        self._path = path

    def __eq__(self, other: t.Any) -> Condition:  # type: ignore[override]
        return Condition(self._path, other, "$eq")

    def __ge__(self, other: t.Any) -> Condition:
        return Condition(self._path, other, "$gte")

    def __gt__(self, other: t.Any) -> Condition:
        return Condition(self._path, other, "$gt")

    def __le__(self, other: t.Any) -> Condition:
        return Condition(self._path, other, "$lte")

    def __lt__(self, other: t.Any) -> Condition:
        return Condition(self._path, other, "$lt")

    def __ne__(self, other: t.Any) -> Condition:  # type: ignore[override]
        return Condition(self._path, other, "$ne")

    def __getattr__(self, name: str) -> "Q":
        self._path = "{0}.{1}".format(self._path, name)
        return self

    def __getitem__(self, name: str) -> "Q":
        self._path = "{0}.{1}".format(self._path, name)
        return self


class Group:
    """
    The Group class is used as a base class for operators that group together
    two or more conditions.
    """

    operator = ""

    def __init__(self, *conditions: t.Any):
        self.conditions = conditions

    def to_dict(self) -> dict[str, t.Any]:
        """Return a dictionary suitable for use with pymongo as a filter"""
        raw_conditions = []
        for condition in self.conditions:
            if isinstance(condition, (Condition, Group)):
                raw_conditions.append(condition.to_dict())
            else:
                raw_conditions.append(condition)
        return {self.operator: raw_conditions}
