# `mongospecs.helpers.empty` Module

This module defines a special class `EmptyObject` and an instance of it called `Empty`. This is used as a placeholder or sentinel value within the `mongospecs` library, likely to represent the absence of a value or an unset field.

## `EmptyObject` Class

The `EmptyObject` class is a subclass of `str` with the following behavior:

*   **`__repr__()`:** Returns the string "Empty".
*   **`__bool__()`:** Returns `False`, meaning it evaluates to false in boolean contexts.
*   **`__str__()`:** Returns the string "Empty".

## `Empty` Instance

`Empty` is a singleton instance of the `EmptyObject` class.

**Usage Example (Illustrative):**

```python
# Example usage (replace with your actual classes)
from mongospecs.helpers.empty import Empty

# ... (your class definitions) ...

# Example checking for an empty value
if some_value is Empty:
    print("The value is empty")
```

**Purpose:**

The `Empty` object likely serves as a way to distinguish between a field that is explicitly set to `None` and a field that is not set at all. This can be useful in situations where `None` might be a valid value, but you need a way to indicate the absence of any value.
