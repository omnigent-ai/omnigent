r"""E2E: a Claude permission prompt parked on one replica renders on another.

On a multi-replica deployment (e.g. Databricks Apps behind a load
balancer) a Claude Code elicitation is not visible on the web UI. The claude-native
``PermissionRequest`` hook parks the prompt in the serving replica's
in-memory ``pending_elicitations`` index; the SSE stream has no replay, so
a browser whose ``GET /v1/sessions/{id}`` snapshot lands on any *other*
replica receives ``pending_elicitations: []`` and renders no approval card.
Claude then sits waiting on a prompt the user cannot see. Only the pending
*count* is mirrored to the conversations row cross-replica
(``session_live_state.persist_pending_count``); the event payload the card
is built from is replica-local.

The reproduction runs the real journey with two server processes sharing
one database, which is exactly what multi-replica deployments do:

1. a Claude permission prompt is parked via the real
   ``POST /v1/sessions/{id}/hooks/permission-request`` path on replica A;
2. the session is opened from replica A (control) — the card renders;
3. the same session is opened from replica B — the multi-replica journey —
   where the card must equally render. Today it does not, which is this bug.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _find_free_port, _server_state

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_APPROVAL_CARD = '[data-testid="approval-card"]'
# Accessible name of the composer textbox — stable across composer states
# (the placeholder mutates while a prompt is pending), so it doubles as the
# "transcript is up" signal on both replicas.
_COMPOSER = "Message the agent"

_LOAD_TIMEOUT_MS = 60_000
_RENDER_TIMEOUT_MS = 15_000
# Replica B does a full server boot (migrations no-op on the shared DB).
_REPLICA_HEALTH_TIMEOUT_S = 60.0


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _park_claude_permission_prompt(
    base_url: str, session_id: str, holder: dict
) -> threading.Thread:
    """Park a claude-native PermissionRequest on *session_id*.

    Mirrors the wrapper's hook POST: the server parks the elicitation and
    long-polls for the web verdict, so the call blocks until the test
    resolves it — it runs on its own thread.
    """

    def _post() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
                json={
                    "tool_name": "Bash",
                    "tool_input": {"command": "rm -rf build/"},
                },
                timeout=180.0,
            )
            resp.raise_for_status()
            holder["response"] = resp.json() if resp.content else None
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=_post, daemon=True)
    thread.start()
    return thread


def _spawn_replica_b(db_uri: str, tmp_root: Path) -> tuple[subprocess.Popen, str, Path]:
    """Boot a second ``omnigent server`` against the SAME database.

    This is the multi-replica topology in miniature: two replicas, one shared DB,
    each with its own in-memory state. Returns the process, its base URL,
    and its log path (dumped on boot failure for triage).
    """
    port = _find_free_port()
    artifacts = tmp_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    log_path = tmp_root / "replica_b.log"
    env: dict[str, str] = {
        **os.environ,
        # Import the worktree's omnigent, same as the conftest server spawn.
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    # No runner ever tunnels to replica B; drop replica A's binding token.
    env.pop("OMNIGENT_RUNNER_TUNNEL_TOKEN", None)
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for Popen lifetime
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            db_uri,
            "--artifact-location",
            str(artifacts),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + _REPLICA_HEALTH_TIMEOUT_S
    last_error = "no response"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2.0)
            if resp.status_code == 200:
                return proc, base_url, log_path
            last_error = f"health HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    proc.terminate()
    tail = ""
    with contextlib.suppress(OSError):
        tail = log_path.read_text()[-2000:]
    raise RuntimeError(f"replica B never became healthy ({last_error}); log tail:\n{tail}")


def _resolve_elicitation(base_url: str, session_id: str, elicitation_id: str) -> None:
    """Decline the parked prompt so the hook long-poll drains at teardown."""
    try:
        httpx.post(
            f"{base_url}/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "decline"},
            timeout=30.0,
        )
    except httpx.HTTPError:
        _log.warning("teardown resolve failed", exc_info=True)


@pytest.mark.timeout(300)
def test_claude_permission_prompt_is_visible_from_a_replica_that_did_not_park_it(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A prompt parked on replica A must render when the SPA loads from replica B.

    On a multi-replica deployment the browser has no replica affinity to the
    pod that parked the elicitation, so visibility from a non-parking replica
    IS the user contract. Today replica B's snapshot carries no pending
    elicitations, no card renders, and Claude waits forever.
    """
    base_a, session_id = seeded_session
    db_uri = str(_server_state.get("database_uri") or "")
    if not db_uri:
        pytest.skip("no spawned-server database to share (external --ui-base-url run)")

    holder: dict = {}
    hook_thread = _park_claude_permission_prompt(base_a, session_id, holder)

    # The prompt is parked before any page opens: the SSE stream has no
    # replay, so on BOTH replicas the card can only come from the snapshot.
    def _parked() -> bool:
        if "error" in holder:
            raise AssertionError(f"hook POST failed: {holder['error']}") from holder["error"]
        return bool(_pending_elicitations(base_a, session_id))

    _wait_for(_parked)
    elicitation_id = str(_pending_elicitations(base_a, session_id)[0].get("elicitation_id") or "")
    assert elicitation_id, "parked elicitation carries no elicitation_id"

    proc_b, base_b, log_path = _spawn_replica_b(db_uri, tmp_path_factory.mktemp("cross_replica_b"))
    _log.info("replica A=%s replica B=%s session=%s", base_a, base_b, session_id)
    try:
        # Control: the replica that parked the prompt renders the card.
        page.goto(f"{base_a}/c/{session_id}")
        expect(page.get_by_role("textbox", name=_COMPOSER)).to_be_visible(timeout=_LOAD_TIMEOUT_MS)
        card_a = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card_a).to_be_visible(timeout=_RENDER_TIMEOUT_MS)
        expect(card_a).to_contain_text("Bash")

        # The lakebox journey: the SPA lands on a replica that did NOT park
        # the prompt. Same session, same shared DB — the page loads fine...
        page.goto(f"{base_b}/c/{session_id}")
        expect(page.get_by_role("textbox", name=_COMPOSER)).to_be_visible(timeout=_LOAD_TIMEOUT_MS)
        # ...and the pending prompt must be visible here too. This is the
        # broken assertion: no approval card renders on replica B.
        card_b = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        expect(card_b).to_be_visible(timeout=_RENDER_TIMEOUT_MS)
        expect(card_b).to_contain_text("Bash")
    finally:
        _resolve_elicitation(base_a, session_id, elicitation_id)
        hook_thread.join(timeout=30)
        proc_b.terminate()
        try:
            proc_b.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc_b.kill()
            proc_b.wait(timeout=10)
        _log.info("replica B log at %s", log_path)
