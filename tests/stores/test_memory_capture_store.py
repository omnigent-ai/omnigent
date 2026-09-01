from __future__ import annotations

import pytest

from omnigent.memory import MemoryCandidate, MemoryCaptureTarget, MemoryScope
from omnigent.stores.memory_capture_store import (
    MemoryCaptureLeaseLostError,
    SqlAlchemyMemoryCaptureStore,
)


def _target() -> MemoryCaptureTarget:
    return MemoryCaptureTarget(
        provider="qm-notebook",
        scope=MemoryScope(7, "personal", "alice"),
        capture_mode="review",
        policy_hash="a" * 64,
    )


def _completed_job(store: SqlAlchemyMemoryCaptureStore):
    intent = store.register_intent(
        workspace_id=7,
        source_item_id="1" * 32,
        conversation_id="2" * 32,
        account_subject="alice",
        targets=(_target(),),
        now=100,
        expires_at=1000,
    )
    jobs = store.complete_intent(
        workspace_id=7,
        intent_id=intent.id,
        source_item_id=intent.source_item_id,
        response_id="resp_1",
        now=110,
    )
    assert len(jobs) == 1
    return intent, jobs[0]


def test_turn_completion_is_idempotent(db_uri: str) -> None:
    store = SqlAlchemyMemoryCaptureStore(db_uri)

    first_intent, first_job = _completed_job(store)
    second_intent = store.register_intent(
        workspace_id=7,
        source_item_id="1" * 32,
        conversation_id="2" * 32,
        account_subject="alice",
        targets=(_target(),),
        now=999,
        expires_at=9999,
    )
    replayed = store.complete_intent(
        workspace_id=7,
        intent_id=first_intent.id,
        source_item_id=first_intent.source_item_id,
        response_id="resp_1",
        now=120,
    )

    assert second_intent.id == first_intent.id
    assert second_intent.targets_hash == first_intent.targets_hash
    assert second_intent.status == "completed"
    assert second_intent.response_id == "resp_1"
    assert [job.id for job in replayed] == [first_job.id]
    assert replayed[0].operation_id == first_job.operation_id


def test_review_approval_releases_exact_candidates_to_write(db_uri: str) -> None:
    store = SqlAlchemyMemoryCaptureStore(db_uri)
    _, queued = _completed_job(store)

    extracting = store.claim_next(worker_id="worker-a", now=120, lease_seconds=30)
    assert extracting is not None
    assert extracting.id == queued.id
    assert extracting.phase == "extraction"
    candidate = MemoryCandidate(
        kind="preference",
        text="Prefers concise answers",
        confidence=0.95,
        sensitivity="personal",
        source_item_ids=("1" * 32,),
    )
    review = store.complete_extraction(
        workspace_id=7,
        job_id=extracting.id,
        worker_id="worker-a",
        attempt_number=extracting.attempt_count,
        candidates=(candidate,),
        now=121,
    )
    assert review is not None
    assert review.candidates == (candidate,)
    assert store.claim_next(worker_id="worker-a", now=122, lease_seconds=30) is None

    approved, ready = store.decide_review(
        workspace_id=7,
        review_id=review.id,
        decision="approved",
        decided_by="alice",
        reason="Accurate preference",
        now=123,
    )
    assert approved.status == "approved"
    assert ready.phase == "write"
    writing = store.claim_next(worker_id="worker-b", now=123, lease_seconds=30)
    assert writing is not None
    assert writing.id == queued.id
    assert writing.operation_id == queued.operation_id

    completed = store.complete_write(
        workspace_id=7,
        job_id=writing.id,
        worker_id="worker-b",
        attempt_number=writing.attempt_count,
        receipt={"added": 1, "revision": "rev-1", "updatedAt": 124},
        now=124,
    )
    assert completed.status == "succeeded"
    assert completed.finished_at == 124
    attempts = store.list_attempts(7, queued.id)
    assert [(attempt.phase, attempt.status) for attempt in attempts] == [
        ("extraction", "succeeded"),
        ("write", "succeeded"),
    ]


