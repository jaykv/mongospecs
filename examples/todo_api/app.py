"""A small REST API with mongospecs-web: per-user to-do lists.

    pip install "mongospecs[msgspec]" mongospecs-web uvicorn
    docker compose -f examples/compose.yaml up -d --wait
    uvicorn app:app --app-dir examples/todo_api

    curl -s -X POST localhost:8000/api/tasks -H 'authorization: Bearer ann-token' \
         -H 'content-type: application/json' -d '{"title": "Write docs"}'
    curl -s 'localhost:8000/api/tasks?done=false&sort=-title' -H 'authorization: Bearer ann-token'
"""

import os
from datetime import datetime
from typing import Annotated

from mongospecs import CreatedAt, Index, Policy, Q, Registry, Revision, SpecConfig
from mongospecs.msgspec import Spec
from mongospecs_web import Api, Create, Delete, Get, List, Patch, Resource, Unauthorized

URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/todo?replicaSet=rs0")

# Stands in for your real user store or token verification.
TOKENS = {"ann-token": "ann", "bob-token": "bob"}


class Task(Spec, kw_only=True):
    __spec__ = SpecConfig(collection="tasks", indexes=[Index("owner", "title")])

    # Policy says what a client may do with each field.
    title: Annotated[str, Policy(filter=("eq", "prefix"), sort=True)]
    done: Annotated[bool, Policy(filter=("eq",))] = False
    owner: Annotated[str, Policy(insert=False, update=False)] = ""  # set by the server
    note_to_self: Annotated[str | None, Policy(private=True)] = None  # never leaves the server
    created_at: Annotated[datetime | None, CreatedAt] = None
    rev: Annotated[int, Revision] = 0  # the ETag, checked by If-Match


def authenticate(request):
    """Who is calling: the principal, or None for an anonymous request."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if not token:
        return None
    if scheme != "Bearer" or token not in TOKENS:
        raise Unauthorized("Unknown token.", headers={"www-authenticate": "Bearer"})
    return TOKENS[token]


def logged_in(ctx):
    if ctx.principal is None:
        raise Unauthorized("Log in first.", headers={"www-authenticate": "Bearer"})
    return True


tasks = Resource(
    Task,
    ops=[List, Get, Create, Patch, Delete],
    guards=[logged_in],  # who may call the operations
    scope=lambda ctx: Q.owner == ctx.principal,  # which rows they may see and change
    server_fields=lambda ctx: {"owner": ctx.principal},  # values the server sets on create
)


@tasks.op("complete", method="POST", summary="Mark a task as done")
async def complete(ctx):
    task = await ctx.load()  # 404 unless the task is within the caller's scope
    task.done = True
    return await task.save()


registry = Registry(URI, models=[Task])
api = Api(tasks, prefix="/api", title="To-do", authenticate=authenticate)
app = api.asgi(registry=registry)  # opens the registry on startup and closes it on shutdown
