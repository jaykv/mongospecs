from typing import Any

__all__ = ["deep_merge", "to_refs"]


def deep_merge(source: dict[str, Any], dest: dict[str, Any]) -> None:
    """
    Deep merges source dict into dest dict.

    This code was taken directly from the mongothon project:
    https://github.com/gamechanger/mongothon/tree/master/mongothon
    """
    for key, value in source.items():
        if key in dest:
            if isinstance(value, dict) and isinstance(dest[key], dict):
                deep_merge(value, dest[key])
                continue
            elif isinstance(value, list) and isinstance(dest[key], list):
                for item in value:
                    if item not in dest[key]:
                        dest[key].append(item)
                continue
        dest[key] = value


def to_refs(value: Any) -> Any:
    from mongospecs.msgspec import Spec, SubSpec

    """Convert all Frame instances within the given value to Ids"""
    # Frame
    if isinstance(value, Spec):
        return value._id

    # SubFrame
    elif isinstance(value, SubSpec):
        return to_refs(value.to_dict())

    # Lists
    elif isinstance(value, (list, tuple)):
        return [to_refs(v) for v in value]

    # Dictionaries
    elif isinstance(value, dict):
        return {k: to_refs(v) for k, v in value.items()}

    return value
