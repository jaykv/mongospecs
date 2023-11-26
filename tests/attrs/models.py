from datetime import datetime, timezone
from typing import Any, Optional

from attrs import define, field

from mongospecs.attrs import Spec, SubSpec

__all__ = [
    "Dragon",
    "Inventory",
    "Lair",
    "ComplexDragon",
    "MonitoredDragon",
]


@define
class Dragon(Spec):
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


@define
class Inventory(SubSpec):
    """
    An inventory of items kept within a lair.
    """

    gold: int = 0
    skulls: int = 0


@define
class Lair(Spec):
    """
    A lair in which a dragon resides.
    """

    name: str = ""
    inventory: Inventory = field(factory=Inventory)


@define
class ComplexDragon(Dragon):
    _collection = "ComplexDragon"

    dob: Optional[datetime] = None
    lair: Lair = field(factory=Lair)
    visited_lairs: list[Lair] = []
    traits: list[str] = []
    misc: Optional[Any] = ""

    _default_projection = {"lair": {"$ref": Lair, "inventory": {"$sub": Inventory}}}


@define
class MonitoredDragon(Dragon):

    created: Optional[datetime] = None
    modified: Optional[datetime] = None

    @staticmethod
    def timestamp_insert(sender, frames):
        """
        Timestamp the created and modified fields for all documents. This method
        should be bound to a frame class like so:

        ```
        MyFrameClass.listen('insert', MyFrameClass.timestamp_insert)
        ```
        """
        for frame in frames:
            timestamp = datetime.now(timezone.utc)
            frame.created = timestamp
            frame.modified = timestamp

    @staticmethod
    def timestamp_update(sender, frames):
        """
        Timestamp the modified field for all documents. This method should be
        bound to a frame class like so:

        ```
        MyFrameClass.listen('update', MyFrameClass.timestamp_update)
        ```
        """
        for frame in frames:
            frame.modified = datetime.now(timezone.utc)
