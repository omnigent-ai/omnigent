"""Real Claude routing and prompt delivery with an isolated mock judge/provider."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_local_server
from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_requests
from tests.e2e.routing._helpers import claude_settings_snapshot, decisions
from tests.e2e_ui.chat.test_claude_model_picker import (
    _PINNED_CLAUDE_MODELS,
    _assert_mock_claude_turn,
    _assert_native_claude_turn,
    _live_claude_snapshot,
    _native_claude_wire_model,
    _require_session_only_claude,
)
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _send,
    _turn_prompt,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _ensure_chat_view,
    _open_terminal_view,
    _pane_text,
    _type_into_tui,
    _wait_for_transcript_message,
    _wait_terminal_connected,
)
from tests.helpers.ui_configuration import validate_native_claude_mock_routing

pytestmark = [pytest.mark.nightly, pytest.mark.timeout(420)]

_JUDGE_MODEL = "mock-claude-routing-judge"
_TARGET_MODEL = _PINNED_CLAUDE_MODELS["opus"]


@pytest.fixture
def claude_mock_routing_stack(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
    claude_managed_settings: None,
) -> Iterator[tuple[str, httpx.Client, str, Path, Path]]:
    """Own both runtimes and give the judge and native CLI only loopback endpoints."""
    validate_native_claude_mock_routing(mock_llm_server_url)
    reset_mock_llm(mock_llm_server_url)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude_config = tmp_path / "claude-config"
    claude_config.mkdir()
    settings_path = claude_config / "settings.json"
    settings_path.write_text(json.dumps({"model": _PINNED_CLAUDE_MODELS["sonnet"]}))
    config = tmp_path / "server.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": _JUDGE_MODEL,
                    "connection": {
                        "base_url": f"{mock_llm_server_url}/v1",
                        "api_key": "mock-key",
                    },
                }
            }
        )
    )
    providers = {
        "pinned-claude": {
            "kind": "key",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": mock_llm_server_url,
                "api_key": "mock-key",
                "models": {"default": _PINNED_CLAUDE_MODELS["sonnet"], **_PINNED_CLAUDE_MODELS},
            },
        }
    }
    mock_env = {
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_BASE_URL": mock_llm_server_url,
        "ANTHROPIC_AUTH_TOKEN": None,
        "CLAUDE_CODE_OAUTH_TOKEN": None,
        "CLAUDECODE": None,
    }
    with (
        isolated_local_server(
            tmp_path / "server",
            extra_args=["--config", str(config)],
            env={**mock_env, "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT)},
        ) as base_url,
        httpx.Client(base_url=base_url, timeout=30, trust_env=False) as client,
        isolated_host(
            tmp_path / "host",
            base_url,
            config={"providers": providers},
            env={
                **mock_env,
                "CLAUDE_CONFIG_DIR": str(claude_config),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
            cwd=workspace,
        ) as host,
    ):
        host.wait_online(client, timeout=120, poll_interval=0.5)
        yield base_url, client, host.host_id, workspace, settings_path


@pytest.mark.parametrize("entry", ["create", "chat", "terminal"])
def test_claude_routing_preserves_default_and_delivers_once(
    page: Page,
    claude_mock_routing_stack: tuple[str, httpx.Client, str, Path, Path],
    mock_llm_server_url: str,
    tmp_path: Path,
    entry: str,
) -> None:
    """Create, composer and first-typed routing preserve a distinct saved default."""
    base_url, client, host_id, workspace, settings_path = claude_mock_routing_stack
    saved_settings = claude_settings_snapshot(settings_path)
    verdict = json.dumps(
        {
            "harness": "claude-native",
            "model": _TARGET_MODEL,
            "rationale": "Use the exact configured Opus model for this complex task.",
        }
    )
    set_fallback_mock_llm(mock_llm_server_url, _JUDGE_MODEL, verdict)
    nonce = uuid.uuid4().hex[:8]
    turns = [(f"route-user-{index}-{nonce}", f"route-reply-{index}-{nonce}") for index in (1, 2)]
    first_prompt = _turn_prompt(1, *turns[0])
    agents = client.get("/v1/agents")
    agents.raise_for_status()
    agent_id = next(
        (row["id"] for row in agents.json()["data"] if row["name"] == "claude-native-ui"),
        None,
    )
    assert agent_id is not None, "The server did not register its native Claude wrapper"
    create_body = {
        "agent_id": agent_id,
        "host_id": host_id,
        "workspace": str(workspace),
        "cost_control_mode_override": "on",
    }
    if entry == "create":
        create_body["smart_routing_message"] = first_prompt
    created = client.post("/v1/sessions", json=create_body, timeout=120)
    created.raise_for_status()
    session_id = str(created.json()["id"])
    try:
        page.set_viewport_size({"width": 1800, "height": 900})
        page.goto(f"{base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        _require_session_only_claude(base_url, session_id)
        assert _live_claude_snapshot(base_url, session_id)["cost_control_mode_override"] == "on"
        if entry != "create":
            poll_until(
                lambda: (
                    bool(model := _live_claude_snapshot(base_url, session_id).get("llm_model"))
                    and model != _native_claude_wire_model(_TARGET_MODEL)
                ),
                timeout_s=60,
            )
            assert decisions(client, session_id) == []
        assert claude_settings_snapshot(settings_path) == saved_settings

        native_sessions = []
        for index, (marker, reply) in enumerate(turns, start=1):
            prompt = _turn_prompt(index, marker, reply)
            set_fallback_mock_llm(mock_llm_server_url, "default", reply)
            set_fallback_mock_llm(
                mock_llm_server_url, _native_claude_wire_model(_TARGET_MODEL), reply
            )
            if entry == "terminal":
                _open_terminal_view(page)
                _wait_terminal_connected(page)
                _type_into_tui(page, prompt)
            else:
                _ensure_chat_view(page)
                _send(page, prompt)
            _wait_for_transcript_message(page, base_url, session_id, reply, role="assistant")
            (tmp_path / f"routing-{entry}-turn-{index}.txt").write_text(
                _pane_text(base_url, session_id)
            )
            _ensure_chat_view(page)
            expect(page.locator(_USER, has_text=marker)).to_have_count(1)
            expect(page.locator(_ASSISTANT, has_text=reply)).to_have_count(1)
            expect(page.locator(_ASSISTANT)).to_have_count(index)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
            native_session, user_text = _assert_native_claude_turn(
                base_url, session_id, marker, reply, _TARGET_MODEL
            )
            _assert_mock_claude_turn(
                mock_llm_server_url,
                marker,
                _TARGET_MODEL,
                native_session=native_session,
                user_text=user_text,
            )
            native_sessions.append(native_session)
            routed = decisions(client, session_id)
            assert routed and all(
                row.get("applied") is True
                and row.get("model") == _TARGET_MODEL
                and row.get("router_source") == "oss-llm"
                for row in routed
            ), routed
            assert len(get_mock_requests(mock_llm_server_url, key=_JUDGE_MODEL)) == 1
            assert claude_settings_snapshot(settings_path) == saved_settings
            expect(page.get_by_test_id("error-headline")).to_have_count(0)
        assert native_sessions[0] == native_sessions[1]
        page.screenshot(path=str(tmp_path / f"routing-{entry}-preserved-default.png"))
    finally:
        client.delete(f"/v1/sessions/{session_id}").raise_for_status()
