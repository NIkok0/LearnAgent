from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from copilot_agent.runtime.checkpoint_store import CheckpointStore
from copilot_agent.runtime.event_store import EventStore
from copilot_agent.runtime.thread_checkpoint import archive_thread_and_purge_checkpoint, purge_thread_checkpoint

log = logging.getLogger(__name__)


class ThreadLifecycleCleaner:
    """Move idle active threads to ended, then archive old ended threads."""

    def __init__(
        self,
        *,
        event_store: EventStore,
        active_idle_ttl_seconds: int,
        ended_archive_ttl_seconds: int,
        interval_seconds: int = 60,
        batch_size: int = 100,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.event_store = event_store
        self.checkpoint_store = checkpoint_store
        self.active_idle_ttl_seconds = max(0, active_idle_ttl_seconds)
        self.ended_archive_ttl_seconds = max(0, ended_archive_ttl_seconds)
        self.interval_seconds = max(1, interval_seconds)
        self.batch_size = max(1, batch_size)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="thread-lifecycle-cleaner")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def run_once(self) -> dict[str, list[dict[str, object]]]:
        ended = self.event_store.end_idle_threads_older_than(
            self.active_idle_ttl_seconds,
            limit=self.batch_size,
        )
        archived = self.event_store.archive_ended_threads_older_than(
            self.ended_archive_ttl_seconds,
            limit=self.batch_size,
        )
        if self.checkpoint_store is not None:
            for thread in archived:
                thread_id = str(thread.get("id", ""))
                if thread_id:
                    purge_thread_checkpoint(self.event_store, self.checkpoint_store, thread_id)
        if ended:
            log.info("Ended %d idle thread(s)", len(ended))
        if archived:
            log.info("Archived %d ended thread(s)", len(archived))
        return {"ended": ended, "archived": archived}

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("Thread lifecycle cleanup failed")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue
