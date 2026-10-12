# `mongospecs.helpers.pagination` Module

This module provides classes for paginating query results, specifically designed to work with `Spec` classes in `mongospecs`.

## Exceptions

*   **`InvalidPage`:** Raised when an invalid page number is requested.

## Classes

*   **`Page(offset, number, items, next, prev)`:** Represents a single page of results.
    *   `offset`: The offset of the first result in the page.
    *   `number`: The page number.
    *   `items`: A list of results (specs) for this page.
    *   `next`: The next page number (or `None` if it's the last page).
    *   `prev`: The previous page number (or `None` if it's the first page).
    *   `__getitem__(i)`: Allows accessing items in the page using indexing (e.g., `page[0]`).
    *   `__iter__()`: Allows iterating over the items in the page.
    *   `__len__()`: Returns the number of items in the page.
    *   `get_offset(item)`: Returns the offset for a specific item in the page.

*   **`Paginator(spec_cls, filter=None, per_page=20, orphans=0, filter_kwargs=None)`:** Handles the pagination logic.
    *   `spec_cls`: The `Spec` class being paginated.
    *   `filter`: The filter to apply when selecting results. Can be a dictionary, `Condition`, or `Group`.
    *   `per_page`: The number of results per page (default: 20).
    *   `orphans`: The number of additional results allowed on the last page to avoid very short pages (default: 0).
    *   `filter_kwargs`: Additional filter arguments (e.g., `sort`, `projection`).
    *   `__post_init__()`: Initializes the paginator, calculates the total number of items and pages, and flattens the filter.
    *   `__getitem__(page_number)`: Returns a `Page` object for the given page number. Raises `InvalidPage` if the page number is out of range.
    *   `__iter__()`: Allows iterating over all pages.
    *   `item_count`: (Property) Returns the total number of items being paginated.
    *   `page_count`: (Property) Returns the total number of pages.
    *   `page_numbers`: (Property) Returns a range of valid page numbers.

**Example Usage:**

```python
# Example usage (replace with your actual classes)
from mongospecs.helpers.pagination import Paginator
from mongospecs.helpers.query import Q

# Assuming you have a Spec class called 'MySpec'
# ... (your class definitions) ...

# Create a Paginator instance
paginator = Paginator(MySpec, filter=Q("field1") == "value1", per_page=10)

# Get the first page
page1 = paginator[1]

# Iterate over all pages
for page in paginator:
    for item in page:
        # Process each item
        print(item)

# Access properties
print(f"Total items: {paginator.item_count}")
print(f"Total pages: {paginator.page_count}")
