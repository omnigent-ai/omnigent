"""E2E: skills must work on a Devin-shaped ACP session.

Devin runs on the generic ACP harness (``acp:<slug>`` — the same wrap the
``acp:devin`` builtin row uses). A workspace can carry skills
(``.claude/skills/<dir>/SKILL.md``); the composer's slash menu lists them and
an invocation must deliver the skill's instructions to the agent. This drives
that journey against a hermetic Devin-shaped ACP agent that reports whether
the skill body ever reached it: the reply says "delivered" only when the
``session/prompt`` text contains the SKILL.md body marker.
"""

from __future__ import annotations

import gzip
import io
import json
import shlex
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online

_ACP_SLUG = "devin-like-agent"
_SKILL_NAME = "grill-me"
_SKILL_BODY_MARKER = "SKILL-BODY-DELIVERY-MARKER"
_DELIVERED_TEXT = "SKILL INSTRUCTIONS DELIVERED: the agent received the skill body"
_MISSING_PREFIX = "SKILL INSTRUCTIONS MISSING"

# A minimal Devin-shaped ACP agent speaking the Agent Client Protocol over
# stdio. Its reply is a deterministic probe: "delivered" when the prompt text
# carries the skill body marker, otherwise "missing" plus the tail of what it
# actually saw. Stdlib only.
_FAKE_DEVIN_ACP_AGENT = r"""
import sys, json

MARKER = "SKILL-BODY-DELIVERY-MARKER"

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

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
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"sessionId": "devin-like-session-1"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        text = "\n".join(
            b.get("text", "") for b in msg["params"].get("prompt", [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        if MARKER in text:
            reply = "SKILL INSTRUCTIONS DELIVERED: the agent received the skill body"
        else:
            tail = text[-200:].replace("\n", " ")
            reply = "SKILL INSTRUCTIONS MISSING: agent saw only: ..." + tail
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid, "update": {
                  "sessionUpdate": "agent_message_chunk",
                  "content": {"type": "text", "text": reply}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""

_SKILL_MD = f"""---
name: {_SKILL_NAME}
description: Repro skill verifying instruction delivery on ACP.
---

# Grill me

When this skill is invoked, interrogate the plan ruthlessly.

{_SKILL_BODY_MARKER}
"""


def _acp_agent_bundle(agent_command: str) -> bytes:
    """Tar an ``acp:<slug>`` agent YAML (the generic ACP launcher shape)."""
    agent_yaml = "\n".join(
        [
            f"name: {_ACP_SLUG}",
            "prompt: You are a skills-delivery probe agent.",
            "executor:",
            f"  harness: acp:{_ACP_SLUG}",
            "  acp_agent:",
            "    name: Devin-like ACP Agent",
            f"    command: {json.dumps(agent_command)}",
            "",
        ]
    )
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = agent_yaml.encode()
        info = tarfile.TarInfo(name="agent.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def devin_like_acp_session(
    live_server: str,
    runner_id: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Bind a session to a Devin-shaped ``acp:<slug>`` agent in a workspace
    that carries one ``.claude/skills`` skill."""
    agent_script = tmp_path / "fake_devin_acp_agent.py"
    agent_script.write_text(_FAKE_DEVIN_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_script)])

    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".claude" / "skills" / _SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD)

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(workspace)})},
        files={"bundle": ("agent.tar.gz", _acp_agent_bundle(command), "application/gzip")},
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


def test_acp_session_skill_menu_and_delivery(
    page: Page,
    devin_like_acp_session: tuple[str, str],
) -> None:
    """The slash menu lists the workspace skill and invoking it delivers the body."""
    base_url, session_id = devin_like_acp_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # Facet A: the composer slash menu lists the workspace skill.
    composer.fill("/")
    menu_item = page.get_by_test_id(f"slash-menu-item-{_SKILL_NAME}")
    expect(menu_item).to_be_visible(timeout=30_000)

    # Facet B: invoking the skill delivers its instructions to the agent.
    composer.fill(f"/{_SKILL_NAME} review this plan")
    composer.press("Enter")

    delivered = page.get_by_text(_DELIVERED_TEXT)
    missing = page.get_by_text(_MISSING_PREFIX)
    error_pill = page.locator('[data-testid="error-pill"][data-level="error"]')

    expect(delivered.or_(missing.first).or_(error_pill.first).first).to_be_visible(timeout=90_000)

    if error_pill.count() > 0:
        error_pill.first.click()
        detail = page.get_by_test_id("error-message-content").first
        expect(detail).to_be_visible(timeout=5_000)
        pytest.fail(f"skill invocation failed with an error: {detail.inner_text()}")
    if missing.count() > 0:
        pytest.fail(f"skill instructions never reached the agent: {missing.first.inner_text()}")

    expect(delivered).to_be_visible()
