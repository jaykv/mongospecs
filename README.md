# mongospecs

Built on top of the https://github.com/GetmeUK/MongoFrames ODM, with:
- Pydantic (`BaseModel`), attrs (`@define`), and msgspec (`Struct`) support for defining schema models (specs)
- Type-hints
- *Spec adapters*
  - Build a spec out of existing BaseModels, attrs classes, or Structs
- *Raw BSON encode/decode for specs/mongo documents*
## Install
Choose from one of the following:
* `pip install mongospecs` for msgspec only
* `pip install mongospecs[pydantic]` for msgspec and pydantic only
* `pip install mongospecs[attrs]` for msgspec and attrs only
* `pip install mongospecs[pydantic,attrs]` for msgspec, pydantic, and attrs

### Example
```python
# Import Spec, either...
## 1. with pydantic:
from mongospecs.pydantic import Spec

## 2. with msgspec:
from mongospecs.msgspec import Spec

## 3. with attrs:
from mongospecs.attrs import Spec

# Define schema model
class Dragon(Spec):
    _collection = "dragons" # Optional. If not defined, uses the class name by default.

    name: str
    breed: Optional[str] = None

# create
burt = Dragon(name="Burt", breed="Cold-drake")
print(burt.name)  # Burt
print(burt.breed) # Cold-drake

# insert
burt.insert()
print(burt.id)  # inserted document ObjectId

# fetch
doc = Dragon.find_one({"name": "Burt"})  # returns raw mongo document

# delete
burt.delete()
```
