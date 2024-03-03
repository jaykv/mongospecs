"""
A set of helpers to simplify the creation of MongoDB queries.
"""

from typing import Any

__all__ = [
    # Queries
    "Q",
]


# Queries
class Condition:
    """
    A query condition of the form `{path: {operator: value}}`.
    """

    def __init__(self, q, value, operator):
        self.q = q
        self.value = value
        self.operator = operator

    def to_dict(self) -> dict[str, dict[str, Any]]:
        """Return a dictionary suitable for use with pymongo as a filter"""
        if self.operator == "$eq":
            return {self.q: self.value}
        if self.q is None:
            return {self.operator: self.value}
        return {self.q: {self.operator: self.value}}


class QMeta(type):
    """
    Meta-class for query builder.
    """

    def __getattr__(self, name):
        return Q(name)

    def __getitem__(self, name):
        return Q(name)

    def __eq__(self, other):
        return Condition(None, other, "$eq")

    def __ge__(self, other):
        return Condition(None, other, "$gte")

    def __gt__(self, other):
        return Condition(None, other, "$gt")

    def __le__(self, other):
        return Condition(None, other, "$lte")

    def __lt__(self, other):
        return Condition(None, other, "$lt")

    def __ne__(self, other):
        return Condition(None, other, "$ne")


class Q(metaclass=QMeta):
    """
    Start point for the query creation, the Q class is a special type of class
    that's typically initialized by appending an attribute, for example:

        Q.hit_points > 100

    """

    def __init__(self, path):
        self._path = path

    def __eq__(self, other):
        return Condition(self._path, other, "$eq")

    def __ge__(self, other):
        return Condition(self._path, other, "$gte")

    def __gt__(self, other):
        return Condition(self._path, other, "$gt")

    def __le__(self, other):
        return Condition(self._path, other, "$lte")

    def __lt__(self, other):
        return Condition(self._path, other, "$lt")

    def __ne__(self, other):
        return Condition(self._path, other, "$ne")

    def __getattr__(self, name):
        self._path = "{0}.{1}".format(self._path, name)
        return self

    def __getitem__(self, name):
        self._path = "{0}.{1}".format(self._path, name)
        return self


class Group:
    """
    The Group class is used as a base class for operators that group together
    two or more conditions.
    """

    operator = ""

    def __init__(self, *conditions):
        self.conditions = conditions

    def to_dict(self):
        """Return a dictionary suitable for use with pymongo as a filter"""
        raw_conditions = []
        for condition in self.conditions:
            if isinstance(condition, (Condition, Group)):
                raw_conditions.append(condition.to_dict())
            else:
                raw_conditions.append(condition)
        return {self.operator: raw_conditions}
