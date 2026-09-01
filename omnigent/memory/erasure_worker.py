from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Mapping

import httpx
from sqlalchemy.exc import SQLAlchemyError

from omnigent.memory.capture_models import MemoryEraseProvider, MemoryEraseRequest
from omnigent.memory.models import MemoryScope
from omnigent.memory.router import MemoryProviderError
from omnigent.spec.types import MemoryProviderName
from omnigent.stores.memory_erasure_store import (
    MemoryErasureLeaseLostError,
    MemoryErasureStateError,
    SqlAlchemyMemoryErasureStore,
)

_logger = logging.getLogger(__name__)


class MemoryErasureVerificationError(RuntimeError):
    pass


class MemoryErasureWorker:
    def __init__(
        self,
        *,
        store: SqlAlchemyMemoryErasureStore,
        providers: Mapping[MemoryProviderName, MemoryEraseProvider],
        poll_seconds: float = 1.0,
        lease_seconds: int = 120,
        worker_id: str | None = None,
    ) -> None:
        self._store = store
        self._providers = dict(providers)
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._worker_id = worker_id or f"memory-erasure-worker-{uuid.uuid4().hex}"
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name=self._worker_id)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                worked = await self.run_once()
            except (OSError, RuntimeError, SQLAlchemyError) as exc:
                _logger.warning("memory erasure worker poll failed: %s", exc, exc_info=True)
                worked = False
            if worked:
                continue
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)

    async def run_once(self, *, now: int | None = None) -> bool:
        attempt_at = int(time.time()) if now is None else now
        task = await asyncio.to_thread(
            self._store.claim_next,
            worker_id=self._worker_id,
            now=attempt_at,
            lease_seconds=self._lease_seconds,
        )
        if task is None:
            return False
        try:
            erasure = await asyncio.to_thread(
                self._store.get_request,
                task.workspace_id,
                task.erasure_id,
            )
            if erasure is None or not erasure.scope_subject:
                raise MemoryErasureStateError("memory erasure request has no active scope")
            provider = self._providers.get(task.provider)
            if provider is None:
                raise MemoryProviderError(
                    f"memory erasure provider {task.provider!r} is unavailable"
                )
            request = MemoryEraseRequest(
                operation_id=task.operation_id,
                scope=MemoryScope(
                    workspace_id=task.workspace_id,
                    kind=erasure.scope_kind,
                    subject_id=erasure.scope_subject,
                ),
                erased_at=erasure.requested_at,
            )
            receipt = await provider.erase(request)
            if not await provider.verify_erased(request):
                raise MemoryErasureVerificationError(
                    "provider still returned memory after erasure"
                )
            await asyncio.to_thread(
                self._store.complete_task,
                workspace_id=task.workspace_id,
                task_id=task.id,
                worker_id=self._worker_id,
                attempt_number=task.attempt_count,
                receipt={
                    "completed_at": receipt.completed_at,
                    "erased_revisions": receipt.erased_revisions,
                    "operation_id": receipt.operation_id,
                    "provider": receipt.provider,
                    "scope_hash": receipt.scope_hash,
                    "tombstoned_operations": receipt.tombstoned_operations,
                },
                verified_at=attempt_at,
            )
        except (MemoryErasureLeaseLostError, MemoryErasureStateError):
            _logger.info(
                "memory erasure task no longer mutable task=%s attempt=%d",
                task.id,
                task.attempt_count,
            )
        except (
            MemoryErasureVerificationError,
            MemoryProviderError,
            OSError,
            SQLAlchemyError,
            TimeoutError,
            ValueError,
            httpx.HTTPError,
        ) as exc:
            failed = await asyncio.to_thread(
                self._store.fail_task,
                workspace_id=task.workspace_id,
                task_id=task.id,
                worker_id=self._worker_id,
                attempt_number=task.attempt_count,
                error_code=type(exc).__name__,
                now=attempt_at,
            )
            _logger.warning(
                "memory erasure task failed task=%s provider=%s status=%s attempts=%d",
                task.id,
                task.provider,
                failed.status,
                failed.attempt_count,
            )
        return True
