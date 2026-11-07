"""E2E regression: a stale ``SSL_CERT_FILE`` must not stop the supervised Claude
transcript forwarder from persisting a session's transcript items.

The real ``supervise_forwarder`` runs against a real local ``omnigent server``
with the loopback classification overridden, so ``open_server_client`` takes
its remote-server branch (``trust_env=True`` plus the shared verifying
context). A seeded one-turn JSONL transcript replaces a live Claude CLI, and
delivery is checked through the session items API that the web chat view
renders.

Run::

    .venv/bin/python -m pytest \\
        tests/e2e/test_claude_native_forwarder_tls_ca_restart_loop_e2e.py -v

No ``--llm-api-key`` / ``--profile`` needed; no LLM is invoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest

from tests._helpers.live_server import isolated_local_server
from tests._helpers.native_session import create_native_session

# CI shells can carry an egress proxy; every HTTP call here targets the local
# server, so bypass it.
_http = httpx.Client(trust_env=False)

# Per-leg forwarder drive budget. The buggy leg exits early once the crash
# loop is demonstrated (>= 3 identical restarts, reached ~3s in); the fixed
# leg exits early once both markers are mirrored (~1-2s in).
_DRIVE_BUDGET_S = 20.0

_USER_CONTROL = "marker-user-control-healthy-ca"
_ASSISTANT_CONTROL = "marker-assistant-control-healthy-ca"
_USER_BUG = "marker-user-stale-ca"
_ASSISTANT_BUG = "marker-assistant-stale-ca"

_CRASH_LOG_PREFIX = "Claude transcript forwarder crashed"


def _seed_conversation_transcript(
    bridge_dir: Path, user_marker: str, assistant_marker: str
) -> Path:
    """Write a one-turn Claude JSONL transcript plus a ``Stop`` hook event.

    Records use the shape a live Claude CLI writes (``uuid`` is the forwarder's
    idempotency key); the hook reports the transcript path so the first poll
    resolves it.

    :param bridge_dir: Native Claude bridge directory.
    :param user_marker: Marker text for the user record.
    :param assistant_marker: Marker text for the assistant record.
    :returns: The transcript path.
    """
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"
    lines: list[dict[str, Any]] = [
        {
            "type": "user",
            "isSidechain": False,
            "uuid": f"{user_marker}-uuid",
            "message": {"role": "user", "content": user_marker},
            "promptSource": "typed",
            "userType": "external",
        },
        {
            "type": "assistant",
            "isSidechain": False,
            "uuid": f"{assistant_marker}-uuid",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_marker}],
            },
        },
    ]
    transcript_path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session-stale-tls-ca",
            "transcript_path": str(transcript_path),
        },
    )
    return transcript_path


def _count_marker(base_url: str, session_id: str, marker: str) -> int:
    """Count committed conversation items whose serialized payload contains *marker*.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation to query.
    :param marker: Substring to match.
    :returns: Number of committed items carrying the marker.
    """
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return sum(1 for item in resp.json()["data"] if marker in json.dumps(item))


class _CrashLogCapture(logging.Handler):
    """Collect ``(message, exception type)`` for each supervisor crash-restart record."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.crashes: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if _CRASH_LOG_PREFIX in msg:
            exc_name = "?"
            if record.exc_info and record.exc_info[0] is not None:
                exc_name = record.exc_info[0].__name__
            self.crashes.append((msg, exc_name))


async def _drive_supervisor_until(
    *,
    base_url: str,
    session_id: str,
    bridge_dir: Path,
    done: Callable[[_CrashLogCapture], bool],
    budget_s: float,
) -> _CrashLogCapture:
    """Run the real ``supervise_forwarder`` until *done* or *budget_s* elapses.

    The supervisor never returns on its own; it is cancelled once the predicate
    holds or the budget runs out.

    :param base_url: Server base URL the forwarder posts to.
    :param session_id: Conversation the forwarder mirrors into.
    :param bridge_dir: Seeded native Claude bridge directory.
    :param done: Early-exit predicate over the captured crash records.
    :param budget_s: Wall-clock cap for the drive.
    :returns: The crash-log capture for assertion.
    """
    from omnigent.harnesses.claude_native import forwarder as fwd

    capture = _CrashLogCapture()
    fwd_logger = logging.getLogger(fwd.__name__)
    fwd_logger.addHandler(capture)
    try:
        task = asyncio.create_task(
            fwd.supervise_forwarder(
                base_url=base_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.02,
            )
        )
        try:
            deadline = time.monotonic() + budget_s
            # The predicate makes blocking HTTP calls; keep them off the loop
            # that runs the forwarder under test.
            while time.monotonic() < deadline and not await asyncio.to_thread(done, capture):
                await asyncio.sleep(0.25)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        fwd_logger.removeHandler(capture)
    return capture


