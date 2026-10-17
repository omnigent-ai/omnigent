"""E2E: ACP tool cards must render the ToolCallContent union, not a JSON dump.

An ACP agent reports tool results as the spec's ToolCallContent union
(https://agentclientprotocol.com/protocol/tool-calls): a ``content`` wrapper
holding a nested content block, or ``diff`` / ``terminal`` variants. The
executor adapter stringifies tool payloads for transcript tool cards; when it
doesn't recognize the union it falls back to ``json.dumps``, so the web UI's
tool-card Output panel shows the escaped JSON wrapper instead of the tool's
result text.

These tests drive the reported journey for real: register a generic ACP agent
(a hermetic stdio fake, same shape as
``tests/e2e_ui/files/test_files_tab_survives_acp_reply.py``) whose one turn
reports a ``content``, a ``diff``, and a ``terminal`` tool result; send a
message from the web composer; expand the settled turn's tool cards; and
assert each Output panel carries readable text rather than the raw union
JSON.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online

_ACP_SLUG = "fake-union-agent"
_ACP_REPLY_TEXT = "Checked the keys and updated the config."
_CONTENT_RESULT_TEXT = "keys: ['token', 'user_id', 'expires_at', 'refresh_token']"

# A minimal ACP agent speaking the Agent Client Protocol over stdio. Each
# session/prompt reports three completed tool calls whose results use the
# three ToolCallContent union variants (content / diff / terminal), then
# streams one deterministic reply chunk and ends the turn. Stdlib only, so
# any Python interpreter on the runner host can run it.
_FAKE_ACP_AGENT = r"""
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def update(session_id, payload):
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": payload},
    })


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "fake-acp-session-1"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        update(sid, {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-content-1",
            "title": "read_auth_keys",
            "kind": "read",
            "status": "in_progress",
            "rawInput": {"path": "auth_tokens.json"},
        })
        update(sid, {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-content-1",
            "status": "completed",
            "content": [{
                "type": "content",
                "content": {
                    "type": "text",
                    "text": "keys: ['token', 'user_id', 'expires_at', 'refresh_token']",
                },
            }],
        })
        update(sid, {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-diff-1",
            "title": "apply_config_diff",
            "kind": "edit",
            "status": "in_progress",
            "rawInput": {"path": "/work/config.py"},
        })
        update(sid, {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-diff-1",
            "status": "completed",
            "content": [{
                "type": "diff",
                "path": "/work/config.py",
                "oldText": "timeout = 30\n",
                "newText": "timeout = 60\n",
            }],
        })
        update(sid, {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-terminal-1",
            "title": "tail_build_log",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": "tail -n 5 build.log"},
        })
        update(sid, {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-terminal-1",
            "status": "completed",
            "content": [{"type": "terminal", "terminalId": "term-1"}],
        })
        update(sid, {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Checked the keys and updated the config."},
        })
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }})
"""


def _acp_launcher_bundle(agent_command: str) -> bytes:
    """Gzip-tar the launcher YAML ``omni run --harness acp:<slug>`` generates.

    :param agent_command: Command line that launches the fake ACP agent.
    :returns: The gzipped tarball bytes for the multipart session create.
    """
    from omnigent.cli import _materialize_harness_launcher_file
    from omnigent.onboarding.acp_auth import AcpAgentEntry

    launcher = _materialize_harness_launcher_file(
        harness=f"acp:{_ACP_SLUG}",
        model=None,
        system_prompt=None,
        acp_agent=AcpAgentEntry(
            slug=_ACP_SLUG,
            name="Fake Union ACP Agent",
            command=agent_command,
        ),
    )
    data = launcher.read_bytes()
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        info = tarfile.TarInfo(name=launcher.name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def acp_union_session(
    live_server: str,
    runner_id: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Bind a session to a generated ``acp:<slug>``-launcher agent.

    :param live_server: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind the session to.
    :param tmp_path: Per-test dir for the fake agent script.
    :param tmp_path_factory: Temp directories for a replacement runner's logs.
    :returns: ``(base_url, session_id)``.
    """
    agent_script = tmp_path / "fake_acp_union_agent.py"
    agent_script.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_script)])

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _acp_launcher_bundle(command), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    respawned_runner: subprocess.Popen[bytes] | None = None
    try:
        respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
        patch_resp = httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        patch_resp.raise_for_status()
        yield (live_server, session_id)
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            if respawned_runner is not None:
                respawned_runner.terminate()
                try:
                    respawned_runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned_runner.kill()
                    respawned_runner.wait(timeout=5)


