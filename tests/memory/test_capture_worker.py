from __future__ import annotations

from collections.abc import Sequence

import pytest

from omnigent.entities import MessageData, NewConversationItem
from omnigent.memory import (
    MemoryCandidate,
    MemoryCaptureReceipt,
    MemoryCaptureRequest,
    MemoryCaptureTarget,
    MemoryScope,
)
from omnigent.memory.capture_worker import (
    MemoryCaptureWorker,
    MemoryExtractionEpisode,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.memory_capture_store import SqlAlchemyMemoryCaptureStore


class Extractor:
    def __init__(self) -> None:
        self.episodes: list[MemoryExtractionEpisode] = []

    async def extract(
        self,
        episode: MemoryExtractionEpisode,
    ) -> Sequence[MemoryCandidate]:
        self.episodes.append(episode)
        return [
            MemoryCandidate(
                kind="preference",
                text="  Prefers   concise answers  ",
                confidence=0.98,
                sensitivity="personal",
                source_item_ids=("fabricated",),
            ),
            MemoryCandidate(
                kind="fact",
                text="api_key=secret",
                confidence=1.0,
                sensitivity="sensitive",
                source_item_ids=("fabricated",),
            ),
        ]


class Provider:
    def __init__(self) -> None:
        self.requests: list[MemoryCaptureRequest] = []

    async def capture(self, request: MemoryCaptureRequest) -> MemoryCaptureReceipt:
        self.requests.append(request)
        return MemoryCaptureReceipt(
            provider="qm-notebook",
            operation_id=request.operation_id,
            scope=request.scope,
            added=len(request.facts),
            revision="4",
            updated_at=request.captured_at,
        )


@pytest.mark.asyncio
async def test_worker_extracts_exact_episode_then_writes_only_after_approval(
    db_uri: str,
) -> None:
    conversation_store = SqlAlchemyConversationStore(db_uri)
    capture_store = SqlAlchemyMemoryCaptureStore(db_uri)
    conversation = conversation_store.create_conversation()
    items = conversation_store.append(
        conversation.id,
        [
            NewConversationItem(
                type="message",
                response_id="turn_input",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "I prefer concise answers"}],
                ),
                created_by="alice",
            ),
            NewConversationItem(
                type="message",
                response_id="resp_target",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "I will keep replies brief."}],
                    agent="agent",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_neighbor",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Neighboring private turn"}],
                    agent="agent",
                ),
            ),
        ],
    )
    intent = capture_store.register_intent(
        workspace_id=0,
        source_item_id=items[0].id,
        conversation_id=conversation.id,
        account_subject="alice",
        targets=(
            MemoryCaptureTarget(
                provider="qm-notebook",
                scope=MemoryScope(0, "personal", "alice"),
                capture_mode="review",
                policy_hash="a" * 64,
            ),
        ),
        now=10,
        expires_at=100,
    )
    [job] = capture_store.complete_intent(
        workspace_id=0,
        intent_id=intent.id,
        source_item_id=items[0].id,
        response_id="resp_target",
        now=11,
    )
    extractor = Extractor()
    provider = Provider()
    worker = MemoryCaptureWorker(
        store=capture_store,
        conversation_store=conversation_store,
        extractor=extractor,
        providers={"qm-notebook": provider},
        worker_id="test-worker",
    )

    assert await worker.run_once(now=12) is True
    assert len(extractor.episodes) == 1
    episode = extractor.episodes[0]
    assert episode.user_text == "I prefer concise answers"
    assert episode.assistant_text == "I will keep replies brief."
    assert "Neighboring private turn" not in episode.assistant_text
    [(review, pending_job)] = capture_store.list_pending_reviews(
        workspace_id=0,
        requested_by="alice",
    )
    assert pending_job.id == job.id
    assert [candidate.text for candidate in review.candidates] == ["Prefers concise answers"]
    assert review.candidates[0].source_item_ids == (items[0].id, items[1].id)
    assert provider.requests == []

    capture_store.decide_review(
        workspace_id=0,
        review_id=review.id,
        decision="approved",
        decided_by="alice",
        reason=None,
        now=13,
    )
    assert await worker.run_once(now=14) is True

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.operation_id == job.operation_id
    assert request.scope == MemoryScope(0, "personal", "alice")
    assert request.facts == ("Prefers concise answers",)
    completed = capture_store.get_job(0, job.id)
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.receipt_json is not None
    assert '"revision":"4"' in completed.receipt_json
