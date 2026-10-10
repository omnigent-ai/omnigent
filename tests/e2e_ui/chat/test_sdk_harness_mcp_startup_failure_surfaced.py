"""SDK-harness MCP startup failures surface on the agent-info popover.

An SDK-harness session (claude-sdk, openai-agents, ...) whose MCP server
fails to start used to show the user nothing: the runner's ``tools/list``
proxy dropped the per-server failure and the server only logged a one-line
``runner MCP server '<name>' unavailable`` WARN, so the session looked stuck
or quietly answered without the tool. Failure notices stay out of the
conversation viewport (they are setup diagnostics, not conversation
content); instead the header agent-info trigger flips to a warning state
and the popover's Tools section names each failed server with its error.

This test drives the real journey rather than fabricating an ``mcp_startup``
map: it registers an SDK-harness agent whose only tool is an unreachable
HTTP MCP server, opens the session, and exercises the product's own
runner-backed ``tools/list`` endpoint so the runner reports the failure. It
then asserts the failure surfaces on the agent-info diagnostics surface and
stays out of the chat viewport.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_TRIGGER = '[data-testid="agent-info-trigger"]'
_FAILURE_ICON = '[data-testid="agent-info-mcp-failure-icon"]'
_FAILURE_BLOCK = '[data-testid="mcp-startup-failures"]'
_STARTUP_BAND = '[data-testid="mcp-startup-indicator"]'
_CHAT_NOTICE = "MCP startup incomplete"
_SERVER_NAME = "pipeshub"

# An SDK-harness agent whose only tool is a remote HTTP MCP server that
# never accepts a connection. gpt-4o-mini keeps the openai-agents harness on
# the in-process mock (no provider auth) — see the conftest agent-YAML note.
_AGENT_YAML = f"""\
spec_version: 1
name: mcp_startup_failure_probe
prompt: |
  You are an assistant with one tool, {_SERVER_NAME}, a remote knowledge base.
  Use {_SERVER_NAME} to look up documents when the user asks about one.

executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

tools:
  {_SERVER_NAME}:
    type: mcp
    url: "http://127.0.0.1:9/mcp"
"""


@pytest.fixture
def unreachable_mcp_session(
    live_server: str,
    runner_id: str,
) -> Iterator[tuple[str, str]]:
    """A runner-bound SDK-harness session whose only MCP server is unreachable.

    :param live_server: Spawned/prepared server base URL.
    :param runner_id: Token-bound runner id the session dispatches to.
    :returns: ``(base_url, session_id)``.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _AGENT_YAML.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    ).raise_for_status()
    try:
        yield live_server, session_id
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def _request_tools_list(base_url: str, session_id: str) -> None:
    """Enumerate the session's MCP tools through the product's runner proxy.

    The runner tries to connect to the unreachable server and reports the
    per-server failure, which the server publishes through
    ``session.mcp_startup``.
    """
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        timeout=60.0,
    )


def test_sdk_harness_mcp_startup_failure_surfaced_in_web_ui(
    page: Page,
    unreachable_mcp_session: tuple[str, str],
) -> None:
    """A failed MCP server is named on the agent-info surface, not the chat.

    Journey: open a session on an SDK-harness agent whose only tool is an
    unreachable MCP server; the runner's tool enumeration fails; the header
    agent-info trigger flips to a warning state; opening the popover names
    the failed server and its error; the failure never appears in the chat
    viewport.

    :param page: Playwright page fixture.
    :param unreachable_mcp_session: ``(base_url, session_id)``.
    :returns: None.
    """
    base_url, session_id = unreachable_mcp_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

    # The session stream is snapshot-plus-live-tail with no replay, so a
    # failure published between snapshot load and live subscribe is missed;
    # re-request the tool list until the diagnostics surface reflects it.
    deadline = time.monotonic() + 30.0
    while True:
        _request_tools_list(base_url, session_id)
        try:
            expect(page.locator(_FAILURE_ICON)).to_be_visible(timeout=3_000)
            break
        except AssertionError:
            if time.monotonic() >= deadline:
                raise

    # The failure is a setup diagnostic, never conversation content.
    expect(page.get_by_text(_CHAT_NOTICE)).to_have_count(0)
    expect(page.locator(_STARTUP_BAND)).to_have_count(0)

    # The agent-info popover names the failed server and its error.
    page.locator(_TRIGGER).click()
    failure_block = page.locator(_FAILURE_BLOCK)
    expect(failure_block).to_be_visible(timeout=5_000)
    expect(failure_block).to_contain_text(_SERVER_NAME)
    expect(failure_block).to_contain_text("failed to start")
