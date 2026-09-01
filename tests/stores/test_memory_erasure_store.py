from __future__ import annotations

import pytest

from omnigent.memory import MemoryScope
from omnigent.spec.types import MemoryProviderName
from omnigent.stores.memory_erasure_store import (
    MemoryErasureLeaseLostError,
    MemoryErasureStateError,
    SqlAlchemyMemoryErasureStore,
)


def _request(
    store: SqlAlchemyMemoryErasureStore,
    *,
    providers: tuple[MemoryProviderName, ...] = ("qm-notebook",),
    supported: frozenset[MemoryProviderName] = frozenset({"qm-notebook"}),
):
    return store.create_request(
        workspace_id=7,
        operation_id="erase-request-1",
        requested_by="alice",
        scope=MemoryScope(7, "personal", "alice"),
        provider_names=providers,
        supported_providers=supported,
        requested_at=10_000,
        now=10,
    )


def test_erasure_request_is_idempotent_and_unsupported_provider_blocks(
    db_uri: str,
) -> None:
    store = SqlAlchemyMemoryErasureStore(db_uri)
    request, tasks = _request(
        store,
        providers=("qm-notebook", "hindsight"),
        supported=frozenset({"qm-notebook"}),
    )
    replay, replay_tasks = _request(
        store,
        providers=("qm-notebook", "hindsight"),
        supported=frozenset({"qm-notebook"}),
    )

    assert replay == request
    assert [task.id for task in replay_tasks] == [task.id for task in tasks]
    assert request.status == "blocked"
    assert {task.provider: task.status for task in tasks} == {
        "hindsight": "unsupported",
        "qm-notebook": "pending",
    }
    with pytest.raises(MemoryErasureStateError, match="different context"):
        store.create_request(
            workspace_id=7,
            operation_id="erase-request-1",
            requested_by="bob",
            scope=MemoryScope(7, "personal", "bob"),
            provider_names=("qm-notebook",),
            supported_providers=frozenset({"qm-notebook"}),
            requested_at=10_000,
            now=10,
        )


def test_erasure_completes_only_after_verified_task_and_redacts_scope(db_uri: str) -> None:
    store = SqlAlchemyMemoryErasureStore(db_uri)
    request, [task] = _request(store)
    claimed = store.claim_next(worker_id="worker-a", now=11, lease_seconds=30)
    assert claimed is not None
    assert claimed.id == task.id

    completed = store.complete_task(
        workspace_id=7,
        task_id=task.id,
        worker_id="worker-a",
        attempt_number=claimed.attempt_count,
        receipt={
            "completed_at": 10_000,
            "erased_revisions": 2,
            "scope_hash": "a" * 64,
            "tombstoned_operations": 1,
        },
        verified_at=12,
    )

    assert completed.status == "completed"
    assert completed.verified_at == 12
    current = store.get_request(7, request.id)
    assert current is not None
    assert current.status == "completed"
    assert current.scope_subject is None
    assert current.completed_at == 12
    assert "alice" not in (completed.receipt_json or "")
    [attempt] = store.list_attempts(7, task.id)
    assert attempt.status == "completed"
    assert attempt.error_code is None


def test_erasure_retries_then_dead_letters(db_uri: str) -> None:
    store = SqlAlchemyMemoryErasureStore(db_uri)
    request, [task] = _request(store)
    now = 11

    for expected_attempt in range(1, 6):
        claimed = store.claim_next(worker_id="worker-a", now=now, lease_seconds=30)
        assert claimed is not None
        assert claimed.attempt_count == expected_attempt
        failed = store.fail_task(
            workspace_id=7,
            task_id=task.id,
            worker_id="worker-a",
            attempt_number=claimed.attempt_count,
            error_code="provider_error",
            now=now,
        )
        assert failed.status == ("dead_letter" if expected_attempt == 5 else "retryable")
        now = failed.next_attempt_at

    current = store.get_request(7, request.id)
    assert current is not None
    assert current.status == "blocked"
    assert current.last_error == "provider_dead_letter"
    assert store.claim_next(worker_id="worker-b", now=now, lease_seconds=30) is None


def test_stale_erasure_worker_cannot_complete_reclaimed_task(db_uri: str) -> None:
    store = SqlAlchemyMemoryErasureStore(db_uri)
    _, [task] = _request(store)
    first = store.claim_next(worker_id="worker-a", now=11, lease_seconds=10)
    assert first is not None
    second = store.claim_next(worker_id="worker-b", now=22, lease_seconds=10)
    assert second is not None
    assert second.attempt_count == 2

    with pytest.raises(MemoryErasureLeaseLostError):
        store.complete_task(
            workspace_id=7,
            task_id=task.id,
            worker_id="worker-a",
            attempt_number=1,
            receipt={"scope_hash": "a" * 64},
            verified_at=23,
        )

    attempts = store.list_attempts(7, task.id)
    actual = [(attempt.attempt_number, attempt.status, attempt.error_code) for attempt in attempts]
    assert actual == [
        (1, "failed", "lease_expired"),
        (2, "running", None),
    ]


def test_deferred_erasure_is_unclaimable_until_local_scrub_activates_it(
    db_uri: str,
) -> None:
    store = SqlAlchemyMemoryErasureStore(db_uri)
    request, [task] = store.create_request(
        workspace_id=7,
        operation_id="erase-deferred",
        requested_by="alice",
        scope=MemoryScope(7, "personal", "alice"),
        provider_names=("qm-notebook",),
        supported_providers=frozenset({"qm-notebook"}),
        requested_at=10_000,
        now=10,
        defer=True,
    )

    assert store.claim_next(worker_id="worker", now=11, lease_seconds=30) is None
    activated, [activated_task] = store.activate_request(
        workspace_id=7,
        erasure_id=request.id,
        now=12,
    )
    assert activated.status == "in_progress"
    assert activated_task.id == task.id
    claimed = store.claim_next(worker_id="worker", now=12, lease_seconds=30)
    assert claimed is not None
    assert claimed.id == task.id
