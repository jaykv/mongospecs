# Contributing

## Setting up

You need [uv](https://docs.astral.sh/uv/) and Docker.

```
make install     # uv sync for the whole workspace
make up          # MongoDB in Docker: a replica set on :27117 and a sharded cluster on :27118
make check       # ruff, mypy --strict, and the whole test suite
make down        # stop MongoDB and delete its data
```

Tests run against real MongoDB; nothing is mocked. `make test-fast` leaves out the tests that
need the sharded cluster. To use another server, set `MONGOSPECS_TEST_URI` (a replica set) and
`MONGOSPECS_TEST_SHARDED_URI`.

## Where things are

| Path | What |
|---|---|
| `packages/mongospecs` | the ODM |
| `packages/mongospecs-web` | the REST layer and its framework plugs |
| `bench` | benchmarks, stress scenarios and the storefront reference application; never published |
| `examples` | the files shown in the tutorial; they are tests too |
| `docs` | the documentation site (`make docs` builds it) |
| `claudedocs/v2-plan.md` | the design of 0.2 and the record of every decision that departed from it |

## What a change needs

- **A test that fails without it.** For a bug, reproduce it first. Tests for behaviour that
  differs by model library are parametrised over `msgspec`, `pydantic` and `attrs`.
- **Docs for anything a user can see.** `python` blocks in `docs/guide` and `docs/web` are
  executed in order, per page, against MongoDB; `py` blocks are shown but not run. Prefer `python`.
- **A changelog entry** in `CHANGELOG.md`, marked **Breaking** when it is.
- **`make check` passing.**

Keep changes to one concern. If a change departs from the design in `claudedocs/v2-plan.md`, add
an amendment row there saying what and why.

## Plugs for other frameworks

A plug only translates requests and responses. Prove it with the contract suite:
`mongospecs_web.testing.run_contract` must produce the same transcript as the reference plug. See
[Plugs](docs/web/plugs.md).

## Releasing

The two packages always carry the same version and are released together: bump both
`pyproject.toml` files (and `mongospecs_web.__version__`), then publish `mongospecs` first,
because `mongospecs-web` depends on it.

Each package is released by publishing a GitHub release whose tag names it: `mongospecs-v0.2.0`,
then `mongospecs-web-v0.2.0`. The tag must match the version in that package's `pyproject.toml`;
the publish workflow checks this and runs the full suite before uploading.
