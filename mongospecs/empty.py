class EmptyObject(str):
    def __repr__(self):
        return "Empty"

    def __bool__(self):
        return False

    def __str__(self):
        return "Empty"


Empty = EmptyObject()
