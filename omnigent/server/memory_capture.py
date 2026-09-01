from __future__ import annotations

import httpx

from omnigent.memory.capture_models import MemoryCandidate
from omnigent.memory.capture_worker import (
    MemoryExtractionEpisode,
    MemoryExtractionError,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.server.schemas import BackgroundMemoryExtractionResponse


class RunnerMemoryExtractor:
    def __init__(self, runner_router: RunnerRouter, *, timeout_seconds: float = 70.0) -> None:
        self._runner_router = runner_router
        self._timeout_seconds = timeout_seconds

    async def extract(self, episode: MemoryExtractionEpisode) -> tuple[MemoryCandidate, ...]:
        routed = self._runner_router.client_for_existing_conversation(episode.conversation.id)
        if routed is None:
            raise MemoryExtractionError("session runner is unavailable for memory extraction")
        try:
            response = await routed.client.post(
                f"/v1/sessions/{episode.conversation.id}/background-memory-extraction",
                json={
                    "agent_id": episode.conversation.agent_id,
                    "assistant_text": episode.assistant_text,
                    "harness_override": episode.conversation.harness_override,
                    "model_override": episode.conversation.model_override,
                    "sub_agent_name": episode.conversation.sub_agent_name,
                    "tool_outcomes": episode.tool_outcomes,
                    "user_text": episode.user_text,
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise MemoryExtractionError("runner memory extraction request failed") from exc
        if response.status_code >= 400:
            raise MemoryExtractionError(
                f"runner memory extraction returned HTTP {response.status_code}"
            )
        try:
            payload = BackgroundMemoryExtractionResponse.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise MemoryExtractionError("runner returned an invalid memory extraction") from exc
        if payload.status != "generated":
            raise MemoryExtractionError("session harness does not support memory extraction")
        return tuple(
            MemoryCandidate(
                kind=candidate.kind,
                text=candidate.text,
                confidence=candidate.confidence,
                sensitivity=candidate.sensitivity,
                source_item_ids=(),
            )
            for candidate in payload.candidates
        )
