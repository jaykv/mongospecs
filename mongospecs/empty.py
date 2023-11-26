class EmptyObject:
    def __repr__(self):
        return "Empty"

    def __bool__(self):
        return False


Empty = EmptyObject()
