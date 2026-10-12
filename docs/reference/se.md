# `mongospecs.helpers.se` Module

This module provides custom JSON encoders and decoders designed to handle MongoDB-specific types, such as `ObjectId` and `date` objects, when working with `msgspec`.

## Classes

*   **`MongoEncoder`:** A subclass of `msgspec.json.Encoder` with a custom `enc_hook` (`mongo_enc_hook`) to handle special types:
    *   `msgspec.UNSET` or `attrs.NOTHING`: Encoded as `None`.
    *   `date`: Encoded as a string using `str(obj)`.
    *   `ObjectId`: Encoded as a string using `str(obj)`.
    *   Other unsupported types raise a `NotImplementedError`.

*   **`MongoDecoder`:** A subclass of `msgspec.json.Decoder` with a custom `dec_hook` (`mongo_dec_hook`) to handle special types:
    *   `ObjectId`: Decoded using `ObjectId(obj)`.
    *   Other unsupported types raise a `NotImplementedError`.

## Functions

*   **`mongo_enc_hook(obj)`:** The encoding hook used by `MongoEncoder`.
*   **`mongo_dec_hook(typ, obj)`:** The decoding hook used by `MongoDecoder`.

**Example Usage:**

```python
# Example usage (replace with your actual classes)
from mongospecs.helpers.se import MongoEncoder, MongoDecoder
from bson import ObjectId
from datetime import date

# ... (your class definitions) ...

# Example encoding
my_object = {
    "date_field": date(2024, 1, 1),
    "object_id_field": ObjectId("64a987654321000000000000")
}

encoded_data = MongoEncoder.encode(my_object)
print(encoded_data)

# Example decoding
decoded_object = MongoDecoder.decode(encoded_data)
print(decoded_object)
```

**Purpose:**

These encoders and decoders are essential for seamlessly converting between Python objects and JSON representations when working with MongoDB data. They ensure that MongoDB-specific types are handled correctly during serialization and deserialization.
