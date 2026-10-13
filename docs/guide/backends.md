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
| Stored documents on read | validated | validated | validated |
| `Model.update(...)`, `PATCH`, filters | validated and cast | validated and cast | validated and cast |
| Your own validators | none in msgspec; use `Annotated[..., msgspec.Meta(...)]` constraints | `field_validator`, `model_validator` | `attrs.field(validator=..., converter=...)` |
| An empty list as a default | `tags: list[str] = []` | `tags: list[str] = []` | `tags: list[str] = attrs.field(factory=list)` |
| Adopting an existing class | `SpecAdapter` from each backend module | same | same |

Speed and memory are measured in [Benchmarks](../benchmarks.md).

## What to know about each

### msgspec and attrs: the constructor does not check types

That is how both libraries work, and mongospecs does not add a check on `insert()` or `save()`:

```py
doc = Counter(n="many")      # no error: msgspec and attrs do not validate here
await doc.insert()           # stored as a string
await Counter.get(doc.id)    # ValidationError: reads are validated
```

Values that come from outside (a request body, a file, another service) should go through
something that validates: `Model.update(id, n=value)`, `Model.insert_many([{"n": value}])` (a
mapping is validated; an instance is taken as it is), a `mongospecs-web` resource, or
`msgspec.convert(data, Counter)`. Values your own typed code builds are covered by your type
checker.

### pydantic: validators and single-field updates

- Models are validated when constructed. `Spec` sets `validate_assignment=False`, as pydantic does
  by default, so a value assigned to an attribute afterwards is not validated, and is saved as it is.
- `Model.update(id, low=5)` and an HTTP `PATCH` validate one field at a time. The field's type,
  constraints and `field_validator`s run. A `model_validator` that compares fields does not: it
  would be comparing the new value with the other fields' defaults, not with what is stored. If a
  rule spans fields, enforce it where you have the whole document: load it, change it, `save()`.
- `id` is declared with the alias `_id`; both names are accepted when constructing.
- The `pydantic.mypy` plugin works as usual.

### attrs: decorate every model

A subclass of `Spec` that is not decorated with `@attrs.define(kw_only=True)` gets no fields of
its own. `kw_only=True` is needed because `id` has a default and your required fields come after
it.

## What is the same everywhere

- The `id` attribute, stored as `_id`; `__spec__ = SpecConfig(...)` assigned without an annotation.
- Field markers in `typing.Annotated`: `CreatedAt`, `Revision`, `Immutable`, `Policy(...)` and the
  rest (see [Models](models.md)).
- Embedded models are plain classes of the same library, without the `Spec` base.
- Two documents are equal when their backend says so (field by field), not when their ids match.
- Models from different backends can be bound to the same registry.
