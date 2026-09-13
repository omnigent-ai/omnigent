"""E2E: a turn on an ACP agent that requires ``authenticate`` must complete.

Grok Build (``grok agent stdio``) advertises ACP ``authMethods`` on
``initialize`` — including ``cached_token`` when ``~/.grok/auth.json`` holds a
valid token — and marks it as ``_meta.defaultAuthMethodId``. The agent then
rejects ``session/new`` with ``Authentication required`` until the client sends
``authenticate``. Omnigent's generic ACP executor
(``omnigent/inner/acp_executor.py``) goes ``initialize`` -> ``session/new``
without ever sending ``authenticate``, so every turn on such an agent dies
with ``inner executor error: ACP session/new failed: Authentication required``
even though the cached credentials are valid and ready to use.

This test drives that journey for real: it registers a hermetic Grok-shaped
ACP agent (a Python script speaking the Agent Client Protocol over stdio that
mirrors the grok CLI's auth handshake), materializes the launcher with the
CLI's own generator, opens the session in the web SPA, sends a chat message,
and asserts the agent's reply renders instead of a turn-failure error pill.
The reply is only reachable when the client authenticates with the advertised
non-browser method (``cached_token``) before ``session/new`` — the browser
method (``grok.com``) fails in this headless shape, exactly like a headless
sandbox — so the test fails while the executor skips ``authenticate`` and
passes once it performs the handshake.
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

_ACP_SLUG = "grok-like-agent"
_ACP_REPLY_TEXT = "ACP agent reply: authenticated turn completed"

# A minimal ACP agent speaking the Agent Client Protocol over stdio, mirroring
# the grok CLI's auth handshake: ``initialize`` advertises two auth methods
# (browser login + cached token) with the cached token as the default, and
# ``session/new`` is rejected with "Authentication required" until the client
# sends ``authenticate``. The browser method errors like it would on a
# headless host, so only ``cached_token`` can succeed. Stdlib only, so any
# Python interpreter on the runner host can run it.
_FAKE_GROK_ACP_AGENT = r"""
import sys, json

authenticated = False

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
            "authMethods": [
                {"id": "grok.com", "name": "Log in with grok.com",
                 "description": "Browser OAuth login"},
                {"id": "cached_token", "name": "Use cached credentials",
                 "description": "Reuse the CLI's stored auth token"},
            ],
            "_meta": {"defaultAuthMethodId": "cached_token"},
        }})
    elif method == "authenticate":
        method_id = (msg.get("params") or {}).get("methodId")
        if method_id == "cached_token":
            authenticated = True
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32603,
                "message": "Browser login is unavailable on a headless host",
            }})
    elif method == "session/new":
        if not authenticated:
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32000, "message": "Authentication required"}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"sessionId": "grok-like-session-1"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid, "update": {
                  "sessionUpdate": "agent_message_chunk",
                  "content": {"type": "text",
                              "text": "ACP agent reply: authenticated turn completed"}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }})
"""


def _acp_launcher_bundle(agent_command: str) -> bytes:
    """Gzip-tar the launcher YAML ``omni run --harness acp:<slug>`` generates.

    Calls the CLI's real materializer so the uploaded spec is byte-for-byte
    the one no-AGENT run dispatch produces for a generic ACP agent — the same
    generated shape that launches Grok Build and every other stdio ACP CLI.

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
            name="Grok-like ACP Agent",
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
def grok_like_acp_session(
    live_server: str,
    runner_id: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Bind a session to a Grok-shaped auth-gated ``acp:<slug>`` agent.

    Writes the hermetic fake agent script to disk, uploads the launcher
    bundle via the same multipart ``POST /v1/sessions`` the CLI's run dispatch
    uses, and binds the session to the spawned runner.

    :param live_server: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind the session to.
    :param tmp_path: Per-test dir for the fake agent script.
    :param tmp_path_factory: Temp directories for a replacement runner's logs.
    :returns: ``(base_url, session_id)``.
    """
    agent_script = tmp_path / "fake_grok_acp_agent.py"
    agent_script.write_text(_FAKE_GROK_ACP_AGENT)
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
        # Earlier tests may deliberately stop the session-scoped runner.
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


def test_turn_on_auth_gated_acp_agent_completes(
    page: Page,
    grok_like_acp_session: tuple[str, str],
) -> None:
    """A chat turn on an auth-advertising ACP agent must render its reply.

    The guarded journey: open the ``acp:<slug>`` session in the web UI and
    send a message. The agent rejects ``session/new`` until the client sends
    ``authenticate`` with the advertised ``cached_token`` method, so the reply
    renders only when the executor performs the ACP auth handshake. While it
    skips ``authenticate``, the turn dies and the chat shows a turn-failure
    error pill ("ACP session/new failed: Authentication required") — the
    failure point this test pins.
    """
    base_url, session_id = grok_like_acp_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # Drive the reported user action: send a turn to the ACP agent.
    composer.fill("Say hello")
    composer.press("Enter")

    reply = page.get_by_text(_ACP_REPLY_TEXT)
    # Turn-failure pills render with data-level="error"; info notices reuse
    # the same testid and must not count as a failure.
    error_pill = page.locator('[data-testid="error-pill"][data-level="error"]')

    # Wait for the turn to settle either way: the streamed reply (correct)
    # or a turn-failure pill (the bug).
    expect(reply.or_(error_pill.first).first).to_be_visible(timeout=90_000)

    if error_pill.count() > 0:
        # Expand the pill so the executor's raw error is visible (and lands
        # in any recording), then fail with the harvested detail.
        error_pill.first.click()
        detail = page.get_by_test_id("error-message-content").first
        expect(detail).to_be_visible(timeout=5_000)
        detail_text = detail.inner_text()
        page.wait_for_timeout(1_500)
        pytest.fail(f"turn failed instead of replying: {detail_text}")

    # The correct behavior: the authenticated turn streams its reply and no
    # turn-failure pill ever renders.
    expect(reply).to_be_visible()
    expect(error_pill).to_have_count(0)
