# v1 test inventory

The v1 suite (302 tests, all passing on `0.1.x` under mongomock) is the behaviour inventory for the
rewrite. Every v1 test name is listed with what became of it. "ported" means the behaviour is
covered by the named v2 test against real MongoDB; "replaced" means a different API covers the
need; "dropped" means the behaviour intentionally does not exist in v2.

| v1 test | runs | disposition | v2 coverage / reason |
|---|---|---|---|
| `test_all` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_and` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_attrs_adapter` | 1 | ported | packages/mongospecs/tests/test_codec.py::test_spec_adapter_turns_existing_class_into_document |
| `test_bson` | 3 | ported | `Codec.decode` accepts raw BSON. packages/mongospecs/tests/test_codec.py::test_decode_accepts_raw_bson_and_string_ids |
| `test_bson_with_spec` | 2 | ported | packages/mongospecs/tests/test_codec.py::test_round_trip_through_bson_ready_dict |
| `test_by_id` | 6 | ported | `Model.get(id)`. packages/mongospecs/tests/test_crud.py::test_get_returns_typed_document_or_none |
| `test_cascade` | 6 | ported | `OnDelete("cascade")`. packages/mongospecs/tests/test_relations.py::test_cascade_rules |
| `test_count` | 6 | ported | packages/mongospecs/tests/test_crud.py::test_count_exists_ids_estimated |
| `test_delete` | 6 | ported | packages/mongospecs/tests/test_crud.py::test_delete_and_reload |
| `test_delete_many` | 6 | ported | `Model.find(...).delete()`. packages/mongospecs/tests/test_updates.py::test_filter_based_update_and_delete |
| `test_dot_notation` | 6 | ported | nested paths in `Q` and dotted deltas. packages/mongospecs/tests/test_expr.py::test_comparisons, test_crud.py::test_nested_document_diff_uses_dotted_paths |
| `test_elem_match` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_equal` | 9 | ported / dropped | query form: packages/mongospecs/tests/test_expr.py::test_comparisons. Instance equality by id (attrs, msgspec, pydantic frames tests): dropped, v2 keeps each backend's own equality |
| `test_exists` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_flattern_projection` | 6 | dropped | internal helper of the projection DSL, which no longer exists |
| `test_get_collection` | 6 | ported | `Model.collection()`. packages/mongospecs/tests/test_crud.py::test_hydrate_and_collection_escape_hatch |
| `test_get_db` | 6 | replaced | `Registry.database()`. packages/mongospecs/tests/test_registry_errors.py |
| `test_get_fields` | 6 | ported | `meta_of(Model).fields`. packages/mongospecs/tests/test_codec.py::test_fields_expose_names_markers_and_policy |
| `test_greater_than` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_greater_than_or_equal` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_ids` | 6 | ported | packages/mongospecs/tests/test_crud.py::test_count_exists_ids_estimated |
| `test_in` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_insert` | 6 | ported | packages/mongospecs/tests/test_crud.py::test_insert_assigns_id_and_tracks |
| `test_insert_many` | 6 | ported | packages/mongospecs/tests/test_bulk_pagination.py::test_insert_many_ordered_and_unordered |
| `test_less_than` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_less_than_or_equal` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_listen` | 6 | replaced | typed hooks `@Model.before/after`. packages/mongospecs/tests/test_plugins_hooks.py::test_document_hooks_order_session_and_fail_fast |
| `test_many` | 6 | ported | `.all()`. packages/mongospecs/tests/test_crud.py::test_find_all_first_one_and_or_fail |
| `test_missing_client` | 1 | ported | `NotBoundError` / `RegistryClosedError`. packages/mongospecs/tests/test_crud.py::test_unbound_model_raises |
| `test_msgspec_adapter` | 1 | ported | packages/mongospecs/tests/test_codec.py::test_spec_adapter_turns_existing_class_into_document |
| `test_nor` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_not` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_not_equal` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_not_in` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_nullify` | 6 | ported | `OnDelete("nullify")`. packages/mongospecs/tests/test_relations.py::test_cascade_rules |
| `test_one` | 6 | ported | `.first()` / `.one()`. packages/mongospecs/tests/test_crud.py::test_find_all_first_one_and_or_fail |
| `test_operations_use_session` | 1 | ported | real sessions instead of mocks. packages/mongospecs/tests/test_tracking_tx.py::test_transaction_commits |
| `test_or` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_page` | 3 | replaced | `Page` from `query.page()`. packages/mongospecs/tests/test_bulk_pagination.py |
| `test_paginator` | 3 | replaced | `query.page(size, number=n)`. packages/mongospecs/tests/test_bulk_pagination.py::test_offset_pagination |
| `test_paginator_with_filter` | 3 | replaced | packages/mongospecs/tests/test_bulk_pagination.py::test_cursor_pagination |
| `test_paginator_with_orphans` | 3 | dropped | orphans are not carried over |
| `test_paginator_with_per_page` | 3 | replaced | packages/mongospecs/tests/test_bulk_pagination.py::test_offset_pagination |
| `test_paginator_with_projection` | 3 | replaced | `select()` composes with `page()` |
| `test_paginator_with_sort` | 3 | replaced | packages/mongospecs/tests/test_bulk_pagination.py::test_cursor_pagination |
| `test_projection` | 6 | replaced | `select()` + `include()` replace the `$ref`/`$sub` projection DSL. packages/mongospecs/tests/test_crud.py::test_select_projection_and_partial_save, test_relations.py |
| `test_pull` | 6 | ported | `OnDelete("pull")`. packages/mongospecs/tests/test_relations.py::test_cascade_rules |
| `test_pydantic_adapter` | 1 | ported | `SpecAdapter`. packages/mongospecs/tests/test_codec.py::test_spec_adapter_turns_existing_class_into_document |
| `test_python_sort` | 6 | dropped | relied on id-based ordering of instances; sort in the query instead |
| `test_q` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_reload` | 6 | ported | packages/mongospecs/tests/test_crud.py::test_delete_and_reload |
| `test_remove_keys` | 9 | dropped | internal dict helper with no v2 equivalent |
| `test_size` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_sort_by` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_spec` | 6 | ported | packages/mongospecs/tests/test_codec.py |
| `test_stop_listening` | 6 | replaced | hooks are registered declaratively; `mongospecs.meta.remove_hook` exists for tests |
| `test_timestamp_insert` | 6 | ported | `CreatedAt`/`UpdatedAt` markers. packages/mongospecs/tests/test_plugins_hooks.py::test_timestamps_on_every_write_path |
| `test_timestamp_update` | 6 | ported | packages/mongospecs/tests/test_plugins_hooks.py::test_timestamps_on_every_write_path |
| `test_to_json_type` | 6 | replaced | backend-native serialisation; the web layer produces JSON views |
| `test_to_refs` | 2 | ported | references encode as ids. packages/mongospecs/tests/test_codec.py::test_documents_and_ids_assigned_to_refs_encode_as_ids |
| `test_transaction_abort` | 2 | ported | packages/mongospecs/tests/test_tracking_tx.py::test_transaction_abort_rolls_back_database_and_memory |
| `test_transaction_signals_aborted` | 1 | dropped | the exception raised by `registry.transaction(fn)` is the abort signal |
| `test_transaction_signals_committed` | 1 | dropped | transaction signals are gone; the awaited `registry.transaction(fn)` returning is the commit |
| `test_transaction_success` | 1 | ported | against a real replica set now. packages/mongospecs/tests/test_tracking_tx.py::test_transaction_commits |
| `test_type` | 3 | ported | packages/mongospecs/tests/test_expr.py |
| `test_unset` | 6 | ported | `Unset(...)` operator. packages/mongospecs/tests/test_updates.py::test_compile_operators |
| `test_unset_many` | 6 | ported | `Model.find(...).update(Unset(...))`. packages/mongospecs/tests/test_updates.py |
| `test_update` | 6 | ported | `save()` sends a delta; atomic `Model.update`. packages/mongospecs/tests/test_crud.py::test_save_new_then_delta, test_updates.py |
| `test_update_many` | 6 | ported | `Model.find(...).update(...)` and `bulk_save`. packages/mongospecs/tests/test_updates.py::test_filter_based_update_and_delete |
| `test_upsert` | 6 | ported | single round trip now. packages/mongospecs/tests/test_updates.py::test_upsert_single_round_trip |
| `test_with_options` | 6 | replaced | `Model.using(read_preference=..., write_concern=..., db=...)`. packages/mongospecs/tests/test_models_misc.py::test_using_another_database |

Total: 302 v1 test runs across 71 distinct tests.
