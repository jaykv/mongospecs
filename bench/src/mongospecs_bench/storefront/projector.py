"""Sales statistics kept current from the orders change stream.

Each event is applied in a transaction together with the stream's resume token,
so an event is counted exactly once even if the process dies between events,
and a restarted projector continues where the last one stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import typing as t

from mongospecs import Inc, MongoSpecsError, Q, Registry, Set

log = logging.getLogger("storefront.projector")


class SalesProjector:
    name = "sales"

    def __init__(self, registry: Registry, models: t.Any) -> None:
        self.registry = registry
        self.m = models
        self._task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sales-projector")
        await self.ready.wait()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _apply(self, event: t.Any) -> None:
        order = event.document
        sign = 1 if event.operation == "insert" else -1  # an update here is a cancellation

        async def work(tx: t.Any) -> None:
            for line in order.lines:
                await self.m.SalesStat.upsert(
                    Q.sku == line.sku,
                    Inc(units=sign * line.qty, revenue=sign * line.qty * line.unit_price),
                    session=tx,
                )
            await self.m.ProjectorState.upsert(
                Q.name == self.name,
                Set(resume_token=dict(event.resume_token)),
                Inc(events=1),
                session=tx,
            )

        await self.registry.transaction(work)

    async def _run(self) -> None:
        pipeline = [
            {
                "$match": {
                    "$or": [
                        {"operationType": "insert"},
                        {"operationType": "update", "updateDescription.updatedFields.status": "cancelled"},
                    ]
                }
            }
        ]
        while True:
            state = await self.m.ProjectorState.find(Q.name == self.name).first()
            options: dict[str, t.Any] = {"max_await_time_ms": 250}
            if state is not None and state.resume_token:
                options["resume_after"] = state.resume_token
            try:
                async with self.m.Order.watch(pipeline, **options) as stream:
                    self.ready.set()
                    async for event in stream:
                        await self._apply(event)
            except asyncio.CancelledError:
                raise
            except (
                MongoSpecsError
            ) as exc:  # lost the stream (step-down, network): resume from the stored token
                log.warning("projector restarting after %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(0.5)
