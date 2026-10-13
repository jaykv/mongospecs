# Running in production

```python
import logging
from typing import Annotated

from mongospecs import Action, Index, Registry, SpecConfig, logging_listener
from mongospecs.msgspec import Spec


class Account(Spec):
    __spec__ = SpecConfig(collection="accounts", indexes=[Index("email", unique=True), Index("-balance")])
    email: str = ""
    balance: Annotated[int, "cents"] = 0


events = []
registry = await Registry(
    "mongodb://localhost/app",
    models=[Account],
    max_time_ms=5000,  # default time limit for reads
    on_command=[events.append, logging_listener(level=logging.DEBUG)],
).open()
```

## Indexes

Nothing is built implicitly. `plan_indexes()` compares what is declared with what exists;
`apply_indexes()` creates what is missing and only drops or recreates with `drop=True`.

```python
plan = await registry.plan_indexes()
assert {a.name: a.action for a in plan} == {"email_1": Action.CREATE, "balance_-1": Action.CREATE}
await registry.apply_indexes(plan)
assert (await registry.plan_indexes()).in_sync
```

| Action | Meaning | Destructive |
|---|---|---|
| `CREATE` | declared, missing | no |
| `RETAIN` | declared and identical | no |
| `RECREATE` | same name, different definition | yes |
| `CONFLICT` | same keys under another name | yes |
| `DROP` | exists, not declared | yes |

`plan.advisories` lists non-blocking findings, such as a reverse relation whose foreign key has no
index.

## Observability

Every driver command produces `started` and `succeeded`/`failed` events carrying the model and the
mongospecs operation.

```python
await Account(email="a@example.com").insert()
started = [e for e in events if e.phase == "started" and e.model]
assert (started[-1].model, started[-1].op, started[-1].command_name) == ("Account", "insert", "insert")
```

`logging_listener()`, `otel_listener()` (extra `otel`) and `sentry_listener()` (extra `sentry`) are
ready-made listeners. With the Sentry SDK, database work shows up as `Account.insert`,
`Account.find` and so on inside the active trace, on Sentry or any compatible server.

## Errors

mongospecs raises its own errors and keeps the driver error as `__cause__`.

| Error | When |
|---|---|
| `ValidationError` | input does not fit the model |
| `QueryError` | unknown field, uncastable value, untrusted operator |
| `DocumentNotFound` | an acknowledged read or write matched nothing |
| `StaleDocumentError` | a revision check failed |
| `DuplicateKeyError` | unique index violated; `.fields` names the values |
| `OperationTimeout`, `ConnectionFailed`, `PoolTimeout` | the database is slow or unreachable |
| `DocumentTooLarge` | a document or pipeline result exceeded 16MB |

## One registry per model, or explicit managers

`Model.find()` and the other class methods use the registry the model is bound to, and a model
class can be bound to only one open registry at a time. When you need the same model on several
registries, or one registry per thread, ask the registry for a manager instead:
`registry.manager(Article).find(...)`. Documents loaded through a manager are saved through it.

## More than one database

```python
archive = Account.using(db="app_archive")
await archive.insert(Account(email="old@example.com"))
assert await archive.count() == 1 and await Account.count() == 1

await registry.client.drop_database("app_archive")
await registry.client.drop_database("app")
await registry.close()
```

Also: `SpecConfig(shard_key=[...])` adds the shard key to document-level writes so they are
targeted; `Model.watch()` opens a change stream; `SpecConfig(json_schema_validation="strict")`
installs a `$jsonSchema` validator derived from the model when indexes are applied.
