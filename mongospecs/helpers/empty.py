class EmptyObject(str):
    def __repr__(self) -> str:
        return "Empty"

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return "Empty"


Empty = EmptyObject()
