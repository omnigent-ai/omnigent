from __future__ import annotations

from omnigent.memory import (
    MemoryEraseReceipt,
    MemoryEraseRequest,
    MemoryScope,
)
from omnigent.memory.erasure_worker import MemoryErasureWorker
from omnigent.stores.memory_erasure_store import SqlAlchemyMemoryErasureStore


class Provider:
    def __init__(self, verifications: list[bool]) -> None:
        self._verifications = verifications
        self.erase_requests: list[MemoryEraseRequest] = []
        self.verify_requests: list[MemoryEraseRequest] = []

    async def erase(self, request: MemoryEraseRequest) -> MemoryEraseReceipt:
        self.erase_requests.append(request)
        return MemoryEraseReceipt(
            provider="qm-notebook",
            operation_id=request.operation_id,
            scope_hash="a" * 64,
            erased_revisions=2,
            tombstoned_operations=1,
            completed_at=request.erased_at,
        )

    async def verify_erased(self, request: MemoryEraseRequest) -> bool:
        self.verify_requests.append(request)
        return self._verifications.pop(0)


async def test_erasure_worker_retries_unverified_delete_with_stable_operation(
    db_uri: str,
) -> None:
    store = SqlAlchemyMemoryErasureStore(db_uri)
    erasure, [task] = store.create_request(
        workspace_id=7,
        operation_id="erase-request-1",
        requested_by="alice",
        scope=MemoryScope(7, "personal", "alice"),
        provider_names=("qm-notebook",),
        supported_providers=frozenset({"qm-notebook"}),
        requested_at=10_000,
        now=10,
    )
    provider = Provider([False, True])
    worker = MemoryErasureWorker(
        store=store,
        providers={"qm-notebook": provider},
        worker_id="test-worker",
    )

    assert await worker.run_once(now=11) is True
    pending = store.get_request(7, erasure.id)
    assert pending is not None
    assert pending.status == "in_progress"
    [retryable] = store.list_tasks(7, erasure.id)
    assert retryable.status == "retryable"

    assert await worker.run_once(now=retryable.next_attempt_at) is True
    completed = store.get_request(7, erasure.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.scope_subject is None
    [completed_task] = store.list_tasks(7, erasure.id)
    assert completed_task.status == "completed"
    assert completed_task.verified_at == retryable.next_attempt_at
    assert completed_task.receipt_json is not None
    assert "alice" not in completed_task.receipt_json

    assert len(provider.erase_requests) == 2
    assert provider.erase_requests[0] == provider.erase_requests[1]
    assert provider.erase_requests[0].operation_id == task.operation_id
    assert provider.erase_requests[0].erased_at == 10_000
    assert provider.verify_requests == provider.erase_requests
    attempts = store.list_attempts(7, task.id)
    assert [(attempt.status, attempt.error_code) for attempt in attempts] == [
        ("failed", "MemoryErasureVerificationError"),
        ("completed", None),
    ]
