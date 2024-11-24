from datetime import datetime, timezone
from typing import Any, Optional

from msgspec import Struct, field

from mongospecs.msgspec import SubSpec

__all__ = [
    "Dragon",
    "Inventory",
    "Lair",
    "ComplexDragon",
    "MonitoredDragon",
]


class Dragon(Struct):
    """
    A dragon.
    """

    name: str
    breed: Optional[str] = None

    def _get_dummy_prop(self):
        return self._dummy_prop

    def _set_dummy_prop(self, value):
        self._dummy_prop = True

    dummy_prop = property(_get_dummy_prop, _set_dummy_prop)


class Inventory(SubSpec):
    """
    An inventory of items kept within a lair.
    """

    gold: int = 0
    skulls: int = 0


class Lair(Struct):
    """
    A lair in which a dragon resides.
    """

    name: str = ""
    inventory: Inventory = field(default_factory=Inventory)


class ComplexDragon(Dragon, kw_only=True):
    _collection = "ComplexDragon"

    dob: Optional[datetime] = None
    lair: Lair = field(default_factory=Lair)
    visited_lairs: list[Lair] = []
    traits: list[str] = []
    misc: Optional[Any] = ""

    _default_projection = {"lair": {"$ref": Lair, "inventory": {"$sub": Inventory}}}


class MonitoredDragon(Dragon):
    created: Optional[datetime] = None
    modified: Optional[datetime] = None

    @staticmethod
    def timestamp_insert(sender, specs):
        """
        Timestamp the created and modified fields for all documents. This method
        should be bound to a spec class like so:

        ```
        MySpecClass.listen('insert', MySpecClass.timestamp_insert)
        ```
        """
        for spec in specs:
            timestamp = datetime.now(timezone.utc)
            spec.created = timestamp
            spec.modified = timestamp

    @staticmethod
    def timestamp_update(sender, specs):
        """
        Timestamp the modified field for all documents. This method should be
        bound to a spec class like so:

        ```
        MySpecClass.listen('update', MySpecClass.timestamp_update)
        ```
        """
        for spec in specs:
            spec.modified = datetime.now(timezone.utc)
