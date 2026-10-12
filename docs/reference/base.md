# `mongospecs.base` Module

This module defines the base classes for creating MongoDB specifications: `SpecBase` and `SubSpecBase`.

## `SpecBase` Class

The `SpecBase` class serves as the foundation for creating MongoDB specifications. It combines several mixins to provide a comprehensive set of functionalities:

* **`CrudMixin`:**  Provides methods for basic CRUD (Create, Read, Update, Delete) operations.
* **`IndexManagementMixin`:**  Provides methods for managing indexes.
* **`IntegrityMixin`:**  Provides methods for enforcing data integrity.
* **`SessionTransactionMixin`:**  Provides methods for managing database sessions and transactions.
* **`SignalMixin`:** Provides methods for handling signals.

**Example:**

```python
from mongospecs.base import SpecBase

class MySpec(SpecBase):
    # ... your specification definition ...
    pass

```

## `SubSpecBase` Class

The `SubSpecBase` class extends the functionality of `SpecBase` to handle sub-specifications and projections within MongoDB documents.  It includes methods for applying projections and handling references to other documents.

**Methods:**

* `_apply_projection(documents, projection)`: Applies a given projection to a list of documents.
* `_projection_to_paths(root_key, projection)`: Expands a sub-projection into a map of full paths.

(Further details on these methods and examples will be added later.)
