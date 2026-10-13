# Errors

mongospecs raises its own exceptions, all subclasses of `MongoSpecsError` and all importable from
`mongospecs`. When one wraps a driver error, that error is its `__cause__`.

```py
from mongospecs import DuplicateKeyError

try:
    await User(email="ann@example.com").insert()
except DuplicateKeyError as exc:
    print(exc.fields)        # {"email": "ann@example.com"}
```

## What each one means

The last column is what a `mongospecs-web` API answers when the error reaches it. Anything not
listed becomes a 500 with no detail, and the cause is logged.

| Error | Raised when | What to do | HTTP |
|---|---|---|---|
| `ValidationError` | a value does not fit the model: on read, on any write, in a request body. `.errors` lists `{"field", "message"}` | fix the input; for stored documents see `SpecConfig(upcast=...)` and `find().on_invalid(...)` | 422 |
| `QueryError` | a filter, sort or update names an unknown field, has a value that cannot be cast, or a raw dict contains an operator | fix the query; wrap filters you wrote yourself in `trusted()` | 400 |
| `DocumentNotFound` | `one()`, `or_fail()`, `update()`, `save()` or `delete()` matched nothing | treat as "gone" | 404 |
| `StaleDocumentError` | a `Revision` check failed: the document changed since you loaded it | reload and retry, or tell the user | 412 |
| `DuplicateKeyError` | a unique index was violated. `.fields` maps index keys to the offending values | report the conflict | 409 |
| `EncodingError` | a value has no BSON form (an integer beyond 64 bits, an unsupported type) | store a type MongoDB has | 422 |
| `NotLoadedError` | `ref.doc` was read before the reference was loaded | add `.include("field")` or `await ref.fetch()` | 500 |
| `OperationTimeout` | the operation passed its time limit (`max_time_ms`) | add an index, raise the limit, or retry | 504 |
| `PoolTimeout` | no pooled connection became free in time | the server is overloaded; see [Tuning for production](production.md) | 503 |
| `ConnectionFailed` | the server could not be reached | retry later | 503 |
| `TransactionError` | a transaction could not be committed, or `transaction()` was called inside another | pass the outer `tx` down | 500 |
| `DocumentTooLarge` | a document or an aggregation result passed 16 MB | split the document | 500 |
| `BulkWriteFailed` | part of a bulk write failed. `.write_errors` lists the items by input index | handle per item | 500 |
| `OperationFailed` | the server rejected the operation for another reason. `.code` is the server's error code | depends on the code | 500 |
| `ConfigurationError` | a model, plugin or registry is declared incorrectly; raised when the model is first used | fix the declaration | at startup |
| `NotBoundError` | a model was used before it was given to a registry | add it to `Registry(models=[...])` | at startup |
| `RegistryClosedError` | an operation ran on a registry that is not open | open it first; a closed registry cannot be reopened | 500 |

`QueryError`, `ValidationError` and `EncodingError` are also `ValueError`s, and `DocumentNotFound`
is a `LookupError`, so code that already catches those keeps working.

Batch operations that partly succeed (`bulk_save`) raise after writing what they could; the
documents that were **not** written are in the error's `.failed`.

## Errors of your own, over HTTP

In a guard, hook or custom operation, raise one of the errors from `mongospecs_web` to choose the
status yourself: `BadRequest`, `Unauthorized`, `Forbidden`, `NotFound`, `Conflict`,
`PreconditionFailed`, `Unprocessable`, or `HTTPError(detail, status=..., title=...)`.

```py
from mongospecs_web import Conflict

@orders.op("ship", method="POST")
async def ship(ctx):
    order = await ctx.load()
    if order.shipped:
        raise Conflict("This order has already shipped.")
    ...
```

Every error response is an RFC 9457 `application/problem+json` document:

```json
{"type": "about:blank", "title": "Unprocessable Content", "status": 422,
 "detail": "The request body failed validation.",
 "errors": [{"field": "title", "message": "Expected `str`, got `int`"}]}
```
