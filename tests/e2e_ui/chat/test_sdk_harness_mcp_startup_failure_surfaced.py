"""An SDK-harness session whose MCP server cannot start must show the failure in the web UI.

Native harnesses report MCP startup through ``external_mcp_startup``. SDK harnesses
(claude-sdk, openai-agents) list MCP tools through the runner's per-turn ``tools/list``
proxy instead, and its per-server failures must reach the user as well, not only the
server log: the header agent-info trigger warns and its popover names the failed
server, while the chat transcript stays clean.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

_MCP_SERVER = "pipeshub"
# TCP port 9 refuses connections, so the runner's MCP connect fails immediately.
_MCP_URL = "http://127.0.0.1:9/mcp"
_CLAUDE_MODEL = "claude-sonnet-4-20250514"
_OPENAI_MODEL = "gpt-4o-mini"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_BAND = '[data-testid="mcp-startup-indicator"]'


def _runner_visible_model_url(mock_llm_server_url: str) -> str:
    """Return the mock model URL the runner itself can reach.

    The workflow-owned reproduction environment relays the mock through a
    per-invocation port that only this test process can see.
    """
    if not _server_state.get("workflow_owned"):
        return mock_llm_server_url
    state = json.loads((_REPO_ROOT / ".omnigent" / "repro-env" / "environment.json").read_text())
    return str(state["mock_url"])


def _agent_spec(name: str, harness: str, model_url: str) -> dict[str, object]:
    executor: dict[str, object] = {"harness": harness}
    if harness == "claude-sdk":
        executor.update(
            model=_CLAUDE_MODEL,
            context_window=200_000,
            auth={"type": "api_key", "api_key": "mock-key", "base_url": model_url},
        )
    else:
        executor["model"] = _OPENAI_MODEL
    return {
        "name": name,
        "prompt": "You are a terse assistant. When asked about documents, use the pipeshub tool.",
        "executor": executor,
        "tools": {_MCP_SERVER: {"type": "mcp", "url": _MCP_URL}},
    }


def _create_session(base_url: str, runner_id: str, harness: str, model_url: str) -> str:
    """Register an agent whose only tool is the unreachable MCP server and bind its session."""
    name = f"mcp-down-{harness}-{uuid.uuid4().hex[:6]}"
    spec = yaml.safe_dump(_agent_spec(name, harness, model_url), sort_keys=False).encode()
    bundle = bundle_files({f"{name}.yaml": spec})
    created = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    agent = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    agent.raise_for_status()
    assert agent.json()["harness"] == harness
    assert [srv["name"] for srv in agent.json()["mcp_servers"]] == [_MCP_SERVER]
    return session_id


def _mcp_tools_offered(mock_url: str, nonce: str) -> set[str]:
    """Names of MCP-server tools the model was offered during the nonce's turn."""
    requests = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
    turn_requests = [r for r in requests if nonce in json.dumps(r)]
    assert turn_requests, "the scripted turn never reached the mock model"
    offered: set[str] = set()
    for req in turn_requests:
        for tool in req.get("tools") or []:
            name = tool.get("name") or (tool.get("function") or {}).get("name") or ""
            if _MCP_SERVER in name:
                offered.add(name)
    return offered


@pytest.mark.timeout(600)
@pytest.mark.parametrize("harness", ["claude-sdk", "openai-agents"])
def test_mcp_startup_failure_is_visible_in_sdk_harness_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    harness: str,
) -> None:
    """Send one message on an SDK-harness agent whose MCP server is unreachable.

    Journey: open the session, ask for the pipeshub tool, wait for the reply, then
    look at the header and the "Agent tools and policies" popover. The runner's
    connect to pipeshub fails and the model is never offered its tools, so the
    trigger must warn and the popover must name pipeshub as failed to start, while
    no startup band appears in the chat transcript.
    """
    if harness == "claude-sdk" and shutil.which("claude") is None:
        pytest.skip("claude-sdk needs the Claude Code CLI on PATH")
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_session(
            live_server, runner_id, harness, _runner_visible_model_url(mock_llm_server_url)
        )
        try:
            reset_mock_llm(mock_llm_server_url)
            nonce = f"q3-{uuid.uuid4().hex[:6]}"
            # Neutral reply: no server name and no failure word, so only the product's
            # own diagnostics can satisfy the assertions below.
            reply = f"Here is a summary of the Q3 roadmap from what I already know. [{nonce}]"
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": reply}] * 6,
                key=f"mcp-down-{nonce}",
                match=nonce,
                required_tools=[],
            )
            for model in (_CLAUDE_MODEL, _OPENAI_MODEL):
                set_fallback_mock_llm(mock_llm_server_url, model, reply)

            page: Page = request.getfixturevalue("page")
            page.goto(f"{live_server}/c/{session_id}")
            composer = page.get_by_role("textbox", name="Message the agent")
            expect(composer).to_be_enabled(timeout=30_000)
            composer.fill(f"Use pipeshub to find the Q3 roadmap document. [{nonce}]")
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator(_ASSISTANT).filter(has_text=nonce)).to_be_visible(timeout=180_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

            offered = _mcp_tools_offered(mock_llm_server_url, nonce)
            assert not offered, f"{_MCP_SERVER} connected after all; tools offered: {offered}"

            # The failure is a setup diagnostic: the header trigger warns and its popover
            # names the server, while the chat transcript shows no startup band.
            expect(page.locator(_BAND)).to_have_count(0)
            trigger = page.get_by_test_id("agent-info-trigger")
            expect(trigger).to_have_attribute(
                "aria-label", re.compile("MCP server startup failed"), timeout=30_000
            )
            expect(page.get_by_test_id("agent-info-mcp-failure-icon")).to_be_visible()
            trigger.click()
            failure_block = page.get_by_test_id("mcp-startup-failures")
            expect(failure_block).to_be_visible(timeout=10_000)
            expect(failure_block).to_contain_text(_MCP_SERVER)
            expect(failure_block).to_contain_text("failed to start")

            output_dir = Path(str(request.config.getoption("--output")))
            output_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output_dir / f"mcp-startup-failure-{harness}.png"))
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)