def test_failures_retry_with_backoff_then_dead_letter(db_uri: str) -> None:
    store = SqlAlchemyMemoryCaptureStore(db_uri)
    _, queued = _completed_job(store)
    now = 120

    for expected_attempt in range(1, 6):
        claimed = store.claim_next(worker_id="worker-a", now=now, lease_seconds=30)
        assert claimed is not None
        assert claimed.attempt_count == expected_attempt
        failed = store.fail_job(
            workspace_id=7,
            job_id=claimed.id,
            worker_id="worker-a",
            attempt_number=claimed.attempt_count,
            error=f"failure {expected_attempt}",
            now=now,
        )
        expected_status = "dead_letter" if expected_attempt == 5 else "retryable"
        assert failed.status == expected_status
        now = failed.next_attempt_at

    assert store.claim_next(worker_id="worker-a", now=now, lease_seconds=30) is None
    attempts = store.list_attempts(7, queued.id)
    assert len(attempts) == 5
    assert all(attempt.status == "failed" for attempt in attempts)
    assert attempts[-1].error == "failure 5"


def test_expired_worker_cannot_complete_reclaimed_job(db_uri: str) -> None:
    store = SqlAlchemyMemoryCaptureStore(db_uri)
    _, queued = _completed_job(store)
    first = store.claim_next(worker_id="worker-a", now=120, lease_seconds=10)
    assert first is not None
    second = store.claim_next(worker_id="worker-b", now=131, lease_seconds=10)
    assert second is not None
    assert second.id == queued.id
    assert second.attempt_count == 2

    with pytest.raises(MemoryCaptureLeaseLostError):
        store.complete_extraction(
            workspace_id=7,
            job_id=queued.id,
            worker_id="worker-a",
            attempt_number=1,
            candidates=(),
            now=132,
        )

    current = store.get_job(7, queued.id)
    assert current is not None
    assert current.status == "processing"
    assert current.lease_owner == "worker-b"
    attempts = store.list_attempts(7, queued.id)
    assert [(attempt.attempt_number, attempt.status, attempt.error) for attempt in attempts] == [
        (1, "failed", "worker lease expired"),
        (2, "running", None),
    ]


def test_expired_intent_is_cancelled_and_cannot_create_jobs(db_uri: str) -> None:
    store = SqlAlchemyMemoryCaptureStore(db_uri)
    intent = store.register_intent(
        workspace_id=7,
        source_item_id="3" * 32,
        conversation_id="4" * 32,
        account_subject="alice",
        targets=(_target(),),
        now=10,
        expires_at=20,
    )

    assert store.expire_intents(19) == 0
    assert store.expire_intents(20) == 1
    cancelled = store.get_intent(7, intent.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert (
        store.complete_intent(
            workspace_id=7,
            intent_id=intent.id,
            source_item_id=intent.source_item_id,
            response_id="resp_late",
            now=21,
        )
        == []
    )


def test_personal_target_must_match_workspace_and_account(db_uri: str) -> None:
    store = SqlAlchemyMemoryCaptureStore(db_uri)

    with pytest.raises(ValueError, match="workspace"):
        store.register_intent(
            workspace_id=7,
            source_item_id="5" * 32,
            conversation_id="6" * 32,
            account_subject="alice",
            targets=(
                MemoryCaptureTarget(
                    provider="qm-notebook",
                    scope=MemoryScope(8, "personal", "alice"),
                    capture_mode="review",
                    policy_hash="a" * 64,
                ),
            ),
            now=10,
            expires_at=20,
        )

    with pytest.raises(ValueError, match="account subject"):
        store.register_intent(
            workspace_id=7,
            source_item_id="7" * 32,
            conversation_id="8" * 32,
            account_subject="alice",
            targets=(
                MemoryCaptureTarget(
                    provider="qm-notebook",
                    scope=MemoryScope(7, "personal", "bob"),
                    capture_mode="review",
                    policy_hash="a" * 64,
                ),
            ),
            now=10,
            expires_at=20,
        )
