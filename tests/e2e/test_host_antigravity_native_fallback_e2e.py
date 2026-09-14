"""Real native Antigravity reply delivery when the CLI's local RPC is unavailable.

The public host/session HTTP flow launches real agy, sends a Chat message, and
reads the persisted user/assistant items after the native Stop hook settles the
session. The fallback label is required, so a successful RPC reply cannot pass
as coverage of transcript fallback. Use an authenticated agy release with
unavailable local RPC (for example, 1.2.2); no transcript or completion is faked.

Opt in with ``OMNIGENT_E2E_ANTIGRAVITY_NATIVE=1`` and run this file with pytest.
The normal path reuses the shared server fixture and native host helpers. Set
``--omnigent-server-url=http://127.0.0.1:18779`` to reuse an isolated local server
and its online host instead. That path does not resolve credential/server
fixtures; the host must run on this machine and already have agy configured.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.native.native_coding_agents import ANTIGRAVITY_NATIVE_AGENT_NAME
from tests.e2e.helpers import POLL_INTERVAL_S
from tests.e2e.test_host_codex_native_e2e import (
    _assistant_text,
    _online_host_id,
    _ordered_message_items,
    _poll_for_terminal_resource,
    _send_user_text,
    _spawn_host_daemon,
    _user_text,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY_NATIVE") != "1"
        or shutil.which("agy") is None
        or shutil.which("tmux") is None,
        reason=(
            "native Antigravity fallback e2e needs "
            "OMNIGENT_E2E_ANTIGRAVITY_NATIVE=1, authenticated agy with unavailable "
            "local RPC, and tmux"
        ),
    ),
    pytest.mark.timeout(420, method="signal"),
]


@pytest.fixture
def antigravity_fallback_client(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[httpx.Client]:
    """Use an existing local rig or the suite's live server and native host."""
    server_url = request.config.getoption("--omnigent-server-url")
    if server_url:
        url = httpx.URL(server_url)
        if url.scheme != "http" or url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise pytest.UsageError("Antigravity E2E server must be an isolated loopback HTTP URL")
        with httpx.Client(base_url=server_url, timeout=30.0, trust_env=False) as client:
            yield client
        return

    client = request.getfixturevalue("http_client")
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=request.getfixturevalue("live_server"),
    )
    try:
        yield client
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)


def test_antigravity_native_fallback_reply_reaches_chat(
    antigravity_fallback_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A real TUI turn reaches Chat once, in order, and completes in fallback mode."""
    client = antigravity_fallback_client
    agents = client.get("/v1/agents")
    agents.raise_for_status()
    agent_id = next(
        (a["id"] for a in agents.json()["data"] if a["name"] == ANTIGRAVITY_NATIVE_AGENT_NAME),
        None,
    )
    assert agent_id is not None, "built-in native Antigravity agent was not registered"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = f"AGY_FALLBACK_{uuid.uuid4().hex}"
    expected = f"{marker}\ncafé 日本語 🚀"
    prompt = (
        "Reply with exactly the following two lines, preserving every character. "
        f"Do not use tools or add commentary or code fences.\n{expected}"
    )
    resource_id = terminal_resource_id("antigravity", "main")
    create = client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": _online_host_id(client),
            "workspace": str(workspace),
        },
        timeout=60.0,
    )
    create.raise_for_status()
    session_id = create.json()["id"]
    try:
        _poll_for_terminal_resource(
            client,
            session_id=session_id,
            resource_id=resource_id,
            timeout=90.0,
        )
        _send_user_text(client, session_id=session_id, text=prompt)

        deadline = time.monotonic() + 240.0
        while time.monotonic() < deadline:
            snapshot = client.get(f"/v1/sessions/{session_id}")
            snapshot.raise_for_status()
            session = snapshot.json()
            assert session.get("status") != "failed", "native Antigravity turn failed"
            messages = _ordered_message_items(client, session_id=session_id)
            if session.get("status") == "idle" and any(
                marker in _assistant_text(item) for item in messages
            ):
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            pytest.fail("native Antigravity did not persist its reply and settle to idle in 240s")

        assert session.get("labels", {}).get("antigravity_native_transcript_fallback") == "1", (
            "the turn did not exercise transcript fallback; use agy with unavailable local RPC"
        )
        assert [item["role"] for item in messages] == ["user", "assistant"]
        assert _user_text(messages[0]) == prompt
        assert _assistant_text(messages[1]).strip() == expected

        # A fresh items read is the same durable conversation that Chat reloads.
        reloaded = _ordered_message_items(client, session_id=session_id)
        assert reloaded == messages
    finally:
        # Close only this test's terminal, including after a failed or timed-out turn.
        with contextlib.suppress(httpx.HTTPError):
            client.delete(f"/v1/sessions/{session_id}/resources/terminals/{resource_id}")
