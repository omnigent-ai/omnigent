"""A smart-routing fan-out's chips must say which sub-agent task each governed.

Drives the real user journey: a claude-native session with Smart Routing on
for spawns, the real Claude Code CLI launched by the runner, and the mock
model scripting one turn that fans out three ``Agent`` (Task) spawns. Each
spawn fires the real ``PreToolUse`` router hook -> runner loopback -> server
relay, persisting one ``routing_decision`` per task; the SPA renders one chip
per decision.

The three spawns share a ``subagent_type``, so only the task description can
tell their decisions apart. While the bug is live the three chips render
byte-identical and this test fails on the attribution assertions.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

_TASKS = (
    "Research auth flows",
    "Implement token refresh",
    "Review session storage",
)

# Native CLI boot + terminal attach + a three-spawn turn against the mock.
_FANOUT_TIMEOUT_MS = 240_000


def _create_routed_claude_native_session(base_url: str, runner_id: str) -> str:
    """Create a claude-native session with Smart Routing on, then bind it.

    Mirrors :func:`tests.e2e_ui.conftest._create_native_claude_session`, but
    stamps ``cost_control_mode_override`` / ``subagent_routing_override``
    between create and bind: the runner wires the spawn-routing hook at
    terminal launch only for sessions that are already Smart Routing ones.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname -> omnigent compat translator (the spec has
        # no spec_version), matching the native_claude_session fixture.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])

    patched = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"cost_control_mode_override": "on", "subagent_routing_override": "on"},
        timeout=10.0,
    )
    patched.raise_for_status()

    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


def _script_fanout_turn(mock_url: str) -> None:
    """Queue one parent turn that spawns three sub-agents; the rest is text.

    The fan-out entry rides a ``match`` queue keyed on the agent-types
    system-reminder, which only the main conversation's requests carry in
    user-role content. Claude Code's background calls (session naming embeds
    the user's message too) therefore cannot drain it, while the sub-agent
    turns and the parent's wrap-up land on the queue's text fallback.

    :param mock_url: Mock LLM server base URL.
    """
    reset_mock_llm(mock_url)
    tool_calls = [
        {
            # Claude Code's subagent-spawn tool (``Task`` before CLI 2.1.63).
            "name": "Agent",
            "call_id": f"toolu_routefan_{idx}",
            "arguments": json.dumps(
                {
                    "description": description,
                    "prompt": (
                        f"{description}. Summarize your approach in one short "
                        "sentence. Do not edit any files."
                    ),
                    "subagent_type": "general-purpose",
                }
            ),
        }
        for idx, description in enumerate(_TASKS)
    ]
    configure_mock_llm(
        mock_url,
        [{"tool_calls": tool_calls}],
        key="fanout-turn",
        match="Available agent types for the Agent tool",
    )
    set_fallback_mock_llm(mock_url, "fanout-turn", "All three tasks are complete.")
    set_fallback_mock_llm(mock_url, "default", "Acknowledged.")


def _send_from_composer(page: Page, text: str) -> None:
    """Type *text* into the web composer and send it.

    :param page: The Playwright page, on the session's chat surface.
    :param text: The message body to send.
    """
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _ensure_chat_view(page: Page) -> None:
    """Switch the terminal-first session to its chat bubble view.

    :param page: The Playwright page, on the session's chat surface.
    """
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=120_000)
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_fanout_routing_chips_name_the_task_each_governed(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Each fan-out routing chip is attributable to the task it governed."""
    if os.environ.get("LLM_API_KEY"):
        pytest.skip("needs the mock-LLM claude-native lane; unset LLM_API_KEY")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is not installed")

    _script_fanout_turn(mock_llm_server_url)
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    with _temp_omnigent_mock_config(mock_llm_server_url, "claude"):
        session_id = _create_routed_claude_native_session(live_server, runner_id)
        try:
            page.goto(f"{live_server}/c/{session_id}")
            _ensure_chat_view(page)
            _send_from_composer(
                page,
                "Fan out three sub-agents: research auth flows, implement "
                "token refresh, and review session storage.",
            )

            cards = page.get_by_test_id("routing-decision-card")
            spawn_cards = cards.filter(
                has=page.get_by_test_id("routing-decision-scope").filter(has_text="subagent")
            )
            expect(spawn_cards).to_have_count(3, timeout=_FANOUT_TIMEOUT_MS)

            # Open a raw verdict so the recorded journey shows what the
            # decision does (not) carry.
            spawn_cards.first.get_by_test_id("routing-decision-raw-toggle").click()
            expect(spawn_cards.first.locator("pre")).to_be_visible()

            items = httpx.get(f"{live_server}/v1/sessions/{session_id}/items", timeout=10.0)
            items.raise_for_status()
            decisions = [
                json.dumps(item)
                for item in items.json()["data"]
                if item["type"] == "routing_decision" and "native_subagent" in json.dumps(item)
            ]
            assert len(decisions) == 3, decisions
            for description in _TASKS:
                named = [d for d in decisions if description in d]
                assert len(named) == 1, (
                    f"expected exactly one spawn routing decision to name "
                    f"{description!r}; decisions: {decisions}"
                )

            for description in _TASKS:
                expect(
                    spawn_cards.filter(has_text=description),
                    f"one chip should name the task {description!r}",
                ).to_have_count(1)

            # The attribution must come from the persisted rows, not only the
            # live stream: a reload renders from GET /items.
            page.reload()
            _ensure_chat_view(page)
            expect(spawn_cards).to_have_count(3, timeout=60_000)
            for description in _TASKS:
                expect(
                    spawn_cards.filter(has_text=description),
                    f"one persisted chip should name the task {description!r}",
                ).to_have_count(1)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)
