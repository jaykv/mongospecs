# Changelog

Two packages are released from this repository; each entry says which one it is about. Versions
follow semantic versioning, and while a package is below 1.0 a minor release may break
compatibility. Breaking changes are marked **Breaking**.

## Unreleased

### mongospecs 0.2.0

A rewrite. See [Migrating from 0.1.x](docs/migration.md) for the API map.

- **Breaking:** async only, Python 3.11+, MongoDB 7.0+. The synchronous 0.1.x line stays on the
  `0.1.x` branch and as `mongospecs<0.2`.
- **Breaking:** models are bound to an explicit `Registry`; there is no class-level client.
- **Breaking:** msgspec is no longer installed implicitly; choose an extra (`[msgspec]`,
  `[pydantic]`, `[attrs]`).
- Change tracking by snapshot: `save()` sends only what changed.
- Typed, immutable queries with `Q`; `tracked`, `lean()` and `raw()` read modes; cursor and offset
  pagination.
- Atomic update operators, real upsert, `bulk_write`, `bulk_save`, partial-failure `insert_many`.
- Relations: `Ref`, `Backref`, `include()`, cascade rules, `$graphLookup`, `$unionWith`.
- Document hooks and plugins; timestamps, soft delete and revisions by field marker.
- Transactions as a retried callback that restores in-memory documents when it aborts.
- Index planning with gated destructive actions; change streams; per-command observability events
  with logging, OpenTelemetry and Sentry listeners.
- Document hooks run for `insert_many` and `bulk_save`.
- `insert`, `save`, `insert_many` and `bulk_save` validate the document before writing it, on
  every backend. `validate=False` on a call or `SpecConfig(validate_writes=False)` on a model
  turns the check off.
- A single-field update on a pydantic model runs the field's validators and not the model-level
  ones, which could only see the other fields' defaults.

### mongospecs-web 0.1.0

First release.

- Resources described as data and mounted on plain ASGI, Starlette, FastAPI, Litestar or Quart.
- Field `Policy` drives the create, patch, put, read, filter and sort surface; guards and row
  scope for authorization; RFC 9457 errors; ETag and `If-Match`; OpenAPI 3.1.
- `PATCH` merges into embedded objects (RFC 7396); `Resource(merge_patch=False)` makes it replace
  them. `PUT` is available as the `Put` operation.

Changes made after the first review builds, for anyone who tried those:

- **Breaking:** `?total=true` is opt-in per resource (`page=Cursor(total=True)`).
- **Breaking:** `PATCH` on an embedded object is partial. Send `null` for keys to clear, or set
  `merge_patch=False`.
- **Breaking:** `If-Match` on a model without a `Revision` field is answered with 412.
- **Breaking:** a filter or sort key given twice is a 400, as are `NaN` and `Infinity` in a body.
- **Breaking:** a guard or hook registered for an operation the resource does not have raises
  `ValueError`.
