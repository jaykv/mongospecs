# Benchmarks

Everything here was measured against real MongoDB by the unpublished `bench/` package. Raw result
files are in `bench/results/<git sha>/`; the numbers below are from `6dc837f`.

**Environment.** MongoDB 8.0.32 in Docker (single-node replica set; a two-shard cluster where
stated), Python 3.13.5, pymongo 4.18.3, msgspec 0.22, pydantic 2.14, Apple Silicon (8 cores),
client and server on the same machine.

**How to read the numbers.**

- Absolute times depend on the machine. The comparisons that travel are ratios against the raw
  driver in the same process.
- Run-to-run variation on this setup is large: the same driver baseline moved by ±30% between runs.
  Treat a ratio within about 0.8–1.25 as "no difference". Conclusions below rest only on effects
  that were larger than that and repeated across runs.
- Cross-runtime absolute times (Python vs Node) are not comparable; overhead ratios are.

Reproduce with `make up` and `uv run mongospecs-bench run`, `run LOAD`, `stress`.

## Summary

| Question | Answer |
|---|---|
| What does a request cost in a real server compared with hand-written pymongo? | About 1.2× the server CPU per request (0.76 ms vs 0.63 ms), 0.83× the throughput at saturation |
| What do typed reads cost over the driver's dicts? | `raw()`: nothing measurable. `lean()`: 1.1–1.7× with msgspec and attrs, about 2.6× with pydantic, on 10,000 nested documents |
| What does change tracking cost? | 0–15% over a lean read and about 1.2 KB per document; a save sends 38 bytes instead of 659 |
| How does that compare with Mongoose? | Mongoose's tracked documents cost 3.8× its driver on the same workload and 3.75× the memory of lean; mongospecs' are 1.6× (msgspec) to 2.7× (pydantic) and 1.2–1.5× the memory |
| `$lookup` or batched queries for `include()`? | Batched. `$lookup` never won clearly; on a sharded cluster it was 5–9× slower for small fan-outs |
| Offset or cursor pagination? | Cursor, with an index ending in `_id`: flat 0.6 ms at any depth against 12.6 ms for offset at the last page |
| Do plugins cost anything? | Not measurably: within noise of zero for 10 plugins |
| Is Rust warranted? | Only for one thing, and only for bulk reads; see [the Rust question](#the-rust-question) |

## Requests in a real server

The storefront application runs under uvicorn (one worker) and is driven over TCP with a mix of
60% product listings (filter, sort, cursor page of 20), 30% product detail with reviews and a
review count, and 10% orders (a multi-document transaction). The baseline implements the same three
endpoints by hand with pymongo and Starlette, including the transaction.

Latency is measured with 2 virtual users (an unloaded server), capacity with 64. The primary metric
is CPU time of the server process per request, which does not depend on the load generator.

| Server | CPU ms / request | vs raw | Requests/s at saturation | list p50 | detail p50 | order p50 |
|---|---|---|---|---|---|---|
| raw pymongo + Starlette | 0.631 | 1.00 | 1512 | 1.20 ms | 2.11 ms | 3.74 ms |
| mongospecs, msgspec, pure ASGI | 0.764 | 1.21 | 1257 | 1.34 ms | 2.46 ms | 4.15 ms |
| mongospecs, msgspec, Litestar | 0.785 | 1.24 | 1239 | 1.37 ms | 2.48 ms | 3.95 ms |
| mongospecs, msgspec, Starlette | 0.796 | 1.26 | 1211 | 1.45 ms | 2.66 ms | 4.79 ms |
| mongospecs, msgspec, Quart | 0.842 | 1.33 | 1171 | 1.64 ms | 2.96 ms | 5.56 ms |
| mongospecs, msgspec, FastAPI | 0.877 | 1.39 | 1110 | 1.46 ms | 2.63 ms | 4.66 ms |
| mongospecs, pydantic, pure ASGI | 0.763 | 1.21 | 1288 | 1.49 ms | 2.62 ms | 4.40 ms |
| mongospecs, attrs, pure ASGI | 0.783 | 1.24 | 1245 | 1.43 ms | 2.53 ms | 3.99 ms |

No request failed in any run, and after every run the stock invariant held: for every product,
stock plus units on placed orders equals the initial stock, and no stock is negative.

What the extra 0.13 ms buys: validated typed models, the field policy, allowlisted filters, scopes,
ETags, problem documents and OpenAPI. In a profile of a listing the driver itself is the largest
part of the request; decoding and serialising the 20 documents comes next.

A first version of this benchmark used httpx as the load generator and reported both servers at
about 330 requests/s. That was the client's ceiling, not the servers': the server sat at 10–30%
CPU. The generator was replaced with a minimal keep-alive client and CPU per request became the
primary metric.

## Reads

10,000 documents fetched with `find()`, median of 15 runs, ratio to the driver returning dicts in
the same loop. "Nested" documents have a sub-document, a list of 8 sub-documents, a list and a map.

| Mode | msgspec | pydantic | attrs |
|---|---|---|---|
| `raw()` | 1.05 | 0.93 | 0.58–1.04 |
| `lean()` | 1.46 | 2.68 | 1.06–1.71 |
| tracked (default) | 1.61 | 2.70 | 1.21–1.93 |

`raw()` is the driver. The cost of `lean()` is the backend's own validation: msgspec converts a
nested document in 3.3 µs, attrs (through msgspec) in 8.3 µs, pydantic in 15.9 µs; the driver's
BSON-to-dict step before that takes 10.1 µs.

## Change tracking

5,000 nested documents:

| Backend | lean | tracked | Heap per document, lean → tracked |
|---|---|---|---|
| msgspec | 76 ms | 63 ms | 2.4 KB → 3.6 KB |
| pydantic | 121 ms | 126 ms | 7.1 KB → 8.3 KB |
| attrs | 85 ms | 90 ms | 2.5 KB → 3.8 KB |

A tracked document keeps the raw BSON bytes it was read from (about 1.2 KB here) and nothing else;
the comparable snapshot is built only if the document is saved. Changing one field and saving sends
a 38-byte update where a full `$set` would send 659 bytes.

This is the result of an optimisation made during this work. The first implementation re-encoded
every loaded document to build the snapshot up front:

| 10,000 nested documents, msgspec | Before (`4f3b2c1`) | After (`6dc837f`) |
|---|---|---|
| tracked read | 428 ms (4.3× driver) | 150 ms (1.6× driver) |
| lean read | 155 ms | 136 ms |

The same pass replaced one-await-per-document cursor iteration with batch fetches and removed a
"trusted read" option whose pydantic fast path measured slower than validating (17 µs against 5 µs
per wide document).

## Compared with Mongoose

Mongoose 8 on Node 22, same MongoDB, same document shapes, ratios against the raw Node driver.

| 10,000 nested documents | Mongoose | mongospecs (msgspec) | mongospecs (pydantic) |
|---|---|---|---|
| Lean read vs driver | 0.88 | 1.46 | 2.68 |
| Tracked documents vs driver | 3.79 | 1.61 | 2.70 |
| Tracked vs lean, time | 4.68 | 0.83–1.15 | 1.04 |
| Tracked vs lean, memory | 3.75 (1.9 KB → 7.2 KB) | 1.51 | 1.17 |
| Hydrating one nested document vs BSON decode | 4.71 | 0.33 | 1.57 |
| `insertMany` of 1,000 vs driver | 1.43 | 1.08 | 1.13 |
| …with timestamps and versioning | 1.97 | 1.26 | 1.67 |

The two are not doing identical work: Mongoose's lean objects are untyped, while a mongospecs lean
read validates into typed models, which is why Mongoose is ahead on lean reads. On tracked
documents, the default in both, mongospecs' overhead over its driver is lower.

## `include()`: `$lookup` or batched queries

Parents each referencing `fan_out` distinct children; ratio of `$lookup` time to batched time
(below 1 means `$lookup` is faster). Two full runs on the replica set are shown because they
disagree.

| Relation | Replica set, run 1 | Replica set, run 2 | Sharded cluster |
|---|---|---|---|
| list, fan-out 1 | 1.27 | 1.09 | 4.70 |
| list, fan-out 10 | 1.29 | 1.10 | 4.95 |
| list, fan-out 100 | 0.61 | 1.25 | 1.35 |
| list, fan-out 1000 | 0.78 | 0.96 | 0.90 |
| to-one, 2,000 parents sharing 20 targets | 3.16 | 2.48 | 9.02 |

**Decision: `strategy="auto"` means batched queries.** `$lookup` is never clearly ahead, is 2.5–3×
slower when many parents share few targets (the server repeats each target per parent), and is
5–9× slower through mongos for small fan-outs. It stays available as `strategy="lookup"` for the
case where a single round trip matters more than time. This is also what Mongoose does.

A `$lookup` whose result document would exceed 16 MB fails with a typed `DocumentTooLarge`; the
batched strategy loads the same data without trouble (stress scenario X5).

## Pagination

50,000 documents, pages of 20, sorted on an indexed field.

| Page | Offset | Cursor |
|---|---|---|
| 1 | 0.58 ms | 0.58 ms |
| 100 | 1.20 ms | 0.65 ms |
| 1,250 | 7.06 ms | 0.66 ms |
| 2,499 (last) | 12.62 ms | 0.67 ms |

- Cursor pagination sorts by the requested key and then by `_id`. With an index on the key alone
  the first page took 28.8 ms, because the server sorted in memory; with an index on
  `(key, _id)` it took 0.58 ms. Index listings accordingly.
- A total from a concurrent `count_documents` took 6.0 ms; the same page and total through `$facet`
  took 28.5 ms and is additionally bound by the 16 MB document limit. `$facet` is not used.

## Writes and plugins

Ratios to the driver; these are single round trips where the network dominates and noise is
highest.

| Operation | msgspec | pydantic | attrs |
|---|---|---|---|
| `insert_many` of 1,000 | 1.08 | 1.13 | (noisy) |
| …with timestamps and revision | 1.26 | 1.67 | (noisy) |
| `Model.update` ×200 (returns the document) | 1.11 | 1.29 | 1.20 |
| `doc.save()` delta ×200 | 1.54 | 1.06 | 0.91 |
| `Model.upsert` ×200 (returns the document) | 1.32 | 1.12 | 1.11 |

Per-operation cost with 0, 3 and 10 no-op plugins on a registry differed by less than the noise
(0.94–1.20× with no trend), as did the three built-in plugins.

## Stress

All scenarios pass at full size.

| Scenario | Result |
|---|---|
| X1: 3,000 concurrent tasks on a pool of 5 | all completed; a starved pool raised `PoolTimeout` |
| X2: 40 contending read-modify-write transactions | counter reached 40; 174 automatic retries, no lost update |
| X3: 40 writers on one revisioned document | exactly one winner per revision; final revision 41 |
| X4: 141 of 200 tasks cancelled mid-operation or mid-transaction | no open transactions left on the server, no leaked tracking state |
| X5: `$lookup` result over 16 MB | typed `DocumentTooLarge`; batched strategy loaded everything |
| X6: change stream across a primary step-down | 20 of 20 events, in order, no duplicates |
| X7: 8 threads, each with its own event loop and registry | no cross-registry state |

X7 ran on a standard (GIL) build. The unit suite runs on the free-threaded build in CI as an
experimental job.

## End-to-end

`bench/tests/test_e2e_storefront.py` boots the storefront under uvicorn and talks to it over HTTP:

- a full customer journey on each of the five plugs, and ordering on each model backend
- 40 concurrent buyers for 10 units: exactly 10 orders succeed, 30 are refused, stock ends at 0
- multi-item orders are all-or-nothing, including opposing lock orders that must not deadlock
- the change-stream projector counts every sale exactly once across a restart and a period with no
  projector running
- requests in flight finish when the server is asked to stop
- under traffic, a primary step-down produces only declared errors (503/504), and afterwards every
  accepted order has moved stock exactly once

A separate check (`mongospecs-bench trace`) sends requests through the Sentry SDK to a local
[rustrak](https://github.com/rustrak/rustrak) server and asserts that each request arrives as one
trace containing the HTTP transaction and its `Model.op` database spans.

These tests found defects the unit and contract suites had not: aggregation paths failed whenever a
default time limit was configured, ids given as strings to reference fields were stored as strings
on msgspec and attrs, and unexpected server errors were not logged.

## The Rust question

The plan asked whether a Rust component is justified, to be decided from these measurements.

Where the time goes when reading 10,000 nested documents with msgspec models:

| Step | Per document |
|---|---|
| Driver: BSON bytes to Python dict | 10.1 µs |
| msgspec: dict to model | 3.3 µs |
| Everything else in mongospecs | under 1 µs |

- For **bulk reads**, decoding BSON into dicts is essentially all of the driver's time: measured
  alone it takes 101 ms for 10,000 documents, and the driver's whole read took 93 ms in the same
  run (equal within noise). It is about two thirds of a lean read. Decoding BSON
  straight into models, skipping the dict, is the one change that could make mongospecs faster than
  the raw driver rather than slower. That is a native-code job.
- For **request-sized reads** (20 documents) that step is an estimated 10–20% of the server CPU
  per request (20 documents at 3–7 µs against 0.63 ms); the driver's asynchronous machinery
  dominates. A native decoder would gain little there.
- Nothing else measured (query building, plugins, tracking, serialisation) is large enough to move
  to Rust.

**Recommendation:** no Rust in 0.2. If bulk reads matter to you, the worthwhile project is a
BSON-to-model decoder as a separate optional package behind `Codec.decode(bytes)`. The seam is
already there: tracked reads receive undecoded BSON from the driver today. A Rust driver is not
indicated by anything here.

## Limits of these measurements

- One machine, client and server together, MongoDB in Docker on macOS. Network cost is unrealistically
  low and variance is high.
- One uvicorn worker; no TLS, no proxy.
- The free-threaded build has not been benchmarked.
- The Mongoose comparison uses default settings on both sides and no tuning of either.
