# `mongospecs.helpers.query` Module

This module provides a set of helpers to simplify the creation of MongoDB queries using a more Pythonic syntax.

## Classes

*   **`Condition(q, value, operator)`:** Represents a query condition in the form `{path: {operator: value}}`.
    *   `q`: The field path (can be `None` for top-level conditions).
    *   `value`: The value to compare against.
    *   `operator`: The comparison operator (e.g., `$eq`, `$gt`, `$lt`).
    *   `to_dict()`: Converts the condition to a dictionary suitable for use as a filter in PyMongo.

*   **`QMeta`:** A metaclass for the `Q` class. It allows using comparison operators directly on `Q` (e.g., `Q == value`, `Q > value`).

*   **`Q`:** The starting point for building queries.
    *   `__init__(path)`: Initializes with a field path.
    *   Comparison operators (`__eq__`, `__ge__`, `__gt__`, `__le__`, `__lt__`, `__ne__`): Create `Condition` objects.
    *   `__getattr__(name)` and `__getitem__(name)`: Allow chaining attribute access to build nested field paths (e.g., `Q.field1.field2`, `Q["field1"]["field2"]`).

*   **`Group`:** A base class for operators that group multiple conditions (e.g., `And`, `Or`, `Nor` in `ops.py`).
    *   `operator`: The grouping operator (e.g., `$and`, `$or`).
    *   `__init__(*conditions)`: Initializes with a list of conditions.
    *   `to_dict()`: Converts the group to a dictionary suitable for use as a filter in PyMongo.

**Example Usage:**

```python
# Example usage (replace with your actual classes)
from mongospecs.helpers.query import Q

# ... (your class definitions) ...

# Creating conditions
condition1 = Q.field1 == "value1"
condition2 = Q.field2 > 10
condition3 = Q.nested.field == "nested_value"

# Using conditions in a filter (assuming you have a Spec class and a collection)
# results = MySpec.many({"$and": [condition1.to_dict(), condition2.to_dict()]})

# Or, using the Group class from ops.py (more convenient)
from mongospecs.helpers.ops import And
and_group = And(condition1, condition2, condition3)
# results = MySpec.many(and_group.to_dict())
```

**Key Features:**

*   Provides a more intuitive way to build queries compared to directly writing MongoDB filter dictionaries.
*   Leverages Python's operator overloading for a concise syntax.
*   Supports nested field paths.
*   Integrates well with the `ops.py` module for more complex query construction.
