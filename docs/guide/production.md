# Tuning for production and high concurrency

Each item says what backs it: **measured** (a committed result under `bench/results/`), **tested**
(a test in this repository asserts it), or **guidance** (sound practice that nothing here verifies).

## Connections and timeouts

Any keyword `Registry` does not know is passed to the driver, so the pool is configured there:

```py
registry = Registry(
    uri,
    models=[...],
    max_time_ms=5_000,          # default time limit for reads
    maxPoolSize=50,             # connections per process (driver default: 100)
    minPoolSize=5,              # kept open, so a burst does not pay for handshakes
    waitQueueTimeoutMS=500,     # fail instead of queueing for a connection without bound
    serverSelectionTimeoutMS=5_000,
    connectTimeoutMS=2_000,
)
```

- **Set `waitQueueTimeoutMS`.** Without it a request that finds the pool empty waits indefinitely,
  so overload shows up as every request getting slower. With it the operation raises `PoolTimeout`,
  which the web layer answers with 503 and `Retry-After`. *Tested*
  (`test_a_full_pool_fails_fast_when_a_wait_limit_is_set`, `test_a_pool_timeout_is_a_503_with_retry_after`).
- **Size the pool for the database, not for the process.** `maxPoolSize` × processes is the number of
  connections the server must hold. One event loop rarely makes use of more than a few dozen. *Guidance.*
- **Give reads a time limit.** `Registry(max_time_ms=...)` or `SpecConfig(max_time_ms=...)`; an
  operation that exceeds it raises `OperationTimeout` (HTTP 504). The web layer applies 10 s to reads
  made for a request when neither is set. *Tested* (`test_listing_limits`).
- **One registry per process and event loop.** Open it when the server starts and close it when it
  stops (`plug.lifespan(registry)`, `init_app(..., registry=registry)`). A model class is bound to
  one open registry; use `registry.manager(Model)` where you need several. *Tested* (lifecycle and
  binding tests); do not share a registry across event loops. *Guidance.*

## Refusing work you cannot do

```py
api = Api(*resources, max_concurrency=200, max_body=1_048_576)
```

- `max_concurrency` answers the request beyond the limit at once with 503 and `Retry-After: 1`
  instead of letting it queue. Set it a little above the concurrency your pool and database
  sustain. *Tested* (`test_requests_beyond_max_concurrency_are_refused_at_once`); the right number
  for your deployment is *guidance*: find it with the load workload below.
- `max_body` is enforced while the body is read, on every plug. *Tested*
  (`test_3_no_plug_reads_a_body_past_the_limit`).
- `Offset(max_offset=...)` caps how far a client can page with skip/limit, and `Cursor(max=...)` the
  page size. *Tested.*

## Reads

- **Use `lean()` for anything you will not save.** A tracked document also keeps its stored bytes:
  3.6 KB against 2.4 KB of heap per document for the benchmark's shape (msgspec). The time
  difference between tracked and lean reads is small. `raw()` skips the model altogether and
  roughly halves the time, but a dict takes more memory than a model (5.3 KB here).
  (`bench/results/6dc837f/B8.json`). *Measured.*
- **Project.** `select("name", "price")` fetches only those fields; mark large, rarely needed
  fields `Deferred` so they are left out by default. *Tested.*