@pytest.mark.timeout(300)
def test_stale_ssl_cert_file_does_not_kill_transcript_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ``SSL_CERT_FILE`` must not crash-loop the transcript forwarder.

    Control leg: a valid bundle persists the seeded transcript, proving the
    harness. Stale leg: ``SSL_CERT_FILE`` names a missing file; the transcript
    must still be persisted with no crash-restart. On the unfixed build the
    stale leg crash-loops with ``FileNotFoundError`` and persists nothing.

    :param tmp_path: Per-test temp dir (server DB, artifacts, bridge dirs).
    :param monkeypatch: Shapes this process's env for the in-process forwarder legs.
    """
    import omnigent.util.tls as tls_module
    from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

    # Take the remote-server branch of open_server_client against the local
    # server: classify every URL as non-loopback and connect without proxies.
    monkeypatch.setattr("omnigent_client._http.is_loopback_url", lambda _url: False)
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridges: list[Path] = []
    try:
        with isolated_local_server(tmp_path) as base_url:
            # ---- Control leg: healthy SSL_CERT_FILE. Proves the harness itself
            # works so the stale-leg assertion cannot fail for environmental reasons.
            control_session = str(
                create_native_session(_http, base_url, harness="claude")["session_id"]
            )
            control_bridge = prepare_bridge_dir(control_session, workspace=workspace)
            bridges.append(control_bridge)
            _seed_conversation_transcript(control_bridge, _USER_CONTROL, _ASSISTANT_CONTROL)
            monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
            tls_module._client_ssl_context = None

            def _control_done(_cap: _CrashLogCapture) -> bool:
                return (
                    _count_marker(base_url, control_session, _USER_CONTROL) >= 1
                    and _count_marker(base_url, control_session, _ASSISTANT_CONTROL) >= 1
                )

            control_cap = asyncio.run(
                _drive_supervisor_until(
                    base_url=base_url,
                    session_id=control_session,
                    bridge_dir=control_bridge,
                    done=_control_done,
                    budget_s=_DRIVE_BUDGET_S,
                )
            )
            server_tail = (tmp_path / "server.log").read_text()[-2000:]
            assert (
                _count_marker(base_url, control_session, _USER_CONTROL) >= 1
                and _count_marker(base_url, control_session, _ASSISTANT_CONTROL) >= 1
            ), (
                "control-leg invariant: with a VALID SSL_CERT_FILE the forwarder "
                f"must persist the seeded transcript; crashes={control_cap.crashes} -- "
                f"the environment (not the bug) is broken. server log tail:\n{server_tail}"
            )

            # ---- Stale leg: SSL_CERT_FILE names a CA bundle that was rotated away.
            # Same server; only the env differs.
            bug_session = str(
                create_native_session(_http, base_url, harness="claude")["session_id"]
            )
            bug_bridge = prepare_bridge_dir(bug_session, workspace=workspace)
            bridges.append(bug_bridge)
            _seed_conversation_transcript(bug_bridge, _USER_BUG, _ASSISTANT_BUG)
            stale_ca = tmp_path / "rotated-away-ca-bundle.pem"  # never created
            assert not stale_ca.exists()
            monkeypatch.setenv("SSL_CERT_FILE", str(stale_ca))
            # The control leg cached the shared context; drop it so this leg
            # resolves trust with the stale bundle in place.
            tls_module._client_ssl_context = None

            def _bug_done(cap: _CrashLogCapture) -> bool:
                # Either the crash loop is demonstrated (>= 3 identical restarts)
                # or -- post-fix -- the transcript made it through.
                if len(cap.crashes) >= 3:
                    return True
                return (
                    _count_marker(base_url, bug_session, _USER_BUG) >= 1
                    and _count_marker(base_url, bug_session, _ASSISTANT_BUG) >= 1
                )

            bug_cap = asyncio.run(
                _drive_supervisor_until(
                    base_url=base_url,
                    session_id=bug_session,
                    bridge_dir=bug_bridge,
                    done=_bug_done,
                    budget_s=_DRIVE_BUDGET_S,
                )
            )
            user_mirrored = _count_marker(base_url, bug_session, _USER_BUG)
            assistant_mirrored = _count_marker(base_url, bug_session, _ASSISTANT_BUG)
            crash_kinds = sorted({exc for _, exc in bug_cap.crashes})

            # On the unfixed build every restart re-raises the identical
            # FileNotFoundError while building the HTTP client; nothing is persisted.
            assert user_mirrored >= 1 and assistant_mirrored >= 1, (
                "A stale SSL_CERT_FILE (missing CA bundle file) killed Claude "
                "transcript forwarding: the forwarder crash-looped "
                f"{len(bug_cap.crashes)} times (exception types: {crash_kinds}, "
                f"first: {bug_cap.crashes[0][0] if bug_cap.crashes else 'none'}) "
                "and persisted "
                f"user={user_mirrored} assistant={assistant_mirrored} of the "
                "seeded transcript items (expected >=1 each; the control leg with a "
                "valid SSL_CERT_FILE persisted both). open_server_client must not "
                "let a stale CA env var kill plain-http forwarding, and "
                "supervise_forwarder must not restart a deterministic startup crash."
            )

            # A stale bundle is tolerated at client construction, so the supervisor
            # never has anything to restart.
            assert bug_cap.crashes == [], (
                "transcript forwarding eventually worked, but the supervisor "
                f"still crash-restarted {len(bug_cap.crashes)} times on the stale "
                f"SSL_CERT_FILE (exception types: {crash_kinds})"
            )
    finally:
        for bridge in bridges:
            shutil.rmtree(bridge, ignore_errors=True)
        tls_module._client_ssl_context = None
