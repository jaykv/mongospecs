# mongospecs v2 — Overhaul Plan

Status: **revision 3, for review** · Date: 2026-10-12
Authors: Claude (Sonnet 5.5, draft 1; Opus 5.5, revisions 2–3) with Jay
Scope: full rewrite, breaking changes allowed everywhere. Async-only. Python 3.11–3.15.

> Revision 3 changes: the target is now stated as **Mongoose + mongospecs + RESTo** (§1). Mongoose
> is added as the reference ODM (§3.3) with a parity table, a do-not-inherit list and a tiebreak
> rule (§7.1 rule 9). Consequences: snapshot-based change tracking replaces "no dirty tracking";
> hooks gain the aggregate and bulk kinds; discriminators, reverse relations, query casting,
> deferred fields, named scopes, bulk save, registry-level plugins, multi-database use, shard-key
> aware writes and command observability are added; Mongoose joins the benchmark comparators.

> Revision 2 changes: `Store` layer removed; plugins and field policy moved to `Annotated` markers;
> transactions redesigned around the callback API; derived view classes replaced by policy-as-data;
> web layer split into its own package in a uv workspace; entry-point discovery dropped; Rust
> removed from the build plan and turned into a decision fed by a real-MongoDB benchmark track;
> phases replaced by a dependency-ordered work graph (§7) written for agent execution.
>
> Claims are tagged **[probed]** when a script was run for this revision, **[read]** when verified by
> reading the repo, and **[unverified]** otherwise. §10 lists everything unverified.

---

## 1. Goals

**Vision: Mongoose + mongospecs + RESTo.**

| From | What it contributes |
|---|---|
| Mongoose | The production feature set and ergonomics of a mature ODM: change tracking, four kinds of middleware, populate, discriminators, query casting, lean reads, bulk operations, index sync, transactions that restore document state |
| mongospecs | Typed models on the backend you already use (msgspec, pydantic, attrs), `Q` expressions, small readable core |
| RESTo | Declarative resources: field policy drives the HTTP surface, mounted on any framework |

Mongoose is also the **fallback**: where this document does not decide a behaviour, v2 does what
Mongoose does, unless the behaviour is on the do-not-inherit list in §3.3. Mongoose is likewise the
reference point for "efficient and production ready" in the benchmark track (§6).

Concretely, a performant, readable, pluggable async ODM for MongoDB plus a framework-neutral web layer:

1. Multi-backend models (msgspec, pydantic, attrs) behind one `Codec` seam.
2. Plugins that carry behaviour (timestamps, soft delete, revisions) across all three backends.
3. Mongo-native relationship loading (`$lookup`, `$graphLookup`, `$unionWith`) with a measured
   fallback to batched `$in`.
