"""E2E regression: a stale ``SSL_CERT_FILE`` must not kill Claude transcript
forwarding in a permanent crash-restart loop.

Guarded bug
-----------
On a host whose ``SSL_CERT_FILE`` points at a CA bundle file that no longer
exists (e.g. a dbcert-managed bundle that was rotated or removed), a
claude-native session against a remote Omnigent server never mirrors the Claude
transcript into the conversation store, and the runner logs::

    Claude transcript forwarder crashed; restarting in <n>.0s; session=...

forever (305 log events/day from a single session in the report), each crash the
identical ``FileNotFoundError`` raised while loading the TLS CA file.

Mechanism (the seam this drives)
--------------------------------
``cli_auth.open_server_client`` builds ``httpx.AsyncClient(trust_env=not
is_loopback_url(server_url))``. For any non-loopback server URL (every real
deployment) ``trust_env=True``, so httpx 0.28's ``create_ssl_context`` eagerly
loads ``os.environ["SSL_CERT_FILE"]`` inside ``AsyncClient.__init__`` — even
when the base URL is plain ``http`` — and raises ``FileNotFoundError`` when the
file is missing. In ``forward_claude_transcript_to_session`` the two
``open_server_client`` calls sit *outside* the loop's per-iteration
``try/except``, so the coroutine dies before its first poll;
``supervise_forwarder`` catches the crash and restarts with backoff capped at
30s, forever — the restart loop is deterministic, never classified as
permanent, and the transcript is never mirrored (the web chat view stays
permanently desynced from the running terminal). The host forwards
``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` into every spawned runner
(``_RUNNER_ENV_ALLOWLIST`` in ``omnigent/host/connect.py``), so one stale host
env poisons all runners.

Environment fidelity
--------------------
The report is against a macOS host talking to a Databricks Apps deployment
whose dbcert-managed CA bundle went stale. This test is a **stand-in**: a local
single-user ``omnigent server`` subprocess reached at
``http://[::ffff:127.0.0.1]:PORT`` — an IPv4-mapped IPv6 literal that
``is_loopback_url`` classifies as NON-loopback (so ``open_server_client`` takes
the exact production remote-server branch, ``trust_env=True``) while still
connecting to the local server. The crash itself happens before any network
I/O, so the mechanism is environment-independent; only the server/host platform
is substituted.

This drives the REAL user path: a real ``omnigent server`` subprocess, a real
claude-native session (production spec materializer + wrapper labels), a real
bridge dir (``prepare_bridge_dir``), and the real ``supervise_forwarder`` — the
exact supervisor from the reported stack — tailing a seeded Claude JSONL
transcript.

Desired behavior (asserted): the seeded transcript reaches the conversation
store even though ``SSL_CERT_FILE`` points at a missing file — a stale CA env
var affects at most TLS verification and must not kill forwarding to the
server (falling back to the default trust roots, as
``omnigent.util.tls.resolve_ca_file`` already does elsewhere), and the
supervisor must not re-crash forever on a deterministic startup error. Buggy
behavior: the forwarder crash-loops with the identical ``FileNotFoundError``
and mirrors nothing — this test FAILS with the observed crash-restart count in
the message.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_forwarder_tls_ca_restart_loop_e2e.py -v

No ``--llm-api-key`` / ``--profile`` needed -- no LLM is invoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
_http = httpx.Client(trust_env=False)

# The spawned server resolves worktree imports from the repo root and the SDKs.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# Plain server launch -- the bug lives entirely in the forwarder process's env
# handling; the server's real commit path is intact.
_SERVER_BOOTSTRAP = "from omnigent.cli import main\n\nmain()\n"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5

# Per-leg forwarder drive budget. The buggy leg exits early once the crash
# loop is demonstrated (>= 3 identical restarts, reached ~3s in); the fixed
# leg exits early once both markers are mirrored (~1-2s in).
_DRIVE_BUDGET_S = 20.0

_USER_CONTROL = "marker-user-control-healthy-ca"
_ASSISTANT_CONTROL = "marker-assistant-control-healthy-ca"
_USER_BUG = "marker-user-stale-ca"
_ASSISTANT_BUG = "marker-assistant-stale-ca"

_CRASH_LOG_PREFIX = "Claude transcript forwarder crashed"


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy/credentials in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # Header auth + single-user keeps the spawned server out of login
        # mode; ambient auth/OIDC vars would otherwise 401 every call.
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    # Strip ambient credentials/config that would alter server behaviour.
    for name in list(env):
        if (
            name.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or name.endswith("_SECRET")
            or name
            in (
                "ANTHROPIC_API_KEY",
                "OMNIGENT_AUTH_ENABLED",
                "OMNIGENT_RUNNER_TUNNEL_TOKEN",
            )
        ):
            env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the created session is a real
    claude-native conversation -- the kind whose transcript the forwarder
    mirrors in production.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat translator
        # (the wrapper spec has no ``spec_version``).
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _seed_conversation_transcript(
    bridge_dir: Path, user_marker: str, assistant_marker: str
) -> Path:
    """Write a one-turn Claude JSONL transcript + a Stop hook.

    Each record uses the shape a live Claude CLI writes: the ``type=user``
    record carries ``message.role == "user"`` with plain-string content and a
    distinct ``uuid`` (the forwarder's idempotency key); the ``type=assistant``
    record carries a text content block. A recorded ``Stop`` hook reports the
    transcript path so the loop resolves it on the first poll.

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
    """Count committed conversation items whose payload contains *marker*.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation to query.
    :param marker: Substring to match against each item's serialized data.
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
    """Capture the supervisor's crash-restart ERROR records.

    Collects ``(message, exception_type_name)`` for every
    ``"Claude transcript forwarder crashed; restarting in ..."`` record so the
    test can count identical restarts and name the crash exception.
    """

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

    This is the exact production supervisor from the reported stack (the
    runner awaits it in-process), driven with the same arguments the runner
    passes. The supervisor never returns on its own; it is cancelled when the
    predicate is satisfied or the budget runs out.

    :param base_url: Server base URL the forwarder posts to (the non-loopback
        remote-style URL -- the ``trust_env=True`` branch).
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
            while time.monotonic() < deadline and not done(capture):
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

    Journey (the reporter's): the host machine's ``SSL_CERT_FILE`` points at a
    CA bundle file that no longer exists (a rotated/removed dbcert bundle); the
    user launches a claude-native session against a remote Omnigent server. The
    terminal works, but the web chat view never mirrors the Claude transcript,
    and the runner logs ``Claude transcript forwarder crashed; restarting in
    <n>.0s`` every backoff interval, forever.

    Expected: the transcript reaches the conversation store despite the stale
    CA env var (which affects at most TLS verification -- the base URL here is
    plain http), and the supervisor does not re-crash forever on a
    deterministic startup error. Buggy behavior: ``open_server_client`` honors
    the stale ``SSL_CERT_FILE`` (``trust_env=True`` for every non-loopback
    URL), ``httpx.AsyncClient.__init__`` raises ``FileNotFoundError`` before
    the first poll, and ``supervise_forwarder`` restarts the identical crash
    forever -- nothing is ever mirrored, and this test FAILS with the observed
    crash-restart count.

    :param tmp_path: Per-test temp dir (server DB, artifacts, bridge dirs).
    :param monkeypatch: Used to shape this process's env for the in-process
        forwarder legs (proxy vars removed; ``SSL_CERT_FILE`` per leg).
    """
    from omnigent_client._http import is_loopback_url

    port = _find_free_port()
    local_url = f"http://127.0.0.1:{port}"
    # An IPv4-mapped IPv6 literal connects to the local IPv4 listener but is
    # classified NON-loopback, so open_server_client takes the production
    # remote-server branch (trust_env=True) that honors SSL_CERT_FILE.
    remote_style_url = f"http://[::ffff:127.0.0.1]:{port}"
    assert not is_loopback_url(remote_style_url), (
        "precondition: the remote-style URL must classify as non-loopback so "
        "open_server_client sets trust_env=True (the reported deployment "
        "branch); is_loopback_url now classifies IPv4-mapped IPv6 loopback as "
        "loopback on this interpreter -- pick another non-loopback alias for "
        "the local server"
    )

    # The in-process forwarder legs must connect directly (trust_env=True
    # would otherwise route the non-loopback URL through any ambient proxy).
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)

    db_path = tmp_path / "chat.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridges: list[Path] = []

    server_log = (tmp_path / "server.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SERVER_BOOTSTRAP,
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{db_path}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{local_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

        # ---- Control leg: healthy SSL_CERT_FILE at the same non-loopback URL.
        # Proves the harness itself works (server reachable, forwarder mirrors)
        # so the bug assertion below cannot fail for environmental reasons.
        control_session = _create_claude_native_session(local_url)
        control_bridge = prepare_bridge_dir(control_session, workspace=workspace)
        bridges.append(control_bridge)
        _seed_conversation_transcript(control_bridge, _USER_CONTROL, _ASSISTANT_CONTROL)
        monkeypatch.setenv("SSL_CERT_FILE", certifi.where())

        def _control_done(_cap: _CrashLogCapture) -> bool:
            return (
                _count_marker(local_url, control_session, _USER_CONTROL) >= 1
                and _count_marker(local_url, control_session, _ASSISTANT_CONTROL) >= 1
            )

        control_cap = asyncio.run(
            _drive_supervisor_until(
                base_url=remote_style_url,
                session_id=control_session,
                bridge_dir=control_bridge,
                done=_control_done,
                budget_s=_DRIVE_BUDGET_S,
            )
        )
        server_tail = (tmp_path / "server.log").read_text()[-2000:]
        assert (
            _count_marker(local_url, control_session, _USER_CONTROL) >= 1
            and _count_marker(local_url, control_session, _ASSISTANT_CONTROL) >= 1
        ), (
            "control-leg invariant: with a VALID SSL_CERT_FILE the forwarder "
            "must mirror the seeded transcript through the non-loopback URL; "
            f"crashes={control_cap.crashes} -- the environment (not the bug) "
            f"is broken. server log tail:\n{server_tail}"
        )

        # ---- Bug leg: SSL_CERT_FILE points at a file that no longer exists
        # (the rotated/removed CA bundle from the report). Same server, same
        # non-loopback URL -- only the env differs.
        bug_session = _create_claude_native_session(local_url)
        bug_bridge = prepare_bridge_dir(bug_session, workspace=workspace)
        bridges.append(bug_bridge)
        _seed_conversation_transcript(bug_bridge, _USER_BUG, _ASSISTANT_BUG)
        stale_ca = tmp_path / "rotated-away-ca-bundle.pem"  # never created
        assert not stale_ca.exists()
        monkeypatch.setenv("SSL_CERT_FILE", str(stale_ca))

        def _bug_done(cap: _CrashLogCapture) -> bool:
            # Either the crash loop is demonstrated (>= 3 identical restarts)
            # or -- post-fix -- the transcript made it through.
            if len(cap.crashes) >= 3:
                return True
            return (
                _count_marker(local_url, bug_session, _USER_BUG) >= 1
                and _count_marker(local_url, bug_session, _ASSISTANT_BUG) >= 1
            )

        bug_cap = asyncio.run(
            _drive_supervisor_until(
                base_url=remote_style_url,
                session_id=bug_session,
                bridge_dir=bug_bridge,
                done=_bug_done,
                budget_s=_DRIVE_BUDGET_S,
            )
        )
        user_mirrored = _count_marker(local_url, bug_session, _USER_BUG)
        assistant_mirrored = _count_marker(local_url, bug_session, _ASSISTANT_BUG)
        crash_kinds = sorted({exc for _, exc in bug_cap.crashes})

        # The bug: with a stale SSL_CERT_FILE the forwarder never comes up --
        # every restart re-raises the identical FileNotFoundError while
        # building its HTTP client, so the transcript is never mirrored and
        # the chat view stays permanently desynced from the terminal.
        assert user_mirrored >= 1 and assistant_mirrored >= 1, (
            "A stale SSL_CERT_FILE (missing CA bundle file) killed Claude "
            "transcript forwarding: the forwarder crash-looped "
            f"{len(bug_cap.crashes)} times (exception types: {crash_kinds}, "
            f"first: {bug_cap.crashes[0][0] if bug_cap.crashes else 'none'}) "
            "and mirrored "
            f"user={user_mirrored} assistant={assistant_mirrored} of the "
            "seeded transcript items into the conversation store (expected "
            ">=1 each; the control leg with a valid SSL_CERT_FILE mirrored "
            "both). open_server_client must not let a stale CA env var kill "
            "plain-http forwarding, and supervise_forwarder must not restart "
            "a deterministic startup crash forever."
        )

        # Restart-loop containment: once forwarding works, the supervisor must
        # not have burned through repeated identical startup crashes first.
        assert len(bug_cap.crashes) <= 1, (
            "transcript forwarding eventually worked, but the supervisor "
            f"still crash-restarted {len(bug_cap.crashes)} times on the stale "
            f"SSL_CERT_FILE (exception types: {crash_kinds}) -- the restart "
            "loop from the report is still present"
        )
    finally:
        _terminate(server_proc)
        server_log.close()
        for bridge in bridges:
            shutil.rmtree(bridge, ignore_errors=True)
