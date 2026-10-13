# Coming from Mongoose

mongospecs takes Mongoose as its reference for what a production ODM should do. This page maps
Mongoose features to their mongospecs form and to the tests that cover them, and lists where
mongospecs deliberately behaves differently.

Test paths are relative to `packages/mongospecs/tests/`. "Source" says how the Mongoose behaviour
was established: **docs** means it was re-read in the Mongoose documentation for this project,
**memory** means it is stated from general knowledge of Mongoose 8.x and has not been re-checked.

## Feature map

| Mongoose | mongospecs | Covered by | Source |
|---|---|---|---|
| Change tracking, delta `save()`, `isNew`, `modifiedPaths()` | snapshot at load; `save()` sends the diff; `doc.is_new`, `doc.changes()` | `test_crud.py::test_save_new_then_delta`, `test_tracking_tx.py` | memory |
| `lean()` | `.lean()` (models, untracked), `.raw()` (dicts) | `test_crud.py::test_read_modes` | docs |
| `Model.hydrate()` | `Model.hydrate(raw)` | `test_crud.py::test_hydrate_and_collection_escape_hatch` | memory |
| Middleware: document, query, aggregate, model | document hooks; `on_insert`/`on_update`/`on_filter`/`on_delete`; `on_pipeline`; `before_bulk`/`after_bulk` | `test_plugins_hooks.py` | docs |
| `schema.plugin()`, `mongoose.plugin()` | marker plugins, `SpecConfig(plugins=)`, `Registry(plugins=)` | `test_plugins_hooks.py::test_registry_level_plugin_applies_to_every_model` | memory |
| `timestamps` | `CreatedAt`, `UpdatedAt` | `test_plugins_hooks.py::test_timestamps_on_every_write_path` | memory |
| `optimisticConcurrency`, `versionKey` | `Revision` | `test_plugins_hooks.py::test_revision_increments_everywhere` | memory |
| `populate()` with `match`, `select`, `options`, `perDocumentLimit`, nested paths | `include()` with `match`, `select`, `sort`, `limit`, `per_parent_limit`, dotted paths | `test_relations.py` | memory |
| Virtual populate (`localField`/`foreignField`, `count`) | `Backref(...)`, `Backref(..., count=True)` | `test_relations.py::test_backrefs_and_counts` | memory |
| `refPath` | `Ref[A \| B]` | `test_relations.py::test_polymorphic_reference` | memory |
| Discriminators | `SpecConfig(discriminator=...)` | `test_models_misc.py::test_discriminators_share_a_collection` | docs |
| Query casting | values cast to the field type | `test_expr.py::test_values_are_cast_to_field_types` | memory |
| `strictQuery: 'throw'` | always: unknown fields raise | `test_expr.py::test_field_names_are_translated_and_checked` | docs |
| `sanitizeFilter`, `trusted()` | raw filters untrusted unless `trusted()` | `test_expr.py::test_untrusted_mappings_and_operator_injection` | memory |
| `select: false` | `Deferred` | `test_crud.py::test_deferred_field_is_not_loaded_or_clobbered` | docs |
| `immutable` | `Immutable` | `test_updates.py::test_immutable_and_required_fields_are_protected` | memory |
| `alias` | `Alias("x")` | `test_codec.py::test_alias_and_transient_fields` | memory |
| Query helpers | `@Model.scope` | `test_plugins_hooks.py::test_named_scopes` | memory |
| `orFail()` | `.or_fail()`, `.one()` | `test_crud.py::test_find_all_first_one_and_or_fail` | memory |
| `cursor()`, `eachAsync({parallel})` | `async for`, `.each(fn, concurrency=n)` | `test_crud.py::test_each_with_concurrency` | memory |
| `bulkWrite`, `bulkSave`, `insertMany({ordered: false})` | `bulk_write`, `bulk_save`, `insert_many(ordered=False)` | `test_bulk_pagination.py` | memory |
| `Connection.transaction(fn)` resets document state on abort | `registry.transaction(fn)` does the same, also before each retry | `test_tracking_tx.py::test_transaction_abort_rolls_back_database_and_memory` | docs |
| `syncIndexes`, `diffIndexes`, `autoIndex: false` | `plan_indexes`, `apply_indexes(drop=...)`; never automatic | `test_indexes.py` | memory |
| `shardKey` | `SpecConfig(shard_key=...)` | `test_streams_sharded.py::test_shard_key_is_added_to_document_writes` | memory |
| `createConnection`, `useDb` | several registries; `Model.using(db=...)` | `test_models_misc.py::test_using_another_database` | memory |
| `maxTimeMS`, `hint`, `comment`, `collation`, read/write concern | `.options(...)`, `SpecConfig`, `Registry(max_time_ms=)` | `test_registry_errors.py::test_default_max_time_ms_from_registry_and_model` | memory |
| `Model.aggregate()`, `Model.collection` | `Model.aggregate(pipeline, as_=)`, `Model.collection()` | `test_crud.py::test_aggregate_decodes_and_explain` | memory |
| `mongoose.set('debug')` | `Registry(on_command=...)` with logging, OpenTelemetry, Sentry listeners | `test_registry_errors.py::test_command_events_carry_model_and_operation` | memory |
| `Model.watch()` | `Model.watch()` | `test_streams_sharded.py::test_change_stream_events` | memory |
| `exists`, `countDocuments`, `estimatedDocumentCount` | `exists`, `count`, `estimated_count` | `test_crud.py::test_count_exists_ids_estimated` | memory |
| Capped, time-series, TTL options | `SpecConfig(collection_options=)`, `Index(ttl=)` | `test_indexes.py::test_collection_options_are_applied` | memory |
| Subdocuments, arrays of subdocuments, Maps | nested models, `list[T]`, `dict[str, T]`; tracked through the parent | `test_crud.py::test_nested_document_diff_uses_dotted_paths` | memory |
| Getters, setters, custom validators | your backend's converters and validators | `test_codec.py::test_validate_field_runs_the_models_own_validators` | memory |
| Virtuals | Python properties; `@computed` to include one in API output | `packages/mongospecs-web/tests/test_mongo_resource.py` | memory |
| Statics and methods | classmethods and methods | (plain Python) | memory |

