from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.memory import MemoryCandidate, MemoryCaptureTarget, MemoryScope
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.memory_capture_store import SqlAlchemyMemoryCaptureStore


def _pending_review(
    store: SqlAlchemyMemoryCaptureStore,
    *,
    source_item_id: str,
    subject: str,
    scope: MemoryScope,
) -> str:
    intent = store.register_intent(
        workspace_id=0,
        source_item_id=source_item_id,
        conversation_id="f" * 32,
        account_subject=subject,
        targets=(
            MemoryCaptureTarget(
                provider="qm-notebook",
                scope=scope,
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
        source_item_id=source_item_id,
        response_id=f"resp_{source_item_id[:8]}",
        now=11,
    )
    claimed = store.claim_next(worker_id="test", now=12, lease_seconds=30)
    assert claimed is not None
    review = store.complete_extraction(
        workspace_id=0,
        job_id=job.id,
        worker_id="test",
        attempt_number=claimed.attempt_count,
        candidates=(
            MemoryCandidate(
                kind="preference",
                text="Prefers concise answers",
                confidence=0.98,
                sensitivity="personal",
                source_item_ids=(source_item_id,),
            ),
        ),
        now=13,
    )
    assert review is not None
    return review.id


@pytest.mark.asyncio
async def test_memory_reviews_are_personal_and_decisions_are_terminal(
    db_uri: str,
    tmp_path: Path,
) -> None:
    capture_store = SqlAlchemyMemoryCaptureStore(db_uri)
    alice_review = _pending_review(
        capture_store,
        source_item_id="1" * 32,
        subject="alice@example.com",
        scope=MemoryScope(0, "personal", "alice@example.com"),
    )
    org_review = _pending_review(
        capture_store,
        source_item_id="2" * 32,
        subject="alice@example.com",
        scope=MemoryScope(0, "org"),
    )
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
        memory_capture_store=capture_store,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        alice = {"X-Forwarded-Email": "alice@example.com"}
        bob = {"X-Forwarded-Email": "bob@example.com"}

        listed = await client.get("/v1/memory/reviews", headers=alice)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["data"]] == [alice_review]
        assert listed.json()["data"][0]["scope"] == "personal"
        assert "alice" not in listed.json()["data"][0]["scope"]

        hidden = await client.patch(
            f"/v1/memory/reviews/{alice_review}",
            json={"action": "approve"},
            headers=bob,
        )
        assert hidden.status_code == 404

        org_hidden = await client.patch(
            f"/v1/memory/reviews/{org_review}",
            json={"action": "approve"},
            headers=alice,
        )
        assert org_hidden.status_code == 404

        approved = await client.patch(
            f"/v1/memory/reviews/{alice_review}",
            json={"action": "approve", "reason": "Accurate preference"},
            headers=alice,
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        repeated = await client.patch(
            f"/v1/memory/reviews/{alice_review}",
            json={"action": "reject"},
            headers=alice,
        )
        assert repeated.status_code == 409
