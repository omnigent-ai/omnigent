"""A saved inference profile cannot be silently reassigned to another runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.entities import Conversation
from omnigent.runner import create_runner_app
from omnigent.runner.session_init_protocol import build_runner_session_init_payload
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["same", "different", "unconfigured-session", "unconfigured-runner"]
)
async def test_session_init_requires_the_runners_saved_profile(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "providers": {"gateway": {"kind": "gateway"}},
        "inference": {"harnesses": {"codex": {"provider": "gateway"}}},
    }
    runner_config = config if mode != "unconfigured-runner" else {}
    expected = config if mode != "unconfigured-session" else None
    if mode == "different":
        runner_config = {"providers": {"other": {"kind": "gateway"}}, "inference": {}}
    config_path = tmp_path / "inference.json"
    config_path.write_text(json.dumps(runner_config))
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(config_path))
    conversation = Conversation(
        id="inference-session",
        created_at=1,
        updated_at=1,
        agent_id="inference-agent",
        root_conversation_id="inference-session",
        inference_snapshot={"runtime_config": expected} if expected is not None else None,
    )
    payload = build_runner_session_init_payload(conversation, server_version="0.6.0")
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        response = await client.post("/v1/sessions", json=payload)
    if mode == "same":
        assert response.status_code == 201, response.text
        assert response.json()["inference_config_verified"] is True
        assert len(manager.get_client_calls) == 1
    else:
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "inference_config_mismatch"
        assert manager.get_client_calls == []


@pytest.mark.asyncio
async def test_bound_runner_rejects_legacy_init_without_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "inference.json"
    config_path.write_text(json.dumps({"providers": {"gateway": {"kind": "gateway"}}}))
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(config_path))
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions", json={"session_id": "legacy-session", "agent_id": "agent"}
        )
    assert response.status_code == 409, response.text
    assert manager.get_client_calls == []