## Deliberate differences

| Mongoose | mongospecs | Why |
|---|---|---|
| Global default connection and model registry | Explicit `Registry`; names resolved per registry | No hidden state, isolated tests |
| `bufferCommands` queues operations until connected | Operations fail immediately when the registry is not open | A dead connection should not look like a slow one |
| `strictQuery` can strip unknown filter paths, so a typo matches everything | Unknown fields raise `QueryError` | Fail closed |
| `strict` silently strips unknown update paths | Unknown fields raise | Same |
| Update validators are off unless `runValidators` | Atomic updates are always validated | Unvalidated writes should be the explicit case |
| Filter casting follows schema types | Same; storage validators are not applied to filters | A search for a value that would be invalid to store is legitimate |
| `findOneAndUpdate` returns the document as it was unless `new: true` | `Model.update` returns the updated document; `returning="before"` for the old one | Least surprise |
| `Model.aggregate()` results are uncast and plugins must hook it | Scopes apply to `aggregate()`; `as_=` decodes | Soft delete and tenancy should not have a hole |
| Indexes are built on startup by default | Never automatic | Unsafe on large collections |
| Collection names are pluralised | The class name, or what you declare | No guessing |
| `transactionAsyncLocalStorage` offers an ambient session | Sessions are always passed explicitly | Tasks spawned inside a transaction would silently share one session |
| Populated paths replace the id with the document | A `Ref` keeps `.id` and gains `.doc` | One type whether or not it is loaded |
| Arrays are tracked per element | A changed list is sent whole | Per-element intent is expressed with atomic operators |
| A document's delete is not version-checked | Soft delete and `save()` are revision-checked; hard delete is not | Recorded as a known gap |
