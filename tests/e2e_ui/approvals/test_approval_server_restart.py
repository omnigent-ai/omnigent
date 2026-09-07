"""E2E coverage for approving a policy-gated turn after a server restart."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_COMMAND = "printf approval-resumed"
_OUTPUT = "approval-resumed"
_FINAL_REPLY = "Approval resumed after restart."
_PROMPT = "Run the approval-gated command."

_AGENT_YAML = """\
spec_version: 1
name: approval_restart_probe
prompt: |
  Run the requested command with sys_os_shell. After the tool returns, reply
  with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_base_url}

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

guardrails:
  ask_timeout: 300
  policies:
    require_os_approval:
      type: function
      function: omnigent.policies.builtins.safety.ask_on_os_tools
"""

_EXTERNAL_MCP_AGENT_YAML = """\
spec_version: 1
name: external_elicitation_restart_probe
prompt: |
  Call elicitation_probe__deploy when asked to deploy. After it returns, reply
  with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_base_url}

tools:
  elicitation_probe:
    type: mcp
    command: {python}
    args:
      - {fixture}
    env:
      ELICITATION_INVOCATION_FILE: {invocation_file}
"""


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json().get("pending_elicitations") or []


def _shell_outputs(base_url: str, session_id: str) -> list[str]:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200},
        timeout=10.0,
    )
    response.raise_for_status()
    items = response.json()["data"]
    call_ids = {
        item.get("call_id") or (item.get("data") or {}).get("call_id")
        for item in items
        if item.get("type") == "function_call"
        and (item.get("name") or (item.get("data") or {}).get("name")) == "sys_os_shell"
    }
    return [
        str(item.get("output") or (item.get("data") or {}).get("output") or "")
        for item in items
        if item.get("type") == "function_call_output"
        and (item.get("call_id") or (item.get("data") or {}).get("call_id")) in call_ids
    ]


def _wait_for_shell_output(base_url: str, session_id: str, timeout_s: float = 30.0) -> list[str]:
    deadline = time.monotonic() + timeout_s
    outputs: list[str] = []
    while time.monotonic() < deadline:
        outputs = _shell_outputs(base_url, session_id)
        if any(_OUTPUT in output for output in outputs):
            return outputs
        time.sleep(0.25)
    return outputs


def _tool_outputs(base_url: str, session_id: str, tool_name: str) -> list[str]:
    """Return persisted outputs for calls to *tool_name*."""
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200},
        timeout=10.0,
    )
    response.raise_for_status()
    items = response.json()["data"]
    call_ids = {
        item.get("call_id") or (item.get("data") or {}).get("call_id")
        for item in items
        if item.get("type") == "function_call"
        and (item.get("name") or (item.get("data") or {}).get("name")) == tool_name
    }
    return [
        str(item.get("output") or (item.get("data") or {}).get("output") or "")
        for item in items
        if item.get("type") == "function_call_output"
        and (item.get("call_id") or (item.get("data") or {}).get("call_id")) in call_ids
    ]


@pytest.mark.timeout(300)
def test_pending_approval_can_be_approved_after_server_restart(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A server-only restart must not strand a runner's approval-gated turn."""
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    restart = _server_state.get("restart_server")
    if not callable(restart):
        pytest.skip("requires the locally spawned restartable server")
    restart_server = cast("Callable[[], None]", restart)
    runner_pid = int(cast(int, _server_state["runner_pid"]))
    server_pid = int(cast(int, _server_state["pid"]))
    runner_id = str(_server_state["runner_id"])

    model = f"approval-restart-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_approval_restart",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": _COMMAND}),
                    }
                ]
            }
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, _FINAL_REPLY)

    session_id = _create_bundled_session(
        live_server,
        runner_id,
        _AGENT_YAML.format(
            model=model,
            mock_base_url=json.dumps(f"{mock_llm_server_url}/v1"),
        ),
    )
    try:
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_placeholder("Send a message…")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(_PROMPT)
        page.get_by_role("button", name="Send", exact=True).click()

        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card).to_be_visible(timeout=120_000)
        assert _pending_elicitations(live_server, session_id)

        restart_server()
        assert int(cast(int, _server_state["pid"])) != server_pid
        assert int(cast(int, _server_state["runner_pid"])) == runner_pid
        os.kill(runner_pid, 0)

        page.reload()
        recovered_card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        try:
            expect(recovered_card).to_be_visible(timeout=30_000)
        except AssertionError as exc:
            raise AssertionError(
                "the server restarted while the runner stayed alive, but no "
                "answerable approval was recovered; "
                f"pending={_pending_elicitations(live_server, session_id)!r}, "
                f"tool_outputs={_shell_outputs(live_server, session_id)!r}"
            ) from exc
        assert not _shell_outputs(live_server, session_id), (
            "the command ran before the user approved the replayed request"
        )
        recovered_card.get_by_role("button", name="Approve", exact=True).click()

        expect(page.get_by_text(_FINAL_REPLY, exact=True)).to_be_visible(timeout=120_000)
        outputs = _wait_for_shell_output(live_server, session_id)
        assert any(_OUTPUT in output for output in outputs), (
            "the approval card was clickable, but the parked tool call did not resume: "
            f"outputs={outputs!r}"
        )
        assert not _pending_elicitations(live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


@pytest.mark.timeout(300)
def test_external_mcp_elicitation_can_be_answered_after_server_restart(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    tmp_path: Path,
) -> None:
    """A server restart must preserve an in-flight external MCP question."""
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    restart = _server_state.get("restart_server")
    if not callable(restart):
        pytest.skip("requires the locally spawned restartable server")
    restart_server = cast("Callable[[], None]", restart)
    runner_pid = int(cast(int, _server_state["runner_pid"]))
    server_pid = int(cast(int, _server_state["pid"]))
    runner_id = str(_server_state["runner_id"])
    invocation_file = tmp_path / "deploy-invocations.txt"

    model = f"external-elicit-restart-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_external_elicitation_restart",
                        "name": "elicitation_probe__deploy",
                        "arguments": "{}",
                    }
                ]
            }
        ],
        key=model,
    )
    final_reply = "External elicitation resumed after restart."
    set_fallback_mock_llm(mock_llm_server_url, model, final_reply)

    fixture = Path(__file__).parents[2] / "tools" / "fixtures" / "elicitation_enum_mcp_server.py"
    session_id = _create_bundled_session(
        live_server,
        runner_id,
        _EXTERNAL_MCP_AGENT_YAML.format(
            model=model,
            mock_base_url=json.dumps(f"{mock_llm_server_url}/v1"),
            python=json.dumps(sys.executable),
            fixture=json.dumps(str(fixture)),
            invocation_file=json.dumps(str(invocation_file)),
        ),
    )
    try:
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_placeholder("Send a message…")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill("Deploy and use the environment I select.")
        page.get_by_role("button", name="Send", exact=True).click()

        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        try:
            expect(card).to_be_visible(timeout=60_000)
        except AssertionError as exc:
            snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0).json()
            items = httpx.get(
                f"{live_server}/v1/sessions/{session_id}/items",
                params={"limit": 200},
                timeout=10.0,
            ).json()
            raise AssertionError(
                f"external MCP elicitation never appeared; snapshot={snapshot!r}, items={items!r}"
            ) from exc
        assert invocation_file.read_text(encoding="utf-8").splitlines() == ["deploy"]

        restart_server()
        assert int(cast(int, _server_state["pid"])) != server_pid
        assert int(cast(int, _server_state["runner_pid"])) == runner_pid
        os.kill(runner_pid, 0)

        page.reload()
        recovered_card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(recovered_card).to_be_visible(timeout=30_000)
        assert invocation_file.read_text(encoding="utf-8").splitlines() == ["deploy"], (
            "recovering the prompt replayed an external tool that may already have side effects"
        )
        recovered_card.get_by_role("button", name="prod", exact=True).click()

        expect(page.get_by_text(final_reply, exact=True).last).to_be_visible(timeout=120_000)
        outputs = _tool_outputs(live_server, session_id, "elicitation_probe__deploy")
        assert any("elicit_answer:prod" in output for output in outputs), outputs
        assert invocation_file.read_text(encoding="utf-8").splitlines() == ["deploy"]
        assert not _pending_elicitations(live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)
