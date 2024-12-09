import pytest

from mongospecs.base import SpecBase


@pytest.mark.parametrize(
    "parent_dict, paths, expected_result",
    [
        # Happy path tests
        ({"a": "something"}, ["a"], {}),  # Remove only key
        ({"a": 1, "b": {"c": 2}}, ["b.c"], {"a": 1, "b": {}}),  # Remove nested key
        ({"a": 1, "b": {"c": 2}}, ["a"], {"b": {"c": 2}}),  # Remove top-level key
        ({"a": {"b": {"c": 3}}}, ["a.b.c"], {"a": {"b": {}}}),  # Deeply nested key
        # Edge cases
        ({}, ["a"], {}),  # Empty dictionary
        ({"a": 1}, [], {"a": 1}),  # No paths to remove
        ({"a": {"b": {"c": 3}}}, ["a.b.d"], {"a": {"b": {"c": 3}}}),  # Non-existent key
        # Error cases
        ({"a": 1}, ["b.c"], {"a": 1}),  # Non-existent nested path
        ({"a": {"b": 2}}, ["a.b.c"], {"a": {"b": 2}}),  # Non-existent deeper path
    ],
    ids=[
        "remove_only_key",
        "remove_nested_key",
        "remove_top_level_key",
        "deeply_nested_key",
        "empty_dict",
        "no_paths",
        "non_existent_key",
        "non_existent_nested_path",
        "non_existent_deeper_path",
    ],
)
def test_remove_keys(parent_dict, paths, expected_result):
    SpecBase._remove_keys(parent_dict, paths)

    assert parent_dict == expected_result