4. A **Resource** layer (RESTo's controller idea, no globals) with plugs for a pure ASGI app,
   Starlette, FastAPI, Litestar and Quart. Shipped as `mongospecs-web`, a separate distribution
   in this repo. This is a first-class deliverable, not an add-on.
5. A stress and benchmark suite that runs against real MongoDB and produces the evidence for
   every performance decision, including whether Rust is ever warranted.

Non-goals: sync API, SQL or other databases, Motor, Python < 3.11, entry-point plugin discovery,
any Rust code inside this plan (see §6).

## 2. Where we are

### 2.1 What `mixins-test` is [read]

A structural refactor of `main`: `base.py` split into
`mongospecs/mixins/{base,crud,query,index,integrity,session,signal}.py`, composed as
`SpecBase(CrudMixin, IndexManagementMixin, IntegrityMixin, SessionTransactionMixin, SignalMixin)`;
helpers moved to `mongospecs/helpers/`. Six commits ahead of `main`. Uncommitted on top:
`mongospecs/exceptions.py` (new), `retry`/error decorators in `utils.py`, pagination/docs edits.

It is a method-grouping refactor over one sync base class with class-level state, not a plugin
system: mixins hold no state, add no fields, cannot be opted into per model. Source is 2,035
lines; the test suite is about 290 tests in 6,955 lines, all on `mongomock`.

### 2.2 Defects in current code [read]

| Area | Issue |
|---|---|
| Global state | `_client`, `_db`, `_collection_context` are class-level mutables; `with_options()` swaps a class attribute, unsafe under asyncio and free-threading |
| Transactions | `transaction()` yields a session but no CRUD call uses it unless `session=` is hand-passed; only `ConnectionFailure`/`OperationFailure` trigger an explicit abort |
| Transaction tests | `tests/test_spec_transactions.py` runs against `MagicMock`; there is no real transaction coverage |
| `exceptions.py` | `class DuplicateKeyError(MongoSpecError, DuplicateKeyError)` shadows the pymongo import, so pymongo's error falls through to the `OperationFailure` branch, which constructs `OperationError` with kwargs its `__init__` does not accept; wrapper lacks `functools.wraps` |
| `utils.py` | Redefines `ConnectionError`/`OperationError`/`DatabaseError` (shadows a builtin, duplicates `exceptions.py`); `retry()` retries `OperationFailure` including duplicate-key on non-idempotent inserts and sleeps with blocking `time.sleep` |
| Dead code | None of `retry`, `handle_database_errors`, `handle_pymongo_errors` is applied anywhere, so the bugs above are latent |
| API naming | `find`/`find_one` return raw dicts (`{}` on miss); `one`/`many` return specs |
| Soft delete | Hardcodes an undeclared `deleted` field; `find_active` mutates the caller's filter dict |
| Consistency | pydantic `from_document` uses `model_construct` (no validation) but `many` uses `cls(**d)`; pydantic `unset()` takes no kwargs so cannot receive a session |
| Duplication | `Spec`/`AdapterBuilder`/`MsgspecAdapter` repeat bodies per backend; `_id` vs `id` hacks |
| Layering | `helpers/pagination.py` and `helpers/bson.py` import `base.py` |
| Updates | `upsert` is read-then-write; `update` `$set`s the whole doc; no optimistic locking |
| Counting | `count()` with no filter uses `estimated_document_count`, which ignores any scope |
| Collection lookup | `getattr(db, name)` breaks for names starting with `_` or colliding with Database attrs |
| Tooling | Python `^3.9`, poetry, pytest 7, ruff 0.3, `mongomock`, CI 3.9–3.12 |

The existing suite has not been run (no `.venv`). Node `F3` establishes that baseline.

## 3. Reference projects (cloned and read for draft 1)

### 3.1 Mongoz (dymmond/mongoz, ~7.9k LOC, pydantic-only, py3.10–3.14, pymongo>=4.13)

Take as ideas, do not mirror structure or API.

Worth adopting:
- Index reconciliation planner (`indexes.py`): typed actions `CORRECT/CREATE/RECREATE/RETAIN/DELETE/CONFLICT`;
  normalises materialised text indexes (`_fts/_ftsx`) to avoid false diffs; destructive ops gated.
- Explicit lifecycle: no I/O at import, `async with Registry`, closed registry is final, documented
  event-loop binding.
- Immutable clone-on-write queries; `update` (atomic `$set` patch) vs `save` split;
  `DocumentNotFound` on acknowledged writes that matched nothing.
- No ambient session: explicit `session=`. A ContextVar session would be copied into
  `gather`/`TaskGroup` children, letting concurrent tasks share one `ClientSession`.
- Trusted-vs-untrusted boundary for raw queries; string helpers are literal-matching by default.
- Release gates: wheel validation, replica-set transaction proof, consumer typing fixtures.

Verified weaknesses to beat:

| Finding | Evidence |
|---|---|
| `only()`/`defer()` projections are client-side only; full docs are fetched | no `$project`/`projection=` sent anywhere; `from_row` filters the dict |
| Every read is an `aggregate`, even plain finds | `manager._cursor()` always calls `aggregate(self._pipeline())` |
| `$lookup` stages are emitted before `$match`/`$sort`/`$limit` | `_pipeline()` order |
| Joined results keyed by collection name, not field name | `item[related_meta.collection.name] = values` |
| Bulk `update` = find ids then `update_many` on `_id $in` (2 round trips, racy) | `persistence.patch_many` |
| Full pydantic validation on every read, no opt-out | `from_row` ends `cls(**item)` |
| No `$graphLookup`/`$unionWith`/change streams | grep found none |
| No web/CRUD-controller layer | only lilya/ravyn integration tests |
| Global `settings` singleton | `mongoz.settings` |

Caveat: ~60% of the source read; `metaclasses.py` and `fields/core.py` not studied; its tests not run.

### 3.2 RESTo (jaykv/RESTo, 1.3k LOC, last commit 2022-08-30)

Concepts to keep:
1. `ActionsConnector`: model-side contract (fetcher/inserter/updater/deleter).
2. `App` ABC per framework: `build_app / load_controller / load_controllers`.
3. `Route` subclasses bound to generators; `execute=(fn, params)` escape hatch; `pipes=[...]`.
4. "Farms": per-field flags (`Insertable/Updatable/Filterable/private/alias/ref/sub`) drive the
   Create/Update/Filter surface. The most valuable idea.
5. `ActionRouter(model, Fetcher=, Inserter=, …)` auto-CRUD; `{data, messages}` envelope.

Defects not to inherit: global singletons; `build_filter_query` is a `# TODO` pass-through;
`deleter` uses the request JSON body as the filter; `inner_patch` ignores the `<_id>` path param;
`Route.validate` iterates `self.pipes` when `None`; stray `print`.

### 3.3 Mongoose (Automattic/mongoose, Node.js) — the reference ODM

Source for this section: Mongoose documentation. Items tagged **[docs]** were re-read for this
revision; the rest is from memory of the 8.x/9.x docs and must be confirmed by spike `S7` before
the owning node starts. Mongoose's source and test suite have not been read.

**Parity table.** "v2 form" is how the feature is expressed here; "Node" is where it is built (§7.2).

| Mongoose feature | What it does | v2 form | Node |
|---|---|---|---|
| Change tracking, delta `save()` | `save()` sends only modified paths; `isModified`, `modifiedPaths`, `isNew` | Snapshot taken at load; `save()` diffs and sends minimal `$set`/`$unset`; `doc.changes()`, `doc.is_new` (§4.5) | K12 |
| `lean()` | Skip hydration and tracking, return plain objects | `.lean()` → backend objects with no snapshot; `.raw()` → dicts | K12 |
| `Model.hydrate()` | Build a tracked document from a raw object | `Model.hydrate(doc)` | K12 |
| Middleware, four kinds **[docs]** | document, query, aggregate, model | Document hooks, command/filter transformers, pipeline transformers, bulk hooks (§4.3) | C4, K7 |
| `schema.plugin()` and global `mongoose.plugin()` | Per-schema and all-schema plugins | Marker-activated plugins plus `Registry(plugins=[...])` applied to every bound model | K7 |
| `timestamps` | `createdAt`/`updatedAt`, also on updates and bulk writes | `CreatedAt`/`UpdatedAt` markers, applied by transformers on every write path incl. bulk | K8 |
| `optimisticConcurrency`, `versionKey` | Version check on save | `Revision` marker | K8 |
| `populate()` | Batched follow-up queries (not `$lookup`); `match`, `select`, `limit`, `perDocumentLimit`, nested paths | `include()` with the same options, both strategies (§4.6) | R1 |
| Virtual populate | Reverse relations via `localField`/`foreignField`, `justOne`, `count` | `Backref[...]` marker fields, never stored | R7 |
| `refPath` | Reference whose target collection is named by another field | `Ref[A \| B]` with a discriminating field | R7 |
| Discriminators **[docs: `discriminatorKey` exists]** | Several models in one collection, keyed by a discriminator field | Tagged unions per backend: msgspec `tag=`, pydantic discriminated unions, attrs via the Codec | K13 |
| Query casting | Filter and update values are cast to schema types | `Q` values coerced through `Codec.validate_field` for the addressed field | K14 |
| `strictQuery: 'throw'` **[docs]** | Unknown filter paths raise | The only mode: a typed query naming an unknown field raises at build time | K14 |
| `sanitizeFilter` / `trusted()` | Block operator injection in filters built from user input | Raw mappings are untrusted by default; `trusted(...)` wraps deliberate raw filters | C2 |
| `select: false` **[docs]** | Field not fetched unless asked for | `Deferred` marker; excluded from the default projection; `.select("+field")` | K5 |
| `immutable` | Field cannot change after insert | `Immutable` marker, enforced in the ODM (not only in `Policy`) | K5 |
| `alias` | Short stored name, long attribute name | Codec rename; queries translate attribute names to stored names | C1 |
| Getters/setters, custom validators | Per-path transforms and checks | The backend's own converters and validators; nothing new | — |
| Virtuals | Computed, non-stored properties | Python properties; inclusion in the Read view is a `Computed` marker | W2 |
| Statics, methods | Model-level and instance-level functions | Plain classmethods and methods | — |
| Query helpers | Chainable named filters (`.byName()`) | Named scopes registered on the model's query type | K16 |
| `orFail()` | Reject when nothing matched | `.or_fail()` raising `DocumentNotFound` | K5 |
| `cursor()` / `eachAsync({parallel})` | Streaming with bounded concurrency | `async for` plus `.each(fn, concurrency=n)` | K5 |
| `bulkWrite`, `bulkSave`, `insertMany({ordered:false})` | Typed bulk ops with casting and timestamps; batch of delta saves; partial-failure reporting | `Model.bulk_write([...])`, `Model.bulk_save(docs)`, `insert_many(ordered=False)` returning per-item errors | K15 |
| `Connection.transaction(fn)` **[docs]** | Wraps `withTransaction`, retries, and **resets document state** when the transaction aborts | `registry.transaction(fn)`; tracked documents touched inside are restored on abort and before each retry | K6, K12 |
| `transactionAsyncLocalStorage` **[docs]** | Opt-in ambient session inside `transaction()` | Not adopted; sessions stay explicit (§3.1 reasoning). Recorded as a deliberate divergence | — |
| `syncIndexes`, `diffIndexes`, `autoIndex: false` | Reconcile declared and actual indexes; no auto-build in production | Index planner; nothing is built implicitly | K9 |
| `shardKey` schema option | Shard key added to the filter of document writes | `SpecConfig(shard_key=...)`; document writes target a shard | K18 |
| `createConnection`, `useDb` | Several connections; same pool, different database | Several registries; `Model.using(db="tenant_a")` | K18 |
| `collation`, `read`, `writeConcern`, `maxTimeMS`, `hint`, `comment`, `explain` | Per-schema and per-query driver options | `SpecConfig` defaults plus pass-through on every query; registry-wide default `max_time_ms` | K5 |
| `Model.aggregate()`, `Model.collection` | Pipeline builder; raw driver collection | `Model.aggregate(pipeline, as_=T)` (scopes applied by pipeline transformers); `Model.collection` | K5 |
| `mongoose.set('debug')` | Log every command | `Registry(on_command=...)` built on pymongo command monitoring; logging and OpenTelemetry adapters | K17 |
| `Model.watch()` | Change streams | `Model.watch()` | R5 |
| `Model.exists`, `countDocuments`, `estimatedDocumentCount` | Explicit cheap checks and counts | `exists`, `count`, `estimated_count`; no silent switch between the last two | K5 |
| Timeseries, capped, TTL (`expires`) collection options | Collection-level options | `SpecConfig` options consumed by the planner | K9 |
| Maps, subdocuments, arrays of subdocuments | Nested structures with their own tracking | Nested models and `dict[str, T]` of the same backend; tracked by the parent's snapshot | K12 |

**Do not inherit.**

| Mongoose behaviour | Why not | v2 instead |
|---|---|---|
| Global default connection and global model registry by name | Hidden state, test isolation problems | Explicit `Registry`; string forward refs (`Ref["User"]`) resolved per registry |
| `bufferCommands` (queue operations until connected) | Hides connection failures until a timeout | Operations fail immediately when the registry is not open |
| `strictQuery` stripping unknown filter paths **[docs]** | `find({typo: 1})` becomes `find({})` | Unknown fields raise |
| `strict` silently stripping unknown update paths **[docs]** | Typos vanish | Unknown fields raise |
| Update validators off unless `runValidators`, with a different `this` **[docs]** | Unvalidated writes by default | Atomic updates always validate each supplied field |
| `findOneAndUpdate` returning the pre-update document by default | Common surprise | Returns the updated document; `.returning("before")` to opt out |
| `Model.aggregate()` results uncast and unscoped | Bypasses soft delete and typing unless a plugin hooks it | Scopes apply through pipeline transformers; `as_=` decodes |
| Auto-building indexes at startup | Unsafe on large collections | Planner, explicit apply |
| Implicit pluralised collection names | Surprising | Collection name is explicit or the class name, as in v1 |
| Overloaded signatures and option bags | Hard to type | Keyword-only options, one meaning per method |

## 4. Architecture

```
Spec      what a document is            pydantic | msgspec | attrs class + Annotated markers
Codec     how it crosses the wire       one implementation per backend
Model ops what you can do to it         ODM directly on pymongo async (no Store indirection)
Resource  what you expose of it         operations + policy + scope + guards (pure data)   ┐ mongospecs-web
Plug      where it is mounted           asgi | starlette | fastapi | litestar | quart      ┘
```

`Store` from draft 1 is gone. A five-method protocol could not carry sessions, atomic operators,
filter-based bulk writes or upserts, and a faithful `MemoryStore` would have been a second
mongomock. The ODM talks to pymongo; the web layer defines the one narrow protocol it needs (§4.9).

### 4.1 Repository layout (uv workspace)

```
pyproject.toml                  # virtual workspace root: members, shared ruff/mypy/pytest config
uv.lock
packages/
  mongospecs/                   # distribution: mongospecs        import: mongospecs
    pyproject.toml              #   deps: pymongo; extras: [msgspec] [pydantic] [attrs]
    src/mongospecs/
      registry.py  errors.py  codec.py  markers.py  query.py  model.py
      relations.py  indexes.py  tx.py  hooks.py  pagination.py
      plugins/   timestamps.py  soft_delete.py  revision.py
      backends/  msgspec.py  pydantic.py  attrs.py        # implement Codec only
    tests/
  mongospecs-web/               # distribution: mongospecs-web    import: mongospecs_web
    pyproject.toml              #   deps: mongospecs; extras: [starlette] [fastapi] [litestar] [quart]
    src/mongospecs_web/
      resource.py  operations.py  policy.py  filters.py  errors.py  openapi.py  backend.py
      plugs/     asgi.py  starlette.py  fastapi.py  litestar.py  quart.py
      testing.py                # FakeBackend + the plug contract suite, importable by third parties
    tests/
bench/                          # workspace member, never published (see §6)
docker/                         # compose files: replica set, sharded cluster
docs/
```

- Two distributions, one lockfile, one CI. `mongospecs-web` depends on `mongospecs` via
  `[tool.uv.sources] mongospecs = { workspace = true }`.
- Import name is `mongospecs_web`, not a `mongospecs.web` namespace package: namespace packages
  across distributions would forbid `mongospecs/__init__.py`.
- Versions move independently; `mongospecs-web` pins a compatible `mongospecs` range.
- Third-party plugs and plugins are ordinary imports. No entry points.

### 4.2 Spec, markers and field policy

Field policy and plugin bindings are `Annotated` metadata. One mechanism works unchanged on all
three backends and on Python 3.13 and 3.15 **[probed]**; a `field(insert=False)` helper would have
needed to return a different object per backend.

```python
class Article(Spec):                       # Spec = the backend's base (msgspec shown; same for others)
    __spec__ = SpecConfig(collection="articles", indexes=[Index("title", unique=True)])

    id: ObjectId | None = None
    title: Annotated[str, Policy(filter=("eq", "prefix"), sort=True)]
    author: Annotated[Ref[User], Policy(filter=("eq", "in"))]
    secret: Annotated[str, Policy(private=True)]
    created_at: Annotated[datetime | None, CreatedAt, Policy(insert=False, update=False)] = None
    updated_at: Annotated[datetime | None, UpdatedAt, Policy(insert=False, update=False)] = None
    deleted_at: Annotated[datetime | None, DeletedAt, Policy(insert=False, update=False)] = None
    rev: Annotated[int, Revision, Policy(insert=False, update=False)] = 0
```

- `Policy` is plain data: `insert`, `update`, `private`, `filter` (allowed operators), `sort`.
  The default for an unmarked field is readable, insertable, updatable, not filterable, not sortable.
- The Python attribute is `id`; storage is `_id`; the Codec owns the mapping. The id type comes
  from the annotation (`ObjectId`, `str`, `UUID`, `int`).
- Model config is a `ClassVar` (`__spec__`), not class keyword arguments, so no custom metaclass is
  needed on any backend **[probed]**.
- Core markers besides the plugin ones: `Immutable` (rejected in any update), `Deferred` (left out
  of the default projection, Mongoose's `select: false`), `Alias("t")` (stored name),
  `Computed` (a property included in the Read view), `Backref[...]` (reverse relation, never stored).
- `Ref["User"]` string forward references are resolved against the registry at bind time, which
  removes circular-import pressure without a global model registry.
- Discriminators: a base model declares `SpecConfig(discriminator="kind")`; subclasses share the
  collection and are selected on decode by tagged-union support in the backend (msgspec tagged
  unions decode correctly **[probed]**; pydantic and attrs go through the Codec). Queries on a
  subclass add the discriminator to the filter.

### 4.3 Plugins

Draft 1's `class Article(Spec, Timestamps, SoftDelete, Revision)` cannot work: two field-bearing
bases raise `TypeError: multiple bases have instance lay-out conflict` on msgspec and on slotted
attrs, and a plain mixin's fields are silently dropped by msgspec **[probed]**.

So plugins never add fields. The user declares the field and marks it (`CreatedAt`, `DeletedAt`,
`Revision`); the marker activates the plugin for that model. Benefits: fields are visible to type
checkers, field names are the user's choice, and there is one implementation per plugin.

A plugin contributes any of the following. The first four rows are Mongoose's four middleware
kinds (document, query, aggregate, model) with typed signatures.

| Contribution | Mongoose kind | Shape | Example |
|---|---|---|---|
| Document hook | document | `async (OpCtx, doc) -> None` | slugify before insert |
| Command transformer | query | `(OpCtx, update_doc) -> update_doc` | `UpdatedAt` adds `$currentDate`; `Revision` adds `$inc` and a `rev` filter |
| Filter transformer (scope) | query | `(OpCtx, filter) -> filter` | `DeletedAt` adds `deleted_at: None` |
| Pipeline transformer | aggregate | `(OpCtx, pipeline) -> pipeline` | soft-delete `$match` prepended to `Model.aggregate()` |
| Bulk hook | model | `async (OpCtx, ops) -> None` | audit log for `insert_many` / `bulk_write` |
| Index contribution | — | `list[Index]` | partial unique index excluding soft-deleted |

Plugins attach in two ways: by marker on a field (per model), or as `Registry(plugins=[...])`,
which applies to every model bound to that registry (tenant scoping, auditing).

Rules:
- `OpCtx` carries `session`, `registry`, `model`, `op`. Every hook and transformer receives it, so
  cascades and hooks participate in the caller's transaction.
- Document hooks fire only for document-level operations (`insert`, `save`, `delete` on an
  instance). Filter-based `update_many`/`delete_many` run command and filter transformers only;
  this is documented, not papered over by loading documents.
- Scopes apply to finds, counts, updates, deletes and inside `$lookup` sub-pipelines for the
  joined model. `.unscoped()` bypasses them explicitly.
- Hooks are async, sequential, fail-fast, in registration order, per class. Blinker is removed.
- A missing or mistyped marked field fails at class registration, not at first write.

### 4.4 Codec (backend seam)

```python
class Codec(Protocol):
    def fields(self, cls) -> Mapping[str, FieldInfo]        # name, type, markers, default, required
    def decode(self, cls, doc: Mapping | bytes, *, trusted: bool = False) -> Any
    def encode(self, obj) -> dict                           # BSON-ready
    def validate_field(self, cls, name: str, value) -> Any  # one field, with the model's own validators
    def json_schema(self, cls) -> dict
```

- Each backend implements this once; `SpecAdapter` over existing classes becomes one generic builder.
- `decode` accepts `bytes` so a raw-BSON path can be added later without changing the seam.
- **No derived view classes.** A `create_model`-style Patch class drops the source model's
  validators **[probed]** and is invisible to type checkers. Instead:
  - Create: filter input keys by `Policy.insert`, then `decode` with the real model.
  - Patch: filter by `Policy.update`, then `validate_field` per supplied key. "Absent" is a missing
    key, "null" is an explicit `None`; no backend needs an UNSET sentinel.
  - Read: `encode`, then drop `private` fields.
  - JSON Schema for each view is produced by transforming `json_schema(cls)` (drop or
    de-require properties). One algorithm, three backends.
- **Read validation:** reads validate by default. `trusted=True` (per query or `SpecConfig`)
  skips validation where the backend allows it. The cost is measured in §6, not assumed.

### 4.5 Model operations

- Query builder: immutable, clone-on-write, compiled from a small AST (`filter`, `sort`, `page`,
  `select`, `include`). v1's `Q` expression syntax (`Q.title == "x"`) is kept as the way to build
  filters; `And/Or/Nor/In/...` helpers are kept.
- **Query casting and strictness** (Mongoose's casting, without its stripping): values in a typed
  filter or update are coerced through `Codec.validate_field` for the addressed field (a string
  becomes an `ObjectId`, a date string a `datetime`). A field name the model does not have raises
  when the query is built. Raw mappings are untrusted unless wrapped in `trusted(...)`.
- **Named scopes** (Mongoose query helpers): `@Article.scope` registers a chainable filter,
  `Article.find().published().by(author)`.
- Reads: `find` when there are no joins, otherwise an aggregation pipeline. Projections are sent
  server-side. `Deferred` fields are excluded unless selected. `.or_fail()`, `.first()`, `.all()`,
  `async for`, `.each(fn, concurrency=n)`, `.explain()`.
- Read modes:

  | Mode | Returns | Validated | Tracked |
  |---|---|---|---|
  | default | model instances | yes | yes |
  | `.lean()` | model instances | per `trusted` setting | no |
  | `.raw()` | dicts | no | no |

- **Change tracking by snapshot.** When a document is loaded in the default mode, its encoded form
  is kept in a side table keyed by object identity and released by a weakref finaliser. This works
  on pydantic, slotted attrs and msgspec, provided the msgspec `Spec` base is declared with
  `weakref=True` (inherited by subclasses) **[probed]**. No setattr interception is needed, so the
  three backends share one implementation.
  - `doc.changes()` → the diff; `doc.is_new`; `doc.reload()`.
  - `await doc.save()` sends only the diff as `$set`/`$unset`. An unchanged document sends nothing.
  - A changed list or dict is sent whole (`$set` of that path). Per-element array operators are
    available through explicit atomic updates, not inferred from the diff.
  - Cost is one extra encoded copy per tracked document; `.lean()` exists to avoid it. The
    overhead is measured (B8), not assumed.
- Writes:
  - `await doc.insert()`, `await doc.save()` (delta, as above), `await doc.delete()`.
  - `await Model.update(id | filter, **fields)` and typed operators (`inc`, `push`, `pull`,
    `set_on_insert`, `unset`) → atomic, via `find_one_and_update`/`update_many`. Returns the
    updated document. Every supplied field is validated; `Immutable` fields are rejected.
  - Real `upsert` (single round trip).
  - `Model.bulk_write([...])` with typed operations, casting and timestamps;
    `Model.bulk_save(docs)` sends each document's delta in one round trip;
    `insert_many(ordered=False)` reports per-item failures instead of raising on the first.
  - `DocumentNotFound` on acknowledged writes that matched nothing.
  - With `SpecConfig(shard_key=...)`, document-level writes include the shard key in the filter.
- Escape hatches: `Model.aggregate(pipeline, as_=T)` (pipeline transformers still apply scopes)
  and `Model.collection` for the raw driver collection.
- Driver options (`max_time_ms`, `hint`, `comment`, `collation`, read/write concern, read
  preference) pass through on every query; defaults come from `SpecConfig`, then the registry.
  `Model.using(db=..., read_preference=..., write_concern=...)` returns a rebound clone.
- Unknown fields in the database are ignored on read and preserved on write.
- **Schema evolution:** optional `SpecConfig(upcast=fn)` where `fn(doc: dict) -> dict` runs before
  `decode`. Enough for lazy migration of renamed or missing fields; no migration framework.
- v1 verbs are replaced: `get(id)`, `find(...)` (async iterator / `.all()` / `.first()`), `count`.
  Nothing returns raw dicts unless `.raw()` is requested.

### 4.6 Relationships

`include()` compiles to one pipeline; results are keyed by field name.

1. Page-narrowing stages (`$match`, `$sort`, `$skip`, `$limit`) before joins.
2. Equality joins use `localField`/`foreignField`; `plan_indexes()` flags a missing foreign-key index.
3. Filtered or limited children use `let` + `pipeline` with `$expr`.
4. To-one relations: `$unwind` with `preserveNullAndEmptyArrays`.
5. Hierarchies: `$graphLookup` (`maxDepth`, `restrictSearchWithMatch`, `depthField`).
6. Polymorphic or multi-collection reads: `$unionWith`.
7. `include(..., strategy="auto"|"lookup"|"batch")`. Both strategies are implemented. **The default
   and the `auto` heuristic are chosen from benchmark data (§6), not presumed.** `$lookup` duplicates
   child payload per parent and each result document must fit 16MB. Mongoose's `populate()` is the
   batch strategy, which is a reason not to assume `$lookup` wins.
8. Cascade rules (`on_delete = cascade|nullify|pull`) are hooks and require a transaction.
9. Populate-level options on either strategy: `match`, `select`, `sort`, `limit`,
   `per_parent_limit`, and nested paths (`include("author.company")`).
10. Reverse relations (`comments: Backref[list[Comment], "article"]`) and counts
    (`Backref[int, ...]` with `count=True`) are loaded only when included and are never stored.
11. Polymorphic references (`Ref[Post | Video]`) resolve the target per document from a
    discriminating field (Mongoose `refPath`).

Pagination: cursor (keyset) pagination is the default and returns no total. Offset pagination is
available. A total is a concurrent `count_documents`, requested explicitly. `$facet` for
page-plus-total is **not** used unless spike `S2` shows it is safe: its output is believed to be a
single document subject to the 16MB limit **[unverified]**.

Also in scope: `Revision` optimistic locking (doubles as ETag), derived `$jsonSchema` validators
via `collMod` (opt-in), TTL/partial/unique/text/Atlas Search/vector index types, change streams
(`async for ev in Model.watch()`).

### 4.7 Lifecycle, sessions, transactions, errors

- `Registry(uri)` owns one `AsyncMongoClient`; `async with`; closing is final; no I/O at import.
  The client binds to the first event loop that uses it.
- Models bind to a registry explicitly (`registry.bind(Article)` or `Registry(models=[...])`).
  No class-level client.
- Transactions. `AsyncClientSession.with_transaction(callback, ...)` is callback-based
  **[probed, pymongo 4.18.3]**, because retrying means re-running the body. Two APIs, both explicit:
  - `await registry.transaction(fn)` where `fn(session)` is re-runnable. Gets pymongo's
    `TransientTransactionError` and unknown-commit-result retries. **This is the recommended form.**
  - `async with registry.session() as s, s.start_transaction(): ...` for manual control. No body
    retry; documented as such.
- Like Mongoose's `Connection.transaction()`, `registry.transaction(fn)` restores the tracked state
  (snapshot, `is_new`, assigned id) of every document written inside it when the transaction aborts
  and before each retry, so in-memory documents never claim writes that were rolled back.
- Every model operation takes `session=`. Nothing is ambient.
- Observability: `Registry(on_command=...)` receives started/succeeded/failed events from pymongo
  command monitoring, with model and operation names attached. Logging and OpenTelemetry adapters
  ship in core; this is the equivalent of `mongoose.set('debug')`.
- Operations on a registry that is not open fail immediately. There is no command buffering.
- One error hierarchy that wraps and never shadows pymongo's; duplicate-key field extraction;
  no library-level retry beyond what the driver does (retryable reads/writes, `with_transaction`).

### 4.8 Resource (in `mongospecs-web`)

```python
articles = Resource(
    Article,
    ops=[List, Get, Create, Patch, Delete],
    scope=lambda ctx: Q.author == ctx.principal.id,     # row-level: ANDed into every op's filter
    guards={Delete: is_admin},                          # op-level: boolean
    page=Cursor(default=20, max=100),
)

@articles.op("publish", method="POST", path="/{id}/publish")
async def publish(ctx: Ctx[Article]) -> Article: ...

@articles.after(Create)
async def notify(ctx, result): ...
```

- `Operation` = name, method, path, input/output JSON Schema, async handler, guards. Pure data,
  no framework imports, testable without a server.
- Authorization has two parts. `guards` answer "may this principal call this op". `scope` returns
  a filter that is ANDed into List, Get, Patch and Delete, so row ownership is enforced in the
  query rather than by load-then-check.
- `Ctx.principal` is produced by an `authenticate(request) -> principal` callable supplied when the
  plug is mounted. The Resource never inspects framework request objects.
- Resource owns: query-param → filter parsing (allowlist from `Policy`, bounded operator set
  `eq ne in nin gt gte lt lte exists prefix`), body validation via Codec, pagination, PATCH
  semantics, ETag from `Revision`, RFC 9457 errors, OpenAPI. Raw mappings never reach Mongo.

### 4.9 ResourceBackend and plugs

```python
class ResourceBackend(Protocol[T]):
    async def get(self, id, *, scope) -> T | None
    async def list(self, query, *, scope) -> Page[T]
    async def create(self, data: dict) -> T
    async def patch(self, id, data: dict, *, scope, if_rev=None) -> T
    async def delete(self, id, *, scope) -> None
```

- `mongospecs` models satisfy it through a thin adapter; that is the only coupling between the
  two packages.
- `mongospecs_web.testing.FakeBackend` is an in-memory double for the plug contract suite. It only
  has to evaluate the bounded operator set above, so it cannot drift into a database emulator.

```python
class Plug(Protocol):
    def mount(self, app, resource: Resource, *, prefix: str, authenticate) -> None: ...
    def lifespan(self, registry: Registry): ...
```

| Plug | Notes |
|---|---|
| ASGI | `resource.asgi_app()`; the reference implementation the others are checked against |
| Starlette | Routes + lifespan |
| FastAPI | `APIRouter`; `response_model` is pydantic-only, so schema is injected via `responses=`/`openapi_extra` and bytes are returned |
| Litestar | Plugin + generated `Controller` |
| Quart | Blueprint + `before_serving`; no native validation, core validation carries it |

Matrix control: one parametrised contract suite; every plug × backend must produce the same
status codes, bodies, error documents and OpenAPI for the same cases.

### 4.10 AI-SDK / MCP (parked)

An `Operation` with in/out JSON Schema is already a tool definition. A later `mongospecs_web.plugs.mcp`
can expose a Resource with identical guards, scope and field policy. Only constraint now: keep
descriptors pure data.

## 5. Platform decisions

| Decision | Choice | Basis |
|---|---|---|
| Sync API | Dropped. v1 stays available on a `0.1.x` maintenance branch | |
| Driver | `pymongo.AsyncMongoClient`, `pymongo>=4.13,<5` | Motor deprecated 2025-05 [unverified date] |
| Python | min 3.11; CI 3.11–3.15 + free-threaded | 3.15.0 is installed locally; msgspec 0.22, attrs 26.1, pydantic 2.14 install and run on it [probed] |
| MongoDB server | Proposed floor 7.0; test 7.0 and 8.0; replica set and sharded | EOL dates [unverified] |
| Packaging | uv workspace, hatchling, two distributions | §4.1 |
| Lint/types | ruff (current), mypy strict; `ty` as a non-blocking second checker | `ty` maturity [unverified] |
| Tests | pytest + pytest-asyncio against real `mongod`; no mongomock | mongomock lacks async and transactions |
| Optional backends | Imported lazily via `importlib`; none is a hard dependency of `mongospecs` | |
| Versioning | `mongospecs` 0.2.0, `mongospecs-web` 0.1.0 | decided by Jay 2026-10-12 |

## 6. Evidence track: stress and benchmarks on real MongoDB

`bench/` is a workspace member that is never published. It answers performance questions with
data from real `mongod` (replica set, plus a sharded cluster for the relation matrix). It runs
independently of the feature graph: any benchmark runs as soon as the node it measures exists.

**Harness**
- `docker/` compose files; `bench run <workload> [--topology rs|sharded] [--backend ...]`.
- Results are written as JSON under `bench/results/<git-sha>/` with server version, topology,
  dataset shape and machine info, so runs are comparable and diffable.
- CPU-only microbenchmarks (codec, pipeline compile) also run under codspeed in CI. Anything that
  touches the network is run on demand, not as a CI gate: it is too noisy to block merges.

**Comparators.** Raw `pymongo` async returning dicts is the floor; ODM overhead is reported as a
ratio against it. **Mongoose** (Node.js, hydrated and `lean()`, same `mongod`, same datasets) is the
production reference: for each workload the report shows v2's overhead over raw pymongo next to
Mongoose's overhead over the raw Node driver, since cross-runtime absolute times are not
comparable. v1 (sync) is a reference point only. Mongoz and Beanie are optional comparators.

**Workloads**

| ID | Workload | Question it answers |
|---|---|---|
| B1 | Materialise N docs × {small, wide, nested} × backend × {validated, trusted} | What share of read time is decode? Does `trusted` matter? |
| B2 | `find` end to end, 1 / 100 / 10k docs | ODM overhead over raw pymongo |
| B3 | `include` via `$lookup` vs batched `$in`, fan-out 1 / 10 / 100 / 1000, rs and sharded | Default strategy and `auto` heuristic |
| B4 | Bulk insert, atomic patch, upsert | Write-path overhead |
| B5 | Offset vs cursor pagination at depth; `$facet` vs concurrent count | Pagination defaults |
| B6 | Hook and transformer overhead per op with 0 / 3 / 10 plugins | Cost of the plugin model |
| B7 | HTTP request → response through each plug × backend | Web-layer overhead per plug |
| B8 | Tracked vs `.lean()` vs `.raw()` reads; delta `save()` vs full `$set`; memory per tracked doc | Cost of change tracking; whether tracked should stay the default |

**Stress**

| ID | Scenario | Pass condition |
|---|---|---|
| X1 | Thousands of concurrent tasks against a small pool | No deadlock; pool-wait errors surface as typed errors |
| X2 | Transaction contention on hot documents with `registry.transaction(fn)` | No lost updates; retries bounded |
| X3 | Optimistic-lock storm on one document via `Revision` | Exactly one winner per revision |
| X4 | Task cancellation mid-operation and mid-transaction | No leaked sessions or connections |
| X5 | Result documents approaching 16MB through `$lookup` | Typed error or automatic batch fallback, never a raw driver error |
| X6 | Change stream consumer across primary step-down | Resume without loss or duplication |
| X7 | Free-threaded build, many threads each with its own loop and registry | No cross-registry state |

**The Rust decision** is an output of this track and nothing in §7 depends on it.
- Input: B1 and B2 profiles.
- Proposed rule (to confirm): open the question only if decode-plus-materialise is the dominant
  cost of end-to-end reads on real MongoDB after the cheap options are exhausted (`trusted` reads,
  server-side projection, `RawBSONDocument` paths). Otherwise the answer is recorded as "no".
- If it is ever "yes", it becomes a separate optional distribution behind the existing `Codec`
  seam. No design in this document changes.

## 7. Work graph

There are no phases and no dates. Work is a dependency graph of nodes. This section is the
operating contract for whichever agent picks up a node.

### 7.1 Rules

1. **Ready set.** A node is ready when every node in its `needs` column is done. All ready nodes
   may be worked concurrently, each on its own branch or worktree.
2. **Done means the check passes.** Every node has an executable check. A node is done when that
   check passes on a clean checkout and the workspace-wide gate (`ruff`, `mypy`, full test suite
   against real `mongod`) is still green. No node is done by assertion.
3. **Contracts before implementations.** `C*` nodes produce a protocol plus a contract test suite.
   Implementations are done when they pass the suite unmodified.
4. **Amending a contract.** If an implementation cannot satisfy a contract, the agent does not
   bend the tests. It writes the amendment into this document, lists the done nodes it invalidates,
   and re-runs their checks.
5. **Spikes are disposable.** `S*` nodes exist to confirm or falsify a design assumption. Their
   output is a recorded result in §10 and, if needed, an amendment. Spike code is not merged.
6. **v1 tests are the behaviour inventory.** Each v1 test is either ported to v2, or listed in
   `docs/migration.md` as intentionally dropped with the replacement. None is silently lost.
7. **No stubs.** A node delivers working code or is not started.
8. **Escalate to Jay** when: a spike falsifies a design in §4; an amendment would change a public
   API already used by `mongospecs-web`; anything is published, tagged or force-pushed; an open
   decision in §10 blocks a ready node.
9. **Mongoose is the tiebreak.** When a behaviour is not decided in this document, the agent reads
   the Mongoose documentation for the equivalent feature and adopts its semantics, unless the
   behaviour is on the do-not-inherit list in §3.3. The choice is recorded in `docs/parity.md`
   (feature, Mongoose behaviour, v2 behaviour, same/diverges and why). This is not an escalation.
10. **Parity is tested, not claimed.** Each row of the §3.3 parity table is done when
    `docs/parity.md` links it to the tests that cover it.

### 7.2 Nodes

**Foundation**

| ID | Node | Needs | Check |
|---|---|---|---|
| F1 | Cut `0.1.x` maintenance branch from `main`; decide the fate of uncommitted `mixins-test` work | — | Branch exists; working tree clean |
| F2 | uv workspace skeleton per §4.1: both packages, `bench/`, shared tool config | F1 | `uv sync --all-packages` and `uv build --all-packages` succeed |
| F3 | Run the v1 suite once under uv; record pass/fail as the behaviour inventory | F1 | Inventory file lists every v1 test with an initial disposition |
| F4 | `docker/` compose for replica set and sharded cluster; pytest fixtures for both | F2 | A test that commits a transaction passes on rs; a `$lookup` test passes on sharded |
| F5 | CI: matrix Python 3.11–3.15 + free-threaded × MongoDB 7.0/8.0; ruff, mypy, wheels | F2, F4 | Pipeline green on the empty workspace |

**Spikes**

| ID | Question | Needs | Result feeds |
|---|---|---|---|
| S1 | Vertical slice: one model on msgspec and pydantic, real rs: bind, insert, find, atomic patch, `registry.transaction(fn)`, one `$lookup` include, one ASGI endpoint | F4 | C1–C4, W1 |
| S2 | `$facet`, `$graphLookup`, `$unionWith`, `$lookup` restrictions on sharded collections and inside transactions; 16MB behaviour | F4 | §4.6, R1–R3 |
| S3 | `Annotated` marker introspection under deferred annotations and forward refs on 3.14/3.15, all three backends, module-level and nested models | F2 | C1 |
| S4 | Per-field validation fidelity: does `validate_field` run model validators/converters on each backend (pydantic `field_validator`, msgspec `Meta` constraints, attrs validators)? | F2 | C1, W2 |
| S5 | FastAPI and Litestar: can externally supplied JSON Schema fully replace native response/DTO schema generation? Read official docs | F2 | P3, P4 |
| S6 | Change tracking: snapshot side table on all three backends under GC pressure, free-threading, copies and pickling; diff semantics for nested models, lists, dicts, `None` vs missing | F2 | C6, K12 |
| S7 | Mongoose parity inventory: read the Mongoose docs (and its test names) for every row of §3.3 not tagged [docs]; correct the table; seed `docs/parity.md` | — | Every node citing §3.3 |
| S8 | Discriminators on pydantic and attrs through the Codec; `Ref["Name"]` forward-ref resolution at bind time | F2 | C1, K13 |

**Contracts**

| ID | Node | Needs | Check |
|---|---|---|---|
| C1 | `Codec` protocol, `FieldInfo`, markers, `Policy`, discriminator and alias support; backend-parametrised codec contract suite | S1, S3, S4, S8 | Suite collects and fails only for "backend not implemented" |
| C2 | Query AST + `Q` builder + compiler to `find` args and pipelines | S1 | Golden tests: AST → exact Mongo commands; property tests for immutability |
| C3 | Error hierarchy and mapping from pymongo | S1 | Every pymongo error class used by the driver maps to exactly one mongospecs error |
| C4 | Hook and transformer signatures, `OpCtx`, ordering semantics | S1 | Contract suite for ordering, fail-fast, session propagation |
| C5 | `ResourceBackend` protocol, `Operation`, `Ctx`, `FakeBackend`; plug contract suite | C1, C2, S1 | Suite runs green against `FakeBackend` through a trivial in-process caller |
| C6 | Change-tracking contract: snapshot, diff, `changes()`, `is_new`, restore-on-abort | C1, S6 | Backend-parametrised suite; fails only for "backend not implemented" |

**Core (`packages/mongospecs`)**

| ID | Node | Needs | Check |
|---|---|---|---|
| K1 | msgspec codec | C1 | Codec contract suite |
| K2 | pydantic codec | C1 | Codec contract suite |
| K3 | attrs codec | C1 | Codec contract suite |
| K4 | `Registry`, binding, lifecycle | C3 | Lifecycle tests: no I/O at import, closed is final, two registries isolated |
| K5 | Model operations: reads, insert, atomic update operators, `save`, upsert, delete, `DocumentNotFound` | C2, C3, K4, one of K1–K3 | CRUD suite on real rs, parametrised over every finished codec |
| K6 | Sessions and both transaction APIs | K5 | Commit, abort, retry-on-transient and cancellation tests on rs |
| K7 | Hook engine and plugin registration | C4, K5 | Hook contract suite |
| K8 | Plugins: timestamps, soft delete, revision | K7 | Per-plugin suites incl. scope inside counts and filter-based writes |
| K9 | Index planner: `plan_indexes`, `check_indexes`, gated destructive apply | K5 | Planner golden tests; apply test on rs |
| K10 | `upcast` and unknown-field preservation | K5 | Old-shape documents load; unknown fields survive `save` |
| K11 | Generic `SpecAdapter` for existing classes | K1–K3 | v1 adapter tests ported |
| K12 | Change tracking: tracked/lean/raw read modes, delta `save`, `hydrate`, state restore in `registry.transaction` | C6, K5, K6 | C6 suite on every codec; rs test: aborted and retried transactions leave documents consistent |
| K13 | Discriminators | K5, S8 | Mixed-type collection decodes to the right classes on every codec; subclass queries filter by key |
| K14 | Query casting, unknown-field errors, `trusted()` boundary | C2, K5 | Casting table tests per type; injection suite at the ODM level |
| K15 | `bulk_write`, `bulk_save`, `insert_many(ordered=False)` with transformers applied | K7, K12 | Timestamps and revisions correct on every bulk path; partial-failure report shape |
| K16 | Named scopes on the query type | K5 | Chaining and typing fixtures |
| K17 | Command observability: `on_command`, logging and OpenTelemetry adapters | K4 | Every operation kind emits start and end events with model and op names |
| K18 | Multi-database `using(db=)`, shard-key aware document writes | K5 | Two-tenant isolation test; sharded test asserts targeted writes via `explain` |

**Relations and streams**

| ID | Node | Needs | Check |
|---|---|---|---|
| R1 | `include` via `$lookup` and via batched `$in`, same results | K5, S2 | One suite, both strategies, identical output; scope applied to joined model |
| R2 | `$graphLookup` and `$unionWith` | R1 | Suites on rs; sharded restrictions asserted as typed errors |
| R3 | Cursor and offset pagination, optional total | K5, S2 | Stable ordering under concurrent inserts |
| R4 | Cascade rules | K6, K7, R1 | Atomicity test: failure mid-cascade leaves no partial state |
| R5 | Change streams with resume | K5 | X6 scenario as a test |
| R6 | Pick `include` default and `auto` heuristic | R1, B3 results | Decision recorded with the benchmark run it cites |
| R7 | Reverse relations (`Backref`), counts, polymorphic `Ref[A \| B]`, nested include paths and per-include options | R1, K13 | Same suite on both strategies; nothing from a `Backref` is ever written |

**Web (`packages/mongospecs-web`)**

| ID | Node | Needs | Check |
|---|---|---|---|
| W1 | `Resource`, built-in operations, guards, scope, `after`/custom ops | C5 | Resource suite against `FakeBackend` |
| W2 | Input pipeline: policy filtering, per-field validation, filter parsing | C5, one of K1–K3 | Security suite: operator injection, allowlist bypass, private-field leakage |
| W3 | RFC 9457 error mapping, ETag/`If-Match` from `Revision` | W1, K8 | Error and precondition suites |
| W4 | OpenAPI generation from operations and transformed schemas | W1, W2 | Generated document validates against the OpenAPI schema; snapshot per backend |
| W5 | `mongospecs` → `ResourceBackend` adapter | C5, K5, R3 | Plug contract suite against real rs through the adapter |
| P1 | ASGI plug | W1–W4 | Plug contract suite |
| P2 | Starlette plug | P1 | Plug contract suite |
| P3 | FastAPI plug | P1, S5 | Plug contract suite; `/openapi.json` matches W4 output |
| P4 | Litestar plug | P1, S5 | Plug contract suite |
| P5 | Quart plug | P1 | Plug contract suite |

**Evidence (`bench/`)** — see §6 for definitions

| ID | Node | Needs | Check |
|---|---|---|---|
| E1 | Harness, dataset generators, result format, raw-pymongo and v1 comparators | F4 | `bench run` produces a results file for the raw-pymongo comparator |
| E2 | Mongoose comparator: Node scripts running B1–B5 and B8 equivalents (hydrated and `lean()`) against the same `mongod` and datasets, plus the raw Node driver floor | E1 | Results file in the same format, overhead ratios computed |
| B1–B8 | Each workload | E1 + the node it measures | Results committed for both topologies where applicable |
| X1–X7 | Each stress scenario | E1 + the node it stresses | Scenario passes its condition; failures become issues against the owning node |
| D1 | Rust decision record | B1, B2 | §6 rule applied; answer and cited runs written into §10 |

**Release**

| ID | Node | Needs | Check |
|---|---|---|---|
| L1 | `docs/migration.md` complete from the F3 inventory | All K, R nodes | Every v1 test is ported or listed |
| L6 | `docs/parity.md` complete, plus a "coming from Mongoose" guide | All K, R nodes, S7 | Every §3.3 row links to tests or is marked a deliberate divergence |
| L2 | Docs site for both packages | All K, R, W, P nodes | Docs build; every public symbol documented |
| L3 | Consumer typing fixtures (positive and negative) | K5, W1 | mypy on fixtures gives exactly the expected errors |
| L4 | `mongospecs` 0.2.0 | K*, R*, L1, L3, L6, X1–X6, B8 | Wheel/sdist validation; clean install on each Python |
| L5 | `mongospecs-web` 0.1.0 | W*, P*, L4, B7 | Same, plus plug extras install cleanly one at a time |

### 7.3 Shape of the graph

```
F1 ─ F2 ─┬─ F4 ─┬─ S1 ─┬─ C1 ─┬─ K1 K2 K3 ─┐
         │      │      ├─ C2 ─┤            ├─ K5 ─┬─ K6 ─┐
         ├─ F3  ├─ S2  ├─ C3 ─┴─ K4 ───────┘      ├─ K7 ─ K8
         ├─ F5  │      ├─ C4 ─────────────────────┘      ├─ R4
         ├─ S3  │      └─ C5 ─ W1 W2 ─ W3 W4 ─ P1 ─ P2 P3 P4 P5
         ├─ S4  └─ E1 ─ B*/X* attach to whatever they measure
         └─ S5                    K5 ─ R1 ─ R2, R3 ─ W5      R1 + B3 ─ R6
```

Not drawn: `S6 → C6 → K12 → K15`, `S8 → K13 → R7`, and `K14`, `K16`, `K17`, `K18`, which hang off
`K5` (or `K4`) independently. `S7` has no prerequisites and can start immediately.

Widest parallel fronts: the eight spikes; the three codecs; the five plugs; all of `bench/`.
The single narrow point is `S1 → C1..C5`: nothing downstream should start before the vertical
slice has exercised every seam once.

## 8. v1 → v2 feature map

| v1 | v2 | Disposition |
|---|---|---|
| `Spec` per backend, `SpecAdapter` | Same idea, one generic adapter over `Codec` | Kept, reimplemented |
| `Q`, `And/Or/Nor`, `In/NotIn/All/ElemMatch/Exists/Size/Type/Not`, `SortBy` | Same surface, compiles to the AST | Kept |
| `one` / `many` / `find` / `find_one` / `by_id` / `ids` | `get`, `find(...).first()/.all()`, `.raw()`, `.ids()` | Replaced |
| Projection DSL with `$ref` / `$sub` / `$sub.` | `select()` + `include()`; embedded docs are typed fields decoded by the Codec | Replaced |
| Dict-of-refs and list-of-refs dereference | `include()` on `Ref[T]`, `list[Ref[T]]`, `dict[str, Ref[T]]` | Kept (dict form needs a test in R1) |
| `SubSpec` | Plain nested model of the same backend | Replaced |
| `Empty` / `unset()` | `Model.update(id, unset=[...])`; no sentinel type | Replaced |
| `update(*fields)` / `upsert` (read-then-write) | Atomic `update`, real `upsert`, delta `save()` | Replaced |
| `reload()` | `reload()`, also resets the snapshot | Kept |
| `insert_many` / `update_many` / `delete_many` taking instances | Kept for instances; filter-based variants added | Kept, extended |
| `soft_delete` / `find_active` | `DeletedAt` marker + scope | Replaced |
| `cascade` / `nullify` / `pull` | `on_delete` rules | Replaced |
| Blinker signals (`listen`, `stop_listening`) | Typed hooks | Replaced |
| `transaction()` context manager | `registry.transaction(fn)` / manual session | Replaced |
| `with_options()` | `Model.using(read_preference=..., write_concern=...)` returning a clone | Replaced |
| `Paginator` / `Page` with orphans | Cursor and offset pagination; orphans dropped | Replaced |
| `create_index` / `drop_index` / `list_indexes` | Declarative indexes + planner | Replaced |
| `helpers/bson.py`, `MongoEncoder/MongoDecoder` | Codec `encode`/`decode` | Folded in |
| `to_json_type`, `encode`, `decode`, `to_tuple` | Backend-native serialisation; `to_tuple` dropped | Reduced |
| `retry`, `handle_*_errors` decorators | Not carried over | Dropped |

## 9. Risks

1. **Backend × plug matrix (3 × 5).** Control: core-owned validation, one contract suite, schema
   produced by one algorithm.
2. **Per-field validation fidelity (S4).** If a backend cannot run its own validators for a single
   field, Patch needs a different mechanism for that backend. This is the most likely amendment.
3. **Scope leakage.** A soft-delete or row scope missed on one code path is a security bug.
   Control: a single choke point that compiles filters, and a test that enumerates every op.
4. **`$lookup` cost and size.** Control: both strategies always available; default chosen by B3.
5. **Filter-based writes skip document hooks.** A documented limitation users will still trip on.
6. **Contract churn.** Protocols are fixed right after one vertical slice. Rule 4 in §7.1 makes
   churn visible instead of silent.
7. **FastAPI and Litestar schema injection (S5).** If native schema generation cannot be fully
   replaced, those plugs degrade to "pydantic models only" or "msgspec models only" for native
   docs, with the ASGI-generated OpenAPI as the fallback.
8. **Free-threaded and 3.15 support in dependencies.** Installs and basic use are confirmed on
   3.15; free-threaded wheels are not.
9. **Two packages, one API boundary.** `ResourceBackend` is the only coupling; anything else that
   `mongospecs-web` imports from `mongospecs` is public API by definition.
10. **Change tracking cost and correctness.** One extra encoded copy per tracked document, and a
    diff on every save. Whole-container `$set` for changed lists can overwrite a concurrent
    element-level write; `Revision` is the guard, and explicit atomic operators the alternative.
    Control: B8 decides whether tracked stays the default; S6 covers the edge cases.
11. **Parity as scope pressure.** Mongoose has a decade of surface. The parity table in §3.3 is the
    boundary: features not in it are not in v2 unless added by amendment. The tiebreak rule decides
    semantics of features already in scope; it does not add features.
12. **Mongoose knowledge is second-hand.** Most of §3.3 is from memory of its docs. `S7` exists so
    no node is built on a misremembered behaviour.

## 10. Verification log and open decisions

### Probed for revisions 2 and 3

| Claim | Result |
|---|---|
| Field-bearing mixins via multiple inheritance | Fail on msgspec 0.22 and attrs 26.1 (`lay-out conflict`); work on pydantic 2.14; plain mixin fields dropped by msgspec |
| `Annotated` markers + `ClassVar` config readable on all three backends | Works on Python 3.13.5 and 3.15.0 |
| `create_model`-derived Patch keeps source validators | It does not |
| `AsyncClientSession.with_transaction` | Exists, coroutine, callback-based (pymongo 4.18.3) |
| Python 3.15.0 final exists | Installed locally via uv |
| New error decorators are used somewhere | They are not |
| Snapshot side table keyed by identity with a weakref finaliser | Works on pydantic and slotted attrs; msgspec needs `weakref=True` on the base, which subclasses inherit; a plain Struct cannot be weak-referenced |
| msgspec tagged unions decode a mixed list to the right classes | Works |
| pydantic can validate one field with the model's own `field_validator` | Works via `validate_assignment` (partial answer to S4) |
| Mongoose `Connection.transaction()` retries and resets document state on abort | Confirmed in its docs |
| Mongoose has four middleware kinds; `transactionAsyncLocalStorage` is opt-in; `strictQuery` strips or throws; `strict` strips unknown update paths; `select: false` exists | Confirmed in its docs |

### Still unverified

- MongoDB server EOL dates behind the 7.0 floor.
- `$facet` single-document/16MB behaviour; `$graphLookup`/`$unionWith`/`$lookup` restrictions on
  sharded collections and in transactions (→ S2).
- Per-field validation on each backend (→ S4).
- Marker introspection with deferred annotations and forward references (→ S3; only eager,
  non-forward cases were probed).
- Litestar DTO and FastAPI schema-override details (→ S5).
- Free-threaded wheels for msgspec, pydantic-core, attrs.
- Motor deprecation dates; Beanie's current driver; `ty` maturity.
- Mongoz internals not read (metaclasses, fields).
- Every §3.3 row not tagged [docs] (→ S7). Mongoose source and tests not read.
- Snapshot tracking under free-threading, `copy`, pickling, and for nested containers (→ S6).
- Discriminators on pydantic and attrs; forward-ref resolution (→ S8).

### Open decisions (each has a default so no node is blocked)

1. **Uncommitted `mixins-test` work** (F1): default is commit it to the branch as-is for the
   record, do not carry any of it into v2.
2. **MongoDB floor:** default 7.0.
3. **Version numbers:** decided: `mongospecs` 0.2.0; `mongospecs-web` starts at 0.1.0.
4. **Read validation default:** default validate; revisit with B1 data.
5. **Rust rule in §6:** default as written; needs your confirmation of what "dominant" means once
   B1/B2 numbers exist.
6. **Default read mode:** default tracked (Mongoose's default). Revisit with B8 data; the
   alternative is lean by default with `.tracked()` opt-in.
7. **Optional comparators in `bench/`** (Mongoz, Beanie): default off. Mongoose is on.
8. **Ambient transaction session** (Mongoose's `transactionAsyncLocalStorage`): default not
   offered. Say so if you want it as an opt-in despite the task-sharing hazard.
9. **Scope of §3.3:** the parity table is my selection of Mongoose features. Add or strike rows
   before agents start on `S7`.

## 11. Amendments made during implementation

Recorded per §7.1 rule 4. Each one names what changed and why; none invalidates a finished node.

| # | Section | Amendment | Why |
|---|---|---|---|
| A1 | §4.1 | Backend modules stay at the v1 import paths `mongospecs.msgspec`, `mongospecs.pydantic`, `mongospecs.attrs` (not `mongospecs/backends/`). Core modules are flat: `expr.py`, `query.py`, `manager.py`, `model.py`, `meta.py`, `codec.py`, `registry.py`, `tracking.py`, `updates.py`, `bulk.py`, `relations.py`, `indexes.py`, `streams.py`, `hooks.py`, `refs.py`, `markers.py`, `plugins/` | Import continuity with 0.1.x; smaller surface |
| A2 | §4.2 | `Ref[T]` is a wrapper object (`.id`, `.doc`, `.loaded`, `await .fetch()`), stored as the id. `OID` is the `ObjectId` annotation that works on every backend | msgspec cannot express a union of a custom type and a struct, so "id or document" needs one runtime type |
| A3 | §4.2 | `__spec__` is assigned without an annotation | With `from __future__ import annotations`, msgspec does not recognise `t.ClassVar[...]` and turns the attribute into a field [found in testing] |
| A4 | §4.2 | A string forward reference (`Ref["User"]`) works when the name is importable in the defining module, as usual in Python. For anything else use `Annotated[Ref, To("User")]`, resolved by class name against the registry the referring model is bound to | Backends resolve annotations themselves and fail on unresolvable names |
| A5 | §4.4 | `trusted` reads only change behaviour for flat pydantic models (`model_construct`). msgspec and attrs always convert | `model_construct` does not build nested models or references; msgspec has no unvalidated path |
| A6 | §4.4 | msgspec and attrs models are encoded by a Python walker; enum, date, decimal, set and UUID leaves are handled by driver codec options on the registry | `msgspec.to_builtins` cannot emit `ObjectId` (the hook result is re-encoded and recurses) [probed] |
| A7 | §4.3 | Timestamps are written with client time at millisecond precision, not `$currentDate` | The in-memory document must hold the value that was stored, without a second read |
| A8 | §4.3 | The revision check applies to `save()`, soft delete and `update(..., if_rev=)`. A hard delete of a loaded document is not revision-checked | There is no update document to carry the check; revisit if needed |
| A9 | §4.5 | Query values are cast by type only; storage validators and constraints are not applied to filters. Updates are fully validated | A search for a value that would be invalid to store is legitimate (Mongoose casts filters the same way) |
| A10 | §4.5 | An untracked document that is saved is inserted. A `lean()` document therefore cannot be saved (the insert fails on its existing id) | Keeps lean reads free of any per-document state |
| A11 | §4.7 | `Registry(on_command=...)` listeners: `logging_listener`, `otel_listener`, and `sentry_listener` (new, requested 2026-10-13). End-to-end trace verification against a Sentry-compatible server (rustrak) is node `E3` | Jay asked for Sentry SDK integration with rustrak for end-to-end tracing |
| A12 | §5 | Versions: `mongospecs` 0.2.0, `mongospecs-web` 0.1.0 | Decided by Jay |
| A13 | §7.2 | Spikes S3, S4, S6 and S8 were answered by the contract tests rather than by throwaway code: markers under deferred annotations (suite passes on 3.11, 3.13, 3.15), per-field validation on all three backends, snapshot tracking, discriminators and forward references | The tests are the durable form of the spike |
| A14 | §4.4 | The `trusted` read option is removed | Measured (B1): the pydantic fast path was slower than validating (17µs vs 5µs per wide document) and the flag did nothing on msgspec and attrs |
| A15 | §4.5 | A tracked document keeps the raw BSON bytes it was read from. The comparable snapshot (decode into the model, encode again) is built only when the document is saved | Measured (B2, B8): building the snapshot at load time made tracked reads about twice as slow as lean reads |
| A16 | §4.5 | A field the stored document lacks is not written back just because the model has a default for it; loading a document never produces changes | Follows from A15. What Mongoose does with defaults applied at load time was not verified |
| A17 | §4.4 | Documents read from the database are decoded leniently on strict backends (msgspec, attrs): an integer stored as a double is accepted. Input (creates, inserts from dicts, updates) stays strict | Data written by other clients is commonly numerically "loose"; pydantic is already lax |
| A18 | §4.2 | `Policy(private=True)` also means not writable, unless `insert`/`update` are set explicitly | Secure default found while building the web layer |
| A19 | §4.6 | Cursor pagination needs an index ending in `_id`; with an index on the sort key alone the server sorts in memory | Measured (B5): 20ms vs under 1ms for the first page of 50k documents |
| A20 | §4.8 | `Resource(server_fields=...)` sets values on create outside the field policy | Needed by the reference application (the author of a review) |
| A21 | §6 | The evidence track gained a production-shaped reference application ("storefront"), end-to-end tests that boot it as a real server process, and an HTTP load benchmark with a hand-written pymongo baseline (nodes E4-E6) | Requested by Jay 2026-10-13: real end-to-end, production-ready scenarios |
| A22 | §6 | Workload B7 (HTTP per plug) is the `LOAD` workload: a real server process under load, with server CPU per request as the primary metric | Measuring plugs through an in-process test client, and later through httpx over TCP, measured the client |
| A23 | §4.5 | An upsert gives the inserted document the model's non-null defaults through `$setOnInsert` | Otherwise a document created by upsert differs in shape from one created by `insert()` and queries on defaulted values miss it (Mongoose: `setDefaultsOnInsert`) |
| A24 | §4.2 | Subclass discovery ignores stale class copies | `attrs.define` replaces a class with a slotted copy and the original lingers until collected; found by the free-threaded CI job |
| A25 | §4.2 | The `Computed` marker became the `@computed` property decorator | A marker goes on an annotation; a computed value is a property and has none |
| A26 | §4.6 | Atlas Search and vector search indexes are not in 0.2. `Index` covers ordinary, compound, text, geospatial, TTL, partial and collated indexes | They need Atlas (or a `mongot` deployment) to create and to test; nothing here could verify them. Declared out of scope rather than shipped untested |
| A27 | §4.3 | A8 is superseded: deleting a loaded document checks its revision on hard deletes as well, and `delete(if_rev=)` / HTTP `If-Match` on DELETE are supported | Raised in review |
| A28 | §4.8 | Web defaults changed after review: totals are opt-in per resource, offset pages are capped by `max_offset`, request reads get a 10s time limit when nothing else sets one, bodies are size-limited while being read on every plug, the row scope is part of the DELETE filter, `Api(openapi=False)` and `Resource(explicit_writes=True)` exist | Raised in review |
| A29 | §4.5 | `bulk_save` writes are independent: successes are brought in sync in memory and the error lists the failures. `Query.update()` returns an int carrying `.matched` and `.modified`. Cursor pagination always fetches its sort keys. Cascades carry a visited set and work in chunks | Defects reproduced in review, each with a regression test |
| A30 | §4.2 | A field whose default is `None` is not stored while it is `None` (the key is omitted; setting it to `None` unsets it) | Second review: a unique sparse index collided on stored nulls. Consequence: `exists` filters mean "has a value" |
| A31 | §4.2 | Field names that shadow the `Document` API raise `ConfigurationError` (`meta.RESERVED_FIELD_NAMES`) | Second review: a field named `count` silently replaced `Model.count()` |
| A32 | §4.4 | `registry.transaction()` inside a running transaction raises unless `independent=True`. A context variable is used to detect this only; sessions are still always passed explicitly | Second review: an inner transaction committed while the outer aborted |
| A33 | §4.3 | Filter values are cast by one rule on all backends (exact type, else text that reads as the type). `find().on_invalid("raise" \| "skip" \| "raw")` | Second review |

New node:

| ID | Node | Needs | Check |
|---|---|---|---|
| E3 | End-to-end tracing: Sentry SDK in an app using `mongospecs-web`, sending to rustrak in Docker; one HTTP request yields one trace containing the request span and `Model.op` database spans | P1, K17 | A script asserts the trace arrived in rustrak with the expected spans |
| E4 | Storefront reference application: auth, guards, row scope, transactional orders, soft delete, ETags, reverse relations, exactly-once change-stream projector, health check, graceful shutdown; runs on every plug and model backend | P1-P5, K6, R5 | The e2e suite below |
| E5 | End-to-end tests over real HTTP against the storefront under uvicorn: customer journey on every plug, ordering on every backend, concurrent buyers never oversell, multi-item orders are atomic, projector resumes across restarts without double counting, in-flight requests finish on shutdown, orders stay consistent across a primary step-down | E4 | `uv run pytest bench/tests/test_e2e_storefront.py` passes; the stock invariant holds after each scenario |
| E6 | HTTP load benchmark: mixed browse/detail/order traffic against the running storefront on every plug and backend, next to a hand-written pymongo + Starlette implementation of the same endpoints | E4, E1 | Results committed; the stock invariant holds after every run |

Bugs the end-to-end tests found that the unit and contract suites had not: aggregation paths
(`aggregate`, `union`, `graph`, backref counts) broke whenever a default time limit was configured;
string ids assigned to reference fields were stored as strings on msgspec and attrs; unexpected
server errors were not logged anywhere.

### Decisions taken from the evidence (see `docs/benchmarks.md`, results `6dc837f`)

| Node | Decision | Basis |
|---|---|---|
| R6 | `include(strategy="auto")` uses batched queries. `$lookup` stays available explicitly | `$lookup` never won clearly on a replica set (two runs disagreed at fan-out 100), was 2.5-3x slower when many parents share few targets, and 5-9x slower through mongos for small fan-outs |
| D1 | No Rust in 0.2. The only candidate worth a native component is a BSON-to-model decoder for bulk reads, as a separate optional package behind `Codec.decode(bytes)` | BSON-to-dict is essentially all of the driver's time on bulk reads (10.1 us per nested document against 3.3 us for msgspec's dict-to-model step) but an estimated 10-20% of server CPU on request-sized reads. Needs Jay's call on whether bulk reads matter enough |
| Open decision 4 | Reads validate; the `trusted` option is removed | Measured slower than validating on pydantic, no effect elsewhere |
| Open decision 6 | Tracked stays the default read mode | 0-15% over lean and about 1.2 KB per document after the raw-BSON snapshot change |

### Review of the pull requests (2026-10-13)

A second session reviewed #6-#8 and reproduced five defects (duplicate pages with `select()`, a bare-id
`Location` header, `bulk_save` leaving memory out of sync, `bulk_write` skipping cascades, an unhelpful
error when saving a copy). All five are fixed with regression tests, along with an infinite recursion on
cyclic cascades found while reproducing them. Its read-only findings on the web layer are addressed in A28.

Known and not changed:

- `registry._BINDINGS` and `tracking._states` are process-global. `Model.find()` resolves the registry a
  model is bound to, so one model class cannot be bound to two open registries at once; code that needs
  several registries or one registry per thread uses `registry.manager(Model)`. A task-scoped "current
  registry" was suggested and is not built.
- S7 (confirming every Mongoose parity row against its documentation) is done for 28 of 35 rows;
  `docs/parity.md` marks the 7 that are still from memory (alias, `useDb`, per-query driver options,
  `exists`/counts, subdocuments and maps, getters/setters, virtuals).