- **Stream large results** with `async for doc in Model.find(...)` instead of `.all()`, which holds
  the whole result. Pydantic instances take roughly 2.6 times the memory of msgspec or attrs ones
  (1829 against 709 bytes per lean document of the review's sample shape). *Measured by the
  reviewer; there is no committed result for it.*
- **The web layer reads without building models where it can.** `GET` on a resource fetches only
  the readable fields and serialises the stored document directly when the model has nothing that
  could change a value on the way (validators, converters, computed fields, a discriminator); any
  other model, and any single document whose stored values do not have their declared types, goes
  through the model. Serialising alone costs 2.5 µs per document this way against 4.3 (msgspec),
  6.4 (attrs) and 6.7 (pydantic) through the model; for a whole request the gain is smaller because
  the driver dominates (`bench/results/fb543de/B9.json`). *Measured*, and *tested* to give the same
  response (`test_raw_reads_equal_model_reads`). `Resource.raw_reads = False` turns it off.

## Pagination

- **Prefer cursors.** `Cursor()` (keyset) costs the same on page 1 and page 10,000; `Offset()`
  makes the server walk and discard everything before the page: at page 1,250 of a 50,000-document
  collection, 7.1 ms against 0.66 ms. *Measured* (`bench/results/6dc837f/B5.json`).
- **The index must end in `_id`.** Cursor pagination sorts by your key and then by `_id`:
  declare `Index("price", "id")`. With an index on the key alone the first page took 28.8 ms
  instead of 0.58 ms. *Measured* (B5).
- Documents without a value for the sort key are paged too (first ascending, last descending).
  *Tested* (`test_cursor_pages_include_documents_without_the_sort_key`).
- **`?total=true` is a second query over every match.** It is off unless the resource opts in
  with `Cursor(total=True)`. There is no cached or estimated total; if you need one, keep a counter.
  *Tested* (two commands; `test_listing_limits`).

## Relations

- `include()` uses one query per relation for the whole page by default. Independent relations are
  sent together, except inside a transaction. *Tested*
  (`test_independent_includes_are_in_flight_together`).
- **Give reverse relations a `per_parent_limit`.** `include("reviews", per_parent_limit=5)` chooses
  the children on the server, so a parent with 100,000 children costs the same as one with five.
  With 20 parents of 100 children each this took 1.5 ms of client CPU against 23 ms when every
  child was fetched (`bench/results/49a32a9/B9.json` before, `fb543de` after). *Measured and tested.*
- Index the foreign key of every reverse relation (`plan.advisories` lists the ones that lack one).
  *Tested.*

## Writes

- `insert_many`, `bulk_save` and `bulk_write` send one command per batch; inserting 1,000 documents
  costs 1.08 times the driver's own `insert_many` (`bench/results/6dc837f/B4.json`). *Measured.*
- `save()` sends only the changed fields; `Model.update(id, Inc(...))` is one atomic round trip and
  needs no read. *Tested.*
- Documents are validated before `insert`, `save`, `insert_many` and `bulk_save` write them. The
  check took about 3 microseconds per document on msgspec and 5 on pydantic for a seven-field
  document with two embedded objects, against about 6 and 4 for encoding it. For bulk loads of
  data that is already known to be sound, `validate=False` on the call or
  `SpecConfig(validate_writes=False)` on the model skips it. *Measured once, on one machine; not
  part of the recorded benchmark runs, which predate the check.*
- A web `DELETE` is one conditional write. It needs a read first only when the model has delete
  hooks, or cascades on a hard delete. *Tested* (`test_delete_is_one_write_unless_the_document_is_needed`).

## What needs a replica set

Transactions and change streams need a replica set (a single-node one is enough). That includes
**hard-deleting a document that other documents refer to with `OnDelete`**: the cascade and the
delete run in one transaction, which a standalone `mongod` cannot do. Soft deletes do not cascade
and need no transaction. *Tested on a replica set; not supported on a standalone server.*

Change streams resume across a primary step-down (`bench/results/6dc837f/X6.json`). To resume
across a restart of your own process, store `event.resume_token` and pass it back as
`resume_after=`. *Measured for the step-down; the restart pattern is guidance.*

## Observability

`on_command` listeners run for every command, inline. The logging, OpenTelemetry and Sentry
listeners do little per command, but anything slow in a listener is added to every operation.
*Guidance:* their cost has not been measured.

## Measuring your own workload

```sh
make up                                         # MongoDB replica set and sharded cluster
uv run python -m mongospecs_bench.cli run B9    # CPU per call and commands per operation
uv run python -m mongospecs_bench.cli run LOAD  # the storefront under HTTP load, against raw pymongo
```

Results are written to `bench/results/<git sha>/`. CPU time and command counts are stable from
run to run; wall-clock numbers on a shared machine vary by 20 to 30 percent, so compare those only
when the difference is large.