def _drive_turn(page: Page, base_url: str, session_id: str) -> None:
    """Open the session, send one message, and wait for the agent's reply."""
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Check the auth keys and update the timeout config")
    composer.press("Enter")
    expect(page.get_by_text(_ACP_REPLY_TEXT)).to_be_visible(timeout=90_000)


def _expand_if_collapsed(trigger: Locator) -> None:
    if trigger.get_attribute("data-state") == "closed":
        trigger.click()


def _reveal_tool_card(page: Page, title_pattern: re.Pattern[str]) -> Locator:
    """Expand the settled turn's folds down to one tool card; return its root.

    Once the turn settles, the trace collapses behind the "Worked for" row
    and the contiguous completed tool run folds into a "Called 3 tools"
    summary, so both must be expanded before the card's trigger is visible.
    """
    fold_trigger = page.get_by_test_id("turn-worked-fold").get_by_role("button").first
    expect(fold_trigger).to_be_visible(timeout=30_000)
    # The fold mounts open and auto-collapses a frame later; let it settle.
    page.wait_for_timeout(500)
    _expand_if_collapsed(fold_trigger)

    group_trigger = page.get_by_role("button", name=re.compile(r"Called 3 tools")).first
    expect(group_trigger).to_be_visible(timeout=10_000)
    _expand_if_collapsed(group_trigger)

    card_trigger = page.get_by_role("button", name=title_pattern).first
    expect(card_trigger).to_be_visible(timeout=10_000)
    _expand_if_collapsed(card_trigger)
    return card_trigger.locator("xpath=..")


def test_acp_content_union_tool_card_renders_text(
    page: Page,
    acp_union_session: tuple[str, str],
) -> None:
    """A ``content``-variant ACP tool result renders as its inner text.

    The assertion pins the correct behavior: the card's Output panel shows
    the nested block's text, not the escaped ToolCallContent wrapper. With
    the bug the panel shows the ``json.dumps`` of the union list, so the
    wrapper's ``"type": "content"`` discriminator is visible and this test
    fails.
    """
    base_url, session_id = acp_union_session
    _drive_turn(page, base_url, session_id)

    card = _reveal_tool_card(page, re.compile("read_auth_keys"))
    expect(card.get_by_text("Output", exact=True)).to_be_visible(timeout=10_000)
    expect(card).to_contain_text(_CONTENT_RESULT_TEXT, timeout=10_000)
    expect(card).not_to_contain_text('"type": "content"')


def test_acp_diff_and_terminal_union_tool_cards_render_summaries(
    page: Page,
    acp_union_session: tuple[str, str],
) -> None:
    """``diff`` / ``terminal`` ACP tool results don't render as raw JSON.

    The union's other two variants must render as readable summaries; with
    the bug both Output panels show the ``json.dumps`` of the union list,
    so the JSON-quoted discriminators and field names are visible and this
    test fails.
    """
    base_url, session_id = acp_union_session
    _drive_turn(page, base_url, session_id)

    diff_card = _reveal_tool_card(page, re.compile("apply_config_diff"))
    terminal_card = _reveal_tool_card(page, re.compile("tail_build_log"))
    expect(diff_card.get_by_text("Output", exact=True)).to_be_visible(timeout=10_000)
    expect(terminal_card.get_by_text("Output", exact=True)).to_be_visible(timeout=10_000)

    expect(diff_card).not_to_contain_text('"type": "diff"')
    expect(diff_card).not_to_contain_text('"oldText"')
    expect(terminal_card).not_to_contain_text('"terminalId"')
