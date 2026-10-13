# Choosing a backend

A model is a msgspec `Struct`, a pydantic `BaseModel` or an attrs class. Queries, writes,
relations, plugins and the web layer are the same on all three; what differs is what the model
library itself does. Pick the one your application already uses. With no preference, msgspec is
the lightest and the one the examples use.

```
pip install "mongospecs[msgspec]"    # or [pydantic], or [attrs] (which also installs msgspec)
```

| | msgspec | pydantic | attrs |
|---|---|---|---|
| Import | `from mongospecs.msgspec import Spec` | `from mongospecs.pydantic import Spec` | `from mongospecs.attrs import Spec` |
| Declaring a model | `class A(Spec):` | `class A(Spec):` | `@attrs.define(kw_only=True)` on **every** model, then `class A(Spec):` |
| Wrong type passed to the constructor | accepted | `ValidationError` | accepted |
| Wrong type assigned to an attribute | accepted | accepted | accepted |
| `insert()`, `save()`, `insert_many()`, `bulk_save()` | validated | validated | validated |
| Stored documents on read | validated | validated | validated |
| `Model.update(...)`, `PATCH`, filters | validated and cast | validated and cast | validated and cast |
| Your own validators | none in msgspec; use `Annotated[..., msgspec.Meta(...)]` constraints | `field_validator`, `model_validator` | `attrs.field(validator=..., converter=...)` |
| An empty list as a default | `tags: list[str] = []` | `tags: list[str] = []` | `tags: list[str] = attrs.field(factory=list)` |
| Adopting an existing class | `SpecAdapter` from each backend module | same | same |

Speed and memory are measured in [Benchmarks](../benchmarks.md).

## What to know about each

### msgspec and attrs: the constructor does not check types

That is how both libraries work. mongospecs checks the document when it is written instead, so a
wrong value never reaches the database:

```py
doc = Counter(n="many")      # no error: msgspec and attrs do not validate here
await doc.insert()           # ValidationError: Counter.n: Expected `int`, got `str`
```

The error comes at the write, not at the line that made the mistake. To be told earlier, build
documents from outside data with something that validates on the spot, such as
`msgspec.convert(data, Counter)` or a `mongospecs-web` resource.

### pydantic: validators and single-field updates

- Models are validated when constructed. `Spec` sets `validate_assignment=False`, as pydantic does
  by default, so a value assigned to an attribute afterwards is not checked by pydantic; it is
  checked when the document is written, like on the other backends.
- `Model.update(id, low=5)` and an HTTP `PATCH` validate one field at a time. The field's type,
  constraints and `field_validator`s run. A `model_validator` that compares fields does not: it
  would be comparing the new value with the other fields' defaults, not with what is stored. If a
  rule spans fields, enforce it where you have the whole document: load it, change it, `save()`, which validates the whole document, model validators included.
- `id` is declared with the alias `_id`; both names are accepted when constructing.
- The `pydantic.mypy` plugin works as usual.

### attrs: decorate every model

A subclass of `Spec` that is not decorated with `@attrs.define(kw_only=True)` gets no fields of
its own. `kw_only=True` is needed because `id` has a default and your required fields come after
it.

## Writes are validated, unless you say otherwise

Before `insert()`, `save()`, `insert_many()` and `bulk_save()` send anything, the document is
checked against its model, in the form it is about to be stored in. What is refused is exactly
what could not be read back. `save()` only checks when something changed, and for a document
loaded with a projection it checks the fields that changed rather than the whole document.

The check costs a few microseconds per document (about 3 on msgspec and 5 on pydantic for a
document with seven fields and two embedded objects, measured on one machine; a round trip to
MongoDB is hundreds). Where even that matters, and the values are known to be sound, turn it off:

```py
await Event.insert_many(events, validate=False)            # for one call
await event.save(validate=False)

class Event(Spec):
    __spec__ = SpecConfig(validate_writes=False)           # for every write of a model
```

With the check off, a wrongly typed value is stored as it is and the document fails to load
afterwards. `Model.update(...)` and the other filter-based writes always validate what they are
given; the switch does not apply to them.

## What is the same everywhere

- The `id` attribute, stored as `_id`; `__spec__ = SpecConfig(...)` assigned without an annotation.
- Field markers in `typing.Annotated`: `CreatedAt`, `Revision`, `Immutable`, `Policy(...)` and the
  rest (see [Models](models.md)).
- Embedded models are plain classes of the same library, without the `Spec` base.
- Two documents are equal when their backend says so (field by field), not when their ids match.
- Models from different backends can be bound to the same registry.
