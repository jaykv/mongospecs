# `mongospecs.helpers.ops` Module

This module provides helper functions for constructing MongoDB query operators and groups, simplifying the creation of complex queries. It also includes a `SortBy` function for defining sort operations.

## Operators

These functions help create individual query conditions:

*   **`All(q, value)`:**  Matches documents where the field (specified by `q`) is a list containing all elements in `value`. Uses the `$all` operator.
*   **`ElemMatch(q, *conditions)`:** Matches documents containing an array field with at least one element matching all specified `conditions`. Uses the `$elemMatch` operator.
*   **`Exists(q, value)`:** Matches documents that either contain or do not contain the field specified by `q`, depending on the boolean `value`. Uses the `$exists` operator.
*   **`In(q, value)`:** Matches documents where the field (specified by `q`) equals any value in the provided `value` list. Uses the `$in` operator.
*   **`Not(condition)`:** Performs a logical NOT on the provided `condition`. Uses the `$not` operator.
*   **`NotIn(q, value)`:** Matches documents where the field (specified by `q`) is not in the provided `value` list or does not exist. Uses the `$nin` operator.
*   **`Size(q, value)`:** Matches lists with the number of elements specified by `value`. Uses the `$size` operator.
*   **`Type(q, value)`:** Matches documents where the field (specified by `q`) is of the specified BSON type `value`. Uses the `$type` operator.

## Groups

These classes help combine multiple conditions:

*   **`And(*conditions)`:** Performs a logical AND operation on a list of conditions. Uses the `$and` operator.
*   **`Or(*conditions)`:** Performs a logical OR operation on a list of conditions. Uses the `$or` operator.
*   **`Nor(*conditions)`:** Performs a logical NOR operation on a list of conditions. Uses the `$nor` operator.

## Sorting

*   **`SortBy(*qs)`:** Converts a list of `Q` objects into a list of sort instructions for MongoDB. Handles ascending and descending order based on the `Q` object's path (e.g., `.desc` suffix for descending).

**Example Usage:**

```python
# Example usage (replace with your actual classes)
from mongospecs.helpers.ops import All, ElemMatch, Exists, In, Not, NotIn, Size, Type, And, Or, Nor, SortBy
from mongospecs.helpers.query import Q

# ... (your class definitions) ...

# Example using operators
all_condition = All(Q("my_list"), [1, 2, 3])
elem_match_condition = ElemMatch(Q("my_array"), {"field1": "value1"}, {"field2": {"$gt": 5}})
exists_condition = Exists(Q("my_field"), True)
in_condition = In(Q("my_field"), ["value1", "value2"])
not_condition = Not(In(Q("my_field"), ["value1"]))
not_in_condition = NotIn(Q("my_field"), ["value3", "value4"])
size_condition = Size(Q("my_list"), 3)
type_condition = Type(Q("my_field"), 1)  # 1 represents the BSON double type

# Example using groups
and_group = And(exists_condition, in_condition)
or_group = Or(size_condition, type_condition)
nor_group = Nor(all_condition, elem_match_condition)

# Example using SortBy
sort_instructions = SortBy(Q("field1"), Q("field2.desc"))

# You would then use these conditions, groups, and sort instructions in your MongoDB queries.
