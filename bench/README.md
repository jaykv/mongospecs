# mongospecs-bench

Benchmarks, stress scenarios and an end-to-end tracing check for mongospecs, all against real
MongoDB. This package is a workspace member and is never published.

```
make up                                             # replica set + sharded cluster
uv run mongospecs-bench run                         # workloads B1-B8
uv run mongospecs-bench run B3 --topology sharded
uv run mongospecs-bench stress                      # scenarios X1-X7
uv run mongospecs-bench report                      # markdown summary of the newest results

docker compose -f docker/compose.rustrak.yaml up -d
uv run mongospecs-bench trace                       # Sentry SDK -> rustrak, end to end

cd bench/mongoose && npm install && npm run bench   # Mongoose comparator
```

Results are JSON files under `results/<git sha>/`, each with the MongoDB version, topology,
Python version and machine. `--quick` uses tiny sizes to prove the harness runs; do not draw
conclusions from quick numbers.

Absolute times depend on the machine. The comparisons that travel are ratios: mongospecs over raw
`pymongo`, and Mongoose over the raw Node driver.

| ID | Measures |
|---|---|
| B1 | Turning raw documents into models, per backend, next to the driver's BSON decode |
| B2 | `find()` end to end for 1, 100 and 10,000 documents in raw, lean and tracked modes |
| B3 | `include()` by `$lookup` vs batched `$in` across fan-outs, on both topologies |
| B4 | Inserts, atomic patches, delta saves and upserts |
| B5 | Offset vs cursor pagination at depth; total by count vs `$facet` |
| B6 | Per-operation cost of plugins |
| B7 | HTTP request to response through each plug and model backend |
| B8 | Time and memory of change tracking; size of a delta save |
| X1-X7 | Pool pressure, transaction contention, optimistic-lock storm, cancellation, 16MB results, change stream across a step-down, threads with their own loops |
