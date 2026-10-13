# Hooks and plugins

mongospecs has the same four kinds of middleware as Mongoose, with typed signatures.

| Kind | Where | Runs for |
|---|---|---|
| Document hooks | `@Model.before("insert" \| "save" \| "delete")`, `@Model.after(...)` | document-level operations |
| Command and filter rewriting | `Plugin.on_insert`, `on_update`, `on_filter`, `on_delete` | every write and read, including filter-based ones |
| Pipeline rewriting | `Plugin.on_pipeline` | `Model.aggregate` |
| Bulk hooks | `Plugin.before_bulk`, `after_bulk` | `insert_many`, `bulk_write`, `bulk_save` |

```python
from mongospecs import Plugin, Q, Registry, SpecConfig
from mongospecs.msgspec import Spec


class Note(Spec):
    __spec__ = SpecConfig(collection="notes")
    text: str = ""
    slug: str = ""


@Note.before("insert")
async def slugify(ctx, note):
    note.slug = note.text.lower().replace(" ", "-")
```

Hooks receive an `OpCtx` (the session, registry and model) and the document. They run in
registration order and the first exception stops the operation.

Document hooks do not run for filter-based writes such as `Note.find(...).update(...)`: no
documents are loaded. Plugins still rewrite those commands, which is how timestamps, revisions and
scopes keep working there.

## Writing a plugin

A plugin overrides only what it needs. This one confines every model of a registry to one tenant.

```python
class Tenant(Plugin):
    name = "tenant"

    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    def on_insert(self, ctx, doc):
        doc["tenant_id"] = self.tenant_id

    def on_filter(self, ctx, flt):
        return {"tenant_id": self.tenant_id}


registry = await Registry("mongodb://localhost/app", models=[Note], plugins=[Tenant("acme")]).open()
note = await Note(text="Hello World").insert()
assert note.slug == "hello-world"

await Note.collection().insert_one({"text": "someone else's", "tenant_id": "other"})
assert [n.text for n in await Note.find()] == ["Hello World"]
assert await Note.find().update(text="changed") == 1  # the other tenant's note is untouched
assert await Note.find().unscoped("tenant").count() == 2

await registry.client.drop_database("app")
await registry.close()
```

Plugins attach in three ways: by a field marker (the built-in timestamps, soft delete and
revisions), per model with `SpecConfig(plugins=[...])`, or to every model with
`Registry(plugins=[...])`.
