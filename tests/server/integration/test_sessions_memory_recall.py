from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT
from omnigent.memory import (
    MemoryCaptureReceipt,
    MemoryCaptureRequest,
    MemoryRecallProvider,
    MemoryRecallRequest,
    MemoryRuntime,
    MemoryScope,
    MemoryTurnContext,
    PreparedTurnMemory,
    RetrievalResult,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.spec.types import (
    AgentSpec,
    MemoryProviderName,
    MemoryProviderSpec,
    MemoryRolloutMode,
    MemorySpec,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.helpers import create_test_agent


class _RecordingProvider:
    name: MemoryProviderName = "qm-notebook"

    def __init__(self) -> None:
        self.requests: list[MemoryRecallRequest] = []

    async def recall(self, request: MemoryRecallRequest) -> Sequence[RetrievalResult]:
        self.requests.append(request)
        return [
            RetrievalResult(
                provider=self.name,
                scope=request.scopes[0],
                text="Prefers concise answers",
            )
        ]

    async def capture(self, request: MemoryCaptureRequest) -> MemoryCaptureReceipt:
        return MemoryCaptureReceipt(
            provider=self.name,
            operation_id=request.operation_id,
            scope=request.scope,
            added=len(request.facts),
            revision="1",
            updated_at=request.captured_at,
        )


class _RecordingMemoryRuntime(MemoryRuntime):
    def __init__(
        self,
        providers: Mapping[MemoryProviderName, MemoryRecallProvider],
    ) -> None:
        super().__init__(providers)
        self.injection_support: list[bool] = []
        self.capture_support: list[bool] = []

    async def prepare(
        self,
        spec: AgentSpec,
        context: MemoryTurnContext,
        *,
        injection_supported: bool,
        capture_supported: bool = True,
    ) -> PreparedTurnMemory:
        self.injection_support.append(injection_supported)
        self.capture_support.append(capture_supported)
        return await super().prepare(
            spec,
            context,
            injection_supported=injection_supported,
            capture_supported=capture_supported,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "native", "erased", "expect_instruction", "expect_capture"),
    [
        ("shadow", False, False, False, False),
        ("read_only", False, False, True, False),
        ("read_only", True, False, False, False),
        ("automatic_capture", False, False, True, True),
        ("automatic_capture", True, False, False, False),
        ("automatic_capture", False, True, False, False),
    ],
)
async def test_authenticated_recall_respects_rollout_and_harness_support(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: MemoryRolloutMode,
    native: bool,
    erased: bool,
    expect_instruction: bool,
    expect_capture: bool,
) -> None:
    provider = _RecordingProvider()
    providers: dict[MemoryProviderName, MemoryRecallProvider] = {
        provider.name: provider,
    }
    memory_runtime = _RecordingMemoryRuntime(providers)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_cache = AgentCache(
        artifact_store=artifact_store,
        cache_dir=tmp_path / "cache",
    )
    app = create_app(
        agent_store=agent_store,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        feature_flags=FeatureFlags(frozenset({Feature.MEMORY_RUNTIME})),
        memory_runtime=memory_runtime,
    )
    headers = {"X-Forwarded-Email": "alice@example.com"}
    captured: list[tuple[str, dict[str, Any]]] = []

    def runner_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        captured.append((request.url.path, payload))
        return httpx.Response(202, json={})

    runner_client = httpx.AsyncClient(
        transport=httpx.MockTransport(runner_handler),
        base_url="http://runner",
    )

    async def get_runner_client(*_: Any, **__: Any) -> httpx.AsyncClient:
        return runner_client

    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_get_runner_client", get_runner_client)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            agent = await create_test_agent(
                client,
                name=f"memory-{mode}-{native}",
                user=headers["X-Forwarded-Email"],
            )
            agent_record = agent_store.get(agent["id"])
            assert agent_record is not None
            assert agent_record.bundle_location is not None
            loaded_agent = agent_cache.load(agent["id"], agent_record.bundle_location)
            loaded_agent.spec.memory = MemorySpec(
                mode=mode,
                providers=[
                    MemoryProviderSpec(
                        provider="qm-notebook",
                        scopes=["personal"],
                        recall="ambient",
                        capture="review" if mode == "automatic_capture" else "off",
                    )
                ],
            )
            session_id = agent["_session_id"]
            if native:
                patch_response = await client.patch(
                    f"/v1/sessions/{session_id}",
                    json={
                        "labels": {
                            "omnigent.wrapper": CLAUDE_NATIVE_CODING_AGENT.wrapper_label,
                        }
                    },
                    headers=headers,
                )
                assert patch_response.status_code == 200, patch_response.text
            if erased:
                app.state.memory_erasure_store.create_request(
                    workspace_id=0,
                    operation_id="erase-before-turn",
                    requested_by="alice@example.com",
                    scope=MemoryScope(0, "personal", "alice@example.com"),
                    provider_names=("qm-notebook",),
                    supported_providers=frozenset(),
                    requested_at=1,
                    now=1,
                )

            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "How should you answer?"}],
                    },
                },
                headers=headers,
            )
            assert response.status_code == 202, response.text
    finally:
        await runner_client.aclose()

    if erased:
        assert memory_runtime.injection_support == []
        assert memory_runtime.capture_support == []
        assert provider.requests == []
    else:
        assert memory_runtime.injection_support == [not native]
        assert memory_runtime.capture_support == [not native]
        assert len(provider.requests) == 1
    if not provider.requests:
        event_payloads = [
            payload for path, payload in captured if path == f"/v1/sessions/{session_id}/events"
        ]
        assert len(event_payloads) == 1
        assert "framework_instructions" not in event_payloads[0]
        assert "memory_capture" not in event_payloads[0]
        return
    recall_request = provider.requests[0]
    assert recall_request.context.account_subject == "alice@example.com"
    assert recall_request.context.conversation_id == session_id
    assert recall_request.context.workspace_id == 0
    assert recall_request.context.query == "How should you answer?"
    assert recall_request.context.operation_id.startswith("memory_")
    assert len(recall_request.scopes) == 1
    assert recall_request.scopes[0].kind == "personal"
    assert recall_request.scopes[0].subject_id == "alice@example.com"

    event_payloads = [
        payload for path, payload in captured if path == f"/v1/sessions/{session_id}/events"
    ]
    assert len(event_payloads) == 1
    if expect_instruction:
        instruction = event_payloads[0]["framework_instructions"]
        assert "Prefers concise answers" in instruction
        assert "untrusted reference data" in instruction
    else:
        assert "framework_instructions" not in event_payloads[0]
    if expect_capture:
        correlation = event_payloads[0]["memory_capture"]
        assert correlation["conversation_id"] == session_id
        assert correlation["source_item_id"] == event_payloads[0]["persisted_item_id"]
        assert correlation["workspace_id"] == 0
        assert isinstance(correlation["intent_id"], str)
    else:
        assert "memory_capture" not in event_payloads[0]
