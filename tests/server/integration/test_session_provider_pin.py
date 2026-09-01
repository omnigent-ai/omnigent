"""HTTP creation carries an exact Codex provider pin through native launch."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import omnigent.codex_native_app_server as app_server
from omnigent.runner.session_init_protocol import (
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def test_http_create_persists_pin_in_snapshot_and_native_launch(
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await create_test_agent(
        client,
        name="pinned-codex",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
        include_llm=False,
    )
    provider_name = "Work account / 東京 --model"

    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "provider_override": provider_name},
    )

    assert response.status_code == 201, response.text
    assert response.json()["provider_override"] == provider_name
    conversation = SqlAlchemyConversationStore(db_uri).get_conversation(response.json()["id"])
    assert conversation is not None
    assert conversation.provider_override == provider_name

    payload = build_runner_session_init_payload(conversation, server_version="test")
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.snapshot.provider_override == provider_name

    work_home = tmp_path / "codex-work"
    work_home.mkdir()
    (work_home / "auth.json").write_text('{"OPENAI_API_KEY":"test-only"}')
    config = {
        "providers": {
            provider_name: {
                "kind": "subscription",
                "cli": "codex",
                "cli_home": str(work_home),
            }
        }
    }
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", lambda: config)

    launch = app_server.resolve_native_codex_launch(
        model=None,
        provider_name=envelope.snapshot.provider_override,
    )

    assert launch.cli_home == work_home
    assert launch.login_required is False
    assert provider_name in launch.summary


async def test_http_create_rejects_pin_for_non_codex_agent(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(client)

    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "provider_override": "work"},
    )

    assert response.status_code == 400
    assert "only to codex-native" in response.text


async def test_http_create_rejects_empty_pin_for_codex_agent(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(
        client,
        name="empty-pin-codex",
        executor={"type": "omnigent", "config": {"harness": "codex-native"}},
        include_llm=False,
    )

    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "provider_override": ""},
    )

    assert response.status_code == 400
    assert "must not be empty" in response.text
