from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from typing import Any

from .models import ScheduledQuery
from .store import RuntimeStore, ScheduleSource
from .transport import HttpCubeTransport


LOGGER = logging.getLogger("metadata_driven_v5.cube_scheduler")


class CubeSchedulerService:
    def __init__(
        self,
        source: ScheduleSource,
        runtime: RuntimeStore,
        transport: HttpCubeTransport,
        poll_seconds: int,
    ) -> None:
        self.source = source
        self.runtime = runtime
        self.transport = transport
        self.poll_seconds = max(1, int(poll_seconds))
        self.worker_id = "cube-scheduler:" + uuid.uuid4().hex
        self.stop = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []

    async def start(self) -> None:
        await asyncio.to_thread(self.runtime.ensure_indexes)
        self.tasks = [
            asyncio.create_task(self._schedule_loop(), name="cube-schedule-reader"),
            asyncio.create_task(self._outbox_loop(), name="cube-outbox-dispatcher"),
        ]

    async def close(self) -> None:
        self.stop.set()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(self.source.close)
        await asyncio.to_thread(self.runtime.close)

    async def sync_once(self) -> dict[str, int]:
        documents = await asyncio.to_thread(self.source.read_all)
        return await asyncio.to_thread(self.runtime.reconcile, documents)

    async def schedule_once(self) -> int:
        processed = 0
        while not self.stop.is_set():
            cursor = await asyncio.to_thread(self.runtime.claim_due, self.worker_id)
            if cursor is None:
                break
            try:
                await asyncio.to_thread(self.runtime.enqueue_claimed, cursor, self.worker_id)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                await asyncio.to_thread(
                    self.runtime.release_claim,
                    str(cursor.get("schedule_id") or cursor.get("_id") or ""),
                    self.worker_id,
                    f"{type(exc).__name__}: {exc}",
                )
                LOGGER.exception("failed to enqueue due schedule")
                break
        return processed

    async def dispatch_once(self) -> bool:
        document = await asyncio.to_thread(self.runtime.claim_outbox, self.worker_id)
        if document is None:
            return False
        try:
            query = ScheduledQuery.model_validate(document.get("payload"))
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(
                self.runtime.fail_outbox,
                document,
                f"Invalid scheduled query payload: {type(exc).__name__}: {exc}",
                False,
            )
            return True
        result = await asyncio.to_thread(self.transport.send, query)
        if result.success:
            await asyncio.to_thread(self.runtime.complete_outbox, document, result.response)
        else:
            await asyncio.to_thread(
                self.runtime.fail_outbox,
                document,
                result.message,
                result.retryable,
            )
        return True

    async def _schedule_loop(self) -> None:
        while not self.stop.is_set():
            try:
                counts = await self.sync_once()
                processed = await self.schedule_once()
                LOGGER.info("schedule sync=%s enqueued=%s", counts, processed)
            except Exception:  # noqa: BLE001
                LOGGER.exception("schedule source synchronization failed")
            await self._wait(self.poll_seconds)

    async def _outbox_loop(self) -> None:
        while not self.stop.is_set():
            try:
                processed = await self.dispatch_once()
            except Exception:  # noqa: BLE001
                LOGGER.exception("outbox dispatch failed")
                processed = False
            if not processed:
                await self._wait(1)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
