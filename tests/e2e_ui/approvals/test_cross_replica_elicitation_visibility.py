r"""E2E: a parked Claude permission prompt is visible from a second replica.

Claude Code's permission-request hook parks its elicitation in the serving
server process while the POST waits for a verdict. On a multi-replica
deployment every replica shares one database, but a browser routed to a
replica other than the parking one loads the session without the pending
prompt: no approval card renders, and Claude waits on a question the user
cannot see.

The journey: park a Claude permission prompt through the real hook endpoint
on the primary server, confirm its approval card renders there, then boot a
second server process against the same database — a stand-in for a second
replica — and open the same session from it. The pending prompt must be in
that replica's session snapshot and its approval card must render there too.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e_ui.conftest import _BUILD_OUTPUT, _REPO_ROOT, _find_free_port, _server_state

_APPROVAL_CARD = '[data-testid="approval-card"]'
_COMPOSER = "Message the agent"
_LOAD_TIMEOUT_MS = 60_000
_RENDER_TIMEOUT_MS = 15_000
_REPLICA_HEALTH_TIMEOUT_S = 120.0


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate: Callable[[], object], *, timeout_s: float = 30.0) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError("condition not met within timeout")


def _spawn_replica(
    database_uri: str, artifact_dir: Path, log_path: Path
) -> tuple[subprocess.Popen[bytes], str]:
    """Boot a second server process against the shared database."""
    port = _find_free_port()
    env: dict[str, str] = {
        **os.environ,
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
    }
    apply_server_env(env, _REPO_ROOT)
    argv = [
        server_executable(),
        "-c",
        "from omnigent.cli import main; main()",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        database_uri,
        "--artifact-location",
        str(artifact_dir),
    ]
    log_handle = open(log_path, "w")  # noqa: SIM115 — handle lives for the Popen lifetime
    proc = subprocess.Popen(
        argv,
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return proc, f"http://127.0.0.1:{port}"


def _wait_replica_healthy(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    """Block until the replica answers /health, or fail with its log tail."""
    deadline = time.monotonic() + _REPLICA_HEALTH_TIMEOUT_S
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last_error = f"process exited early with code {proc.returncode}"
            break
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    log_text = log_path.read_text() if log_path.exists() else ""
    raise RuntimeError(
        f"second replica never became healthy on {base_url} "
        f"(last_error={last_error}).\n{log_text[-3000:]}"
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.timeout(300)
def test_parked_claude_permission_prompt_is_visible_from_a_second_replica(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Prompt parked on replica A renders in the session opened from replica B."""
    base_url, session_id = seeded_session
    result_holder: dict = {}

    def _post_hook() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={
                    "tool_name": "Bash",
                    "tool_input": {"command": "printf cross-replica"},
                },
                timeout=240.0,
            )
            resp.raise_for_status()
            result_holder["response"] = resp.json()
        except Exception as exc:  # surfaced after the assertions below
            result_holder["error"] = exc

    hook_thread = threading.Thread(target=_post_hook, daemon=True)
    hook_thread.start()

    _wait_for(lambda: _pending_elicitations(base_url, session_id))
    parked = _pending_elicitations(base_url, session_id)
    elicitation_id = parked[0].get("elicitation_id") or parked[0].get("id")
    assert isinstance(elicitation_id, str) and elicitation_id, parked

    # Parking replica: the card renders, so any failure below can only be
    # about the second replica.
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name=_COMPOSER)).to_be_visible(timeout=_LOAD_TIMEOUT_MS)
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first).to_be_visible(
        timeout=_RENDER_TIMEOUT_MS
    )

    artifact_dir = tmp_path / "replica_b_artifacts"
    artifact_dir.mkdir()
    log_path = tmp_path / "replica_b.log"
    replica_b, replica_b_url = _spawn_replica(
        str(_server_state["database_uri"]), artifact_dir, log_path
    )
    try:
        _wait_replica_healthy(replica_b, replica_b_url, log_path)

        page.goto(f"{replica_b_url}/c/{session_id}")
        expect(page.get_by_role("textbox", name=_COMPOSER)).to_be_visible(timeout=_LOAD_TIMEOUT_MS)
        # Allow any cross-replica fan-out to land before judging the snapshot.
        page.wait_for_timeout(3000)

        replica_b_pending = _pending_elicitations(replica_b_url, session_id)
        assert any(
            event.get("elicitation_id") == elicitation_id or event.get("id") == elicitation_id
            for event in replica_b_pending
        ), (
            "the second replica's session snapshot carries no pending "
            f"elicitation (got {replica_b_pending!r}); the prompt is parked "
            "only in the first replica's process memory"
        )
        expect(page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first).to_be_visible(
            timeout=_RENDER_TIMEOUT_MS
        )
    finally:
        _terminate(replica_b)
        # Unpark the hook so the session fixture can tear down cleanly.
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(
                f"{base_url}/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
                json={"action": "decline"},
                timeout=15.0,
            )
        hook_thread.join(timeout=30)
