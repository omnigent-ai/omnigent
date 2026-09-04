"""E2E: the Files tab must survive a generic ACP agent's first reply.

A user registers a generic ACP coding agent (``omni setup``) and launches it
from a project directory with ``omni run --harness acp:<slug>``. The web SPA
gates the Files tab on the session's default environment resource
(``useWorkspaceEnvironment`` -> ``showFilesPanel`` in
``web/src/shell/AppShell.tsx``); when the generated launcher spec carries no
top-level ``os_env`` the runner 404s that resource, so the Files tab shows at
first (the query's no-flash default) and unmounts once availability resolves
after the agent starts responding.

This test drives that journey for real: it materializes the launcher spec with
the CLI's own generator (so the uploaded bundle is exactly what no-AGENT run
dispatch produces), embeds a hermetic fake ACP agent (a Python script speaking
the Agent Client Protocol over stdio - the same shape as
``tests/inner/test_acp_executor.py``), sends a chat message, waits for the ACP
agent's streamed reply to render, and asserts the Files tab is still
available. It fails when generic ACP launchers drop workspace browsing and
passes while they preserve it.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import shlex
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_ACP_SLUG = "fake-agent"
_ACP_REPLY_TEXT = "ACP agent reply: hello from fake-agent"

# A minimal ACP agent speaking the Agent Client Protocol over stdio, mirroring
# the hermetic fake in tests/inner/test_acp_executor.py but without the
# permission round-trip: initialize -> session/new -> session/prompt streams
# one deterministic agent_message_chunk and completes the turn. Stdlib only,
# so any Python interpreter on the runner host can run it.
_FAKE_ACP_AGENT = r"""
import sys, json

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
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "fake-acp-session-1"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid, "update": {
                  "sessionUpdate": "agent_message_chunk",
                  "content": {"type": "text",
                              "text": "ACP agent reply: hello from fake-agent"}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }})
"""


def _acp_launcher_bundle(agent_command: str) -> bytes:
    """Gzip-tar the launcher YAML ``omni run --harness acp:<slug>`` generates.

    Calls the CLI's real materializer so the uploaded spec is byte-for-byte
    the one no-AGENT run dispatch produces for a generic ACP agent - the
    regression this guards lived in that generated shape (a launcher without
    ``os_env`` loses the web Files panel once availability resolves).

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
            name="Fake ACP Agent",
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
def acp_launcher_session(
    live_server: str,
    runner_id: str,
    tmp_path: Path,
) -> Iterator[tuple[str, str]]:
    """Bind a session to a generated ``acp:<slug>``-launcher agent.

    Writes the hermetic fake ACP agent script to disk, uploads the launcher
    bundle via the same multipart ``POST /v1/sessions`` the CLI's run dispatch
    uses, and binds the session to the spawned runner.

    :param live_server: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind the session to.
    :param tmp_path: Per-test dir for the fake agent script.
    :returns: ``(base_url, session_id)``.
    """
    agent_script = tmp_path / "fake_acp_agent.py"
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

    try:
        patch_resp = httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        patch_resp.raise_for_status()
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def test_files_tab_survives_generic_acp_agent_reply(
    page: Page,
    acp_launcher_session: tuple[str, str],
) -> None:
    """The Files tab stays available after a generic ACP agent's first reply.

    The guarded journey: open the ``acp:<slug>`` session in the web UI, send
    an arbitrary request, and watch the Files tab across the reply. The
    assertion pins the *correct* behavior (tab still available after the
    reply renders), so it fails whenever generic ACP launchers drop workspace
    browsing and passes while they preserve it.
    """
    base_url, session_id = acp_launcher_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # Scope to the desktop "Workspace" rail so the locator can't match the
    # hidden mobile drawer that mirrors the same tab markup.
    rail = page.get_by_role("complementary", name="Workspace")
    files_tab = rail.get_by_role("tab", name=re.compile("^Files"))

    # Drive the reported user action: an arbitrary request to the ACP agent.
    composer.fill("Say hello")
    composer.press("Enter")

    # The fake ACP agent streams a deterministic reply - once it renders, the
    # agent has "started responding", the moment the disappearance was pinned
    # to (the runner is now serving the session's resources, so the
    # environment availability query has resolved).
    expect(page.get_by_text(_ACP_REPLY_TEXT)).to_be_visible(timeout=90_000)

    # The launched agent ran from a real working directory, so workspace
    # browsing must survive the reply. A launcher without os_env makes the
    # environment resource 404 and the Files tab unmount - this assertion is
    # the failure point.
    expect(files_tab).to_be_visible(timeout=15_000)
