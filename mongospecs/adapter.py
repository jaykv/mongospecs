from typing import TypeVar

from .attrs import Spec as AttrsSpec
from .base import SpecBase
from .msgspec import Spec as MsgspecSpec
from .pydantic import Spec as PydanticSpec

SPECS_ADAPTER = {"attrs": AttrsSpec, "msgspec": MsgspecSpec, "pydantic": PydanticSpec}

T = TypeVar("T")


class SpecAdapterBase(SpecBase):
    def __init__(self, obj: object) -> None:
        self.obj = obj
