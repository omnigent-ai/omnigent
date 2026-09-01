from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import httpx

from omnigent.memory import (
    MemoryCandidate,
    MemoryCaptureTarget,
    MemoryEraseReceipt,
    MemoryEraseRequest,
    MemoryRecallRequest,
    MemoryRuntime,
    MemoryScope,
    RetrievalResult,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import FeatureFlags
from omnigent.spec.types import MemoryProviderName
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.memory_capture_store import SqlAlchemyMemoryCaptureStore
from omnigent.stores.memory_erasure_store import SqlAlchemyMemoryErasureStore


class Provider:
    name: MemoryProviderName = "qm-notebook"

    def __init__(self) -> None:
        self.erase_requests: list[MemoryEraseRequest] = []

    async def recall(self, request: MemoryRecallRequest) -> Sequence[RetrievalResult]:
        return []

    async def erase(self, request: MemoryEraseRequest) -> MemoryEraseReceipt:
        self.erase_requests.append(request)
        return MemoryEraseReceipt(
            provider=self.name,
            operation_id=request.operation_id,
            scope_hash="a" * 64,
            erased_revisions=2,
            tombstoned_operations=1,
            completed_at=request.erased_at,
        )

    async def verify_erased(self, request: MemoryEraseRequest) -> bool:
        return request == self.erase_requests[-1]


def _seed_review(store: SqlAlchemyMemoryCaptureStore) -> tuple[str, str]:
    intent = store.register_intent(
        workspace_id=0,
        source_item_id="1" * 32,
        conversation_id="2" * 32,
        account_subject="alice@example.com",
        targets=(
            MemoryCaptureTarget(
                provider="qm-notebook",
                scope=MemoryScope(0, "personal", "alice@example.com"),
                capture_mode="review",
                policy_hash="a" * 64,
            ),
        ),
        now=10,
        expires_at=100,
    )
    [job] = store.complete_intent(
        workspace_id=0,
        intent_id=intent.id,
        source_item_id=intent.source_item_id,
        response_id="resp_capture",
        now=11,
    )
    claimed = store.claim_next(worker_id="capture-worker", now=12, lease_seconds=30)
    assert claimed is not None
    review = store.complete_extraction(
        workspace_id=0,
        job_id=job.id,
        worker_id="capture-worker",
        attempt_number=claimed.attempt_count,
        candidates=(
            MemoryCandidate(
                kind="preference",
                text="Prefers concise answers",
                confidence=0.98,
                sensitivity="personal",
                source_item_ids=(intent.source_item_id,),
            ),
        ),
        now=13,
    )
    assert review is not None
    return job.id, review.id


async def test_personal_erasure_scrubs_local_candidates_and_verifies_provider(
    db_uri: str,
    tmp_path: Path,
) -> None:
    capture_store = SqlAlchemyMemoryCaptureStore(db_uri)
    erasure_store = SqlAlchemyMemoryErasureStore(db_uri)
    job_id, review_id = _seed_review(capture_store)
    provider = Provider()
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        feature_flags=FeatureFlags(),
        memory_runtime=MemoryRuntime({"qm-notebook": provider}),
        memory_capture_store=capture_store,
        memory_erasure_store=erasure_store,
    )
    assert app.state.memory_capture_worker is None
    erasure_worker = app.state.memory_erasure_worker
    assert erasure_worker is not None
    transport = httpx.ASGITransport(app=app)
    alice = {"X-Forwarded-Email": "alice@example.com"}
    bob = {"X-Forwarded-Email": "bob@example.com"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/memory/erasures",
            json={"operation_id": "erase-alice-memory"},
            headers=alice,
        )
        assert created.status_code == 202, created.text
        created_body = created.json()
        assert created_body["scope"] == "personal"
        assert created_body["status"] == "in_progress"
        assert "alice@example.com" not in json.dumps(created_body)

        scrubbed_job = capture_store.get_job(0, job_id)
        scrubbed_review = capture_store.get_review(0, review_id)
        assert scrubbed_job is not None
        assert scrubbed_job.status == "cancelled"
        assert scrubbed_job.scope.subject_id == "__erased__"
        assert scrubbed_review is not None
        assert scrubbed_review.status == "cancelled"
        assert scrubbed_review.candidates == ()

        hidden = await client.get(
            f"/v1/memory/erasures/{created_body['id']}",
            headers=bob,
        )
        assert hidden.status_code == 404

        [pending_task] = erasure_store.list_tasks(0, created_body["id"])
        assert await erasure_worker.run_once(now=pending_task.next_attempt_at) is True
        completed = await client.get(
            f"/v1/memory/erasures/{created_body['id']}",
            headers=alice,
        )
        assert completed.status_code == 200
        completed_body = completed.json()
        assert completed_body["status"] == "completed"
        assert completed_body["providers"][0]["status"] == "completed"
        assert completed_body["providers"][0]["verified_at"] == (pending_task.next_attempt_at)
        assert "alice@example.com" not in json.dumps(completed_body)

        replay = await client.post(
            "/v1/memory/erasures",
            json={"operation_id": "erase-alice-memory"},
            headers=alice,
        )
        assert replay.status_code == 202
        assert replay.json()["id"] == created_body["id"]
        assert replay.json()["status"] == "completed"

    assert len(provider.erase_requests) == 1
