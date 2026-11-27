from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .job_state import INVERSE_INTERRUPTION_STAGES, RUNTIME_STATUSES
from .store import JobStore


class RuntimeExecutionManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def start_once(
        self,
        job_id: str,
        runner: Callable[[], Awaitable[Any]],
    ) -> tuple[asyncio.Task, bool]:
        async with self._lock:
            existing = self._tasks.get(job_id)
            if existing is not None and not existing.done():
                return existing, False
            task = asyncio.create_task(runner())
            self._tasks[job_id] = task
            task.add_done_callback(
                lambda finished, bound_job_id=job_id: self._discard(
                    bound_job_id,
                    finished,
                )
            )
            return task, True

    def get(self, job_id: str) -> asyncio.Task | None:
        task = self._tasks.get(job_id)
        return task if task is not None and not task.done() else None

    def is_active(self, job_id: str) -> bool:
        return self.get(job_id) is not None

    def _discard(self, job_id: str, finished: asyncio.Task) -> None:
        if self._tasks.get(job_id) is finished:
            self._tasks.pop(job_id, None)
        if not finished.cancelled() and finished.exception() is not None:
            # Observe fire-and-forget failures without emitting the raw exception
            # via asyncio's "Task exception was never retrieved" handler.
            logging.getLogger(__name__).error("event=runtime_task_finished status=failed")


def reconcile_interrupted_product_jobs(
    store: JobStore,
    manager: RuntimeExecutionManager,
) -> list[str]:
    changed: list[str] = []
    recoverable = RUNTIME_STATUSES | INVERSE_INTERRUPTION_STAGES
    for job_id in store.list_job_ids():
        if store.get(job_id).get("status") not in recoverable:
            continue
        with store.mutation(job_id):
            job = store.get_consistent(job_id)
            if job["status"] in recoverable and not manager.is_active(job_id):
                store.mark_runtime_interrupted(job_id)
                changed.append(job_id)
    return changed
