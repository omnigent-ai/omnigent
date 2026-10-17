"""E2E: ACP tool cards must render the ToolCallContent union, not a JSON dump.

An ACP agent reports tool results as the spec's ToolCallContent union
(https://agentclientprotocol.com/protocol/tool-calls): a ``content`` wrapper
holding a nested content block, or ``diff`` / ``terminal`` variants. The
executor adapter stringifies that list for the transcript's tool cards.

This test registers a generic ACP agent (a hermetic stdio fake, same shape as
``tests/e2e_ui/files/test_files_tab_survives_acp_reply.py``) whose one turn
reports a ``content``, a ``diff``, and a ``terminal`` tool result, sends a
message from the web composer, expands the settled turn's tool cards, and asserts
each Output panel shows readable text rather than the raw union JSON.
"""

from __future__ import annotations

import contextlib
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

from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import _ensure_runner_online

_ACP_SLUG = "fake-union-agent"
_ACP_REPLY_TEXT = "Checked the keys and updated the config."
_CONTENT_RESULT_TEXT = "keys: ['token', 'user_id', 'expires_at', 'refresh_token']"

# Minimal stdio ACP agent: each prompt reports three completed tool calls using the
# content/diff/terminal ToolCallContent union variants, then streams one reply chunk
# and ends the turn. Stdlib only; the fixture JSON-encodes the placeholder texts.
_FAKE_ACP_AGENT_TEMPLATE = r"""
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
                "content": {"type": "text", "text": __CONTENT_RESULT_TEXT__},
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
            "content": {"type": "text", "text": __REPLY_TEXT__},
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
    agent_script.write_text(
        _FAKE_ACP_AGENT_TEMPLATE.replace(
            "__CONTENT_RESULT_TEXT__", json.dumps(_CONTENT_RESULT_TEXT)
        ).replace("__REPLY_TEXT__", json.dumps(_ACP_REPLY_TEXT))
    )
    command = shlex.join([sys.executable, str(agent_script)])

    create_resp = post_session_bundle(
        httpx.post, f"{live_server}/v1/sessions", _acp_launcher_bundle(command), timeout=30.0
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    respawned_runner: subprocess.Popen[bytes] | None = None
    try:
        respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
        bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
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


def _expand_worked_fold(page: Page) -> None:
    """Expand the settled turn's "Worked for" fold once its auto-collapse has landed.

    The fold mounts open and collapses a frame after the turn settles, so wait for
    that collapse (when it comes) instead of racing it with a fixed sleep.
    """
    fold_trigger = page.get_by_test_id("turn-worked-fold").get_by_role("button").first
    expect(fold_trigger).to_be_visible(timeout=30_000)
    with contextlib.suppress(AssertionError):
        expect(fold_trigger).to_have_attribute("data-state", "closed", timeout=5_000)
    _expand_if_collapsed(fold_trigger)
    expect(fold_trigger).to_have_attribute("data-state", "open")


def _reveal_tool_card(page: Page, title_pattern: re.Pattern[str]) -> Locator:
    """Expand the "Called 3 tools" group of an open worked fold down to one tool card."""
    group_trigger = page.get_by_role("button", name=re.compile(r"Called 3 tools")).first
    expect(group_trigger).to_be_visible(timeout=10_000)
    _expand_if_collapsed(group_trigger)

    card_trigger = page.get_by_role("button", name=title_pattern).first
    expect(card_trigger).to_be_visible(timeout=10_000)
    _expand_if_collapsed(card_trigger)
    return card_trigger.locator("xpath=..")


def test_acp_union_tool_cards_render_readable_output(
    request: pytest.FixtureRequest,
    acp_union_session: tuple[str, str],
) -> None:
    """Each ACP union variant renders as readable text in its tool card, not the union JSON."""
    base_url, session_id = acp_union_session
    # Request the page after the session setup so a recording starts at the journey.
    page: Page = request.getfixturevalue("page")
    _drive_turn(page, base_url, session_id)
    _expand_worked_fold(page)

    content_card = _reveal_tool_card(page, re.compile("read_auth_keys"))
    expect(content_card.get_by_text("Output", exact=True)).to_be_visible(timeout=10_000)
    expect(content_card).to_contain_text(_CONTENT_RESULT_TEXT, timeout=10_000)
    expect(content_card).not_to_contain_text('"type": "content"')

    diff_card = _reveal_tool_card(page, re.compile("apply_config_diff"))
    terminal_card = _reveal_tool_card(page, re.compile("tail_build_log"))
    expect(diff_card.get_by_text("Output", exact=True)).to_be_visible(timeout=10_000)
    expect(terminal_card.get_by_text("Output", exact=True)).to_be_visible(timeout=10_000)

    expect(diff_card).to_contain_text("diff /work/config.py (1 line)", timeout=10_000)
    expect(diff_card).to_contain_text("timeout = 60")
    expect(terminal_card).to_contain_text("[terminal term-1]", timeout=10_000)
    expect(diff_card).not_to_contain_text('"type": "diff"')
    expect(diff_card).not_to_contain_text('"oldText"')
    expect(terminal_card).not_to_contain_text('"terminalId"')
