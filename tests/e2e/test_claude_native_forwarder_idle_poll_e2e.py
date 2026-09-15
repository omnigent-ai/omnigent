"""E2E regression test: an idle claude-native session's transcript forwarder
must not keep re-reading its bridge at the full fixed poll rate forever.

``forward_claude_transcript_to_session`` polls every 0.25 s per session,
indefinitely, at a fixed cadence. Each tick re-opens the bridge hook-state
JSON, re-derives the transcript path, re-reads the transcript tail, and runs
the full set of forwarding scans even when nothing about the session has
changed. The supervisor's backoff covers only crash restarts -- nothing
throttles steady-state polling on a fully idle session -- so CPU burn scales
with the number of accumulated sessions rather than with work being done
(py-spy on production runners attributed 29-46% of on-CPU Python time to this
loop with every session idle, dominated by pathlib open/stat).

Two facets, one test each:

A. **Idle session, no backoff**: a launched-and-idle session whose transcript
   never changes keeps being polled at the full fixed rate, re-opening its
   bridge files many times per second indefinitely.
B. **Stopped harness never quiesces**: a session whose recorded hook state is
   terminal (``last_hook_event_name: "Stop"``) is polled at the same full
   rate forever -- the stopped harness's forwarder neither backs off nor
   tears down.

Both tests drive the REAL user path end to end: a real ``omnigent server``
subprocess, a real claude-native session created exactly like ``omnigent
claude`` creates one, a bridge directory prepared by the production
``prepare_bridge_dir``, hook state recorded through the production
``record_hook_event``, and the real ``forward_claude_transcript_to_session``
loop running at its production default poll interval. The measured signal is
the sys.audit-observed rate of file opens under the bridge directory during a
window in which the session is fully idle (nothing changes on disk and no
turn is running).

Desired behavior (asserted): once nothing is changing, the forwarder settles
at or below ``_IDLE_OPEN_BUDGET_PER_S`` file opens per second (a change-gated
or backed-off implementation sits near zero). Buggy behavior: the fixed
0.25 s cadence re-opens bridge files ~10-30x per second, so these tests FAIL
with the measured rate in the message.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_forwarder_idle_poll_e2e.py -v

No ``--llm-api-key`` or ``--profile`` needed -- no LLM is invoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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

_SERVER_BOOTSTRAP = "from omnigent.cli import main\n\nmain()\n"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5

# Steady-state budget for bridge file opens while a session is fully idle.
# The production poll loop at its fixed 0.25s cadence opens the hook-state
# JSON, the hooks log, and the transcript every tick (>= 12 opens/s); an
# implementation with idle backoff or a change gate sits near zero. 3/s
# tolerates a coarse (1s) backoff cadence without passing the fixed 4 Hz loop.
_IDLE_OPEN_BUDGET_PER_S = 3.0
# Settle window before measuring: absorbs the loop's startup work (initial
# transcript forwarding, the conversation PATCH, first status sync) AND
# outlasts any reasonable idle-throttle's own settle window (the shipped
# change gate waits 8 s of unchanged inputs before engaging). The unfixed
# fixed-rate poller fails identically at any settle length.
_IDLE_SETTLE_S = 12.0
# A stopped harness gets the same long settle so a quiescing/tearing-down
# implementation has ample time to engage before the idle window is judged.
_STOP_SETTLE_S = 12.0
_MEASURE_WINDOW_S = 5.0

# Sentinel forwarded from the seeded transcript; its arrival on the server
# proves the real loop engaged with this bridge before the idle window.
_MARKER_IDLE = "idle-poll-sentinel-assistant-turn"


@pytest.fixture(autouse=True)
def _idle_throttle_at_production_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any idle-throttle kill switch: the loop under test runs in this
    process, so an ambient value would measure the ungated loop instead."""
    monkeypatch.delenv("OMNIGENT_CLAUDE_FORWARDER_IDLE_GATE", raising=False)


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
    # Strip ambient credentials/config that would alter server behaviour:
    # any Databricks or OIDC setting, any cookie/signing secret, and the
    # specific provider/tunnel vars below.
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


def _seed_idle_session(bridge_dir: Path, *, stopped: bool) -> Path:
    """Seed one finished exchange and the hook state of an idle session.

    Mirrors the bridge state a real session leaves behind after a turn: a
    Claude JSONL transcript with one user + one assistant entry, and hook
    state recorded through the production ``record_hook_event`` -- a
    ``SessionStart`` (which pins the identity and transcript path), plus a
    terminal ``Stop`` when *stopped* is set.

    :param bridge_dir: Native Claude bridge directory.
    :param stopped: Also record a terminal ``Stop`` hook, leaving
        ``last_hook_event_name: "Stop"`` in the bridge state.
    :returns: The transcript path.
    """
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"
    lines = [
        {
            "type": "user",
            "uuid": "user-idle-poll-1",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "say hi and stop"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "assistant-idle-poll-1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": _MARKER_IDLE}],
            },
        },
    ]
    transcript_path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session-idle-poll",
            "transcript_path": str(transcript_path),
        },
    )
    if stopped:
        record_hook_event(
            bridge_dir,
            {
                "hook_event_name": "Stop",
                "session_id": "claude-session-idle-poll",
                "transcript_path": str(transcript_path),
            },
        )
    return transcript_path


# ---------------------------------------------------------------------------
# File-open measurement via sys.audit
# ---------------------------------------------------------------------------

# Registered measurement windows; the process-wide audit hook (which cannot be
# removed once installed) only counts while a window is active.
_OPEN_COUNTERS: list[dict[str, Any]] = []
_AUDIT_HOOK_INSTALLED = False


def _install_audit_hook() -> None:
    """Install the process-wide ``open`` audit hook exactly once."""
    global _AUDIT_HOOK_INSTALLED
    if _AUDIT_HOOK_INSTALLED:
        return

    def _on_audit(event: str, args: tuple[Any, ...]) -> None:
        if event != "open" or not _OPEN_COUNTERS:
            return
        raw = args[0] if args else None
        if isinstance(raw, os.PathLike):
            raw = os.fspath(raw)
        if isinstance(raw, bytes):
            try:
                raw = os.fsdecode(raw)
            except (UnicodeDecodeError, ValueError):
                return
        if not isinstance(raw, str):
            return
        for counter in _OPEN_COUNTERS:
            if counter["active"] and raw.startswith(counter["prefixes"]):
                counter["total"] += 1
                by_path = counter["by_path"]
                by_path[raw] = by_path.get(raw, 0) + 1

    sys.addaudithook(_on_audit)
    _AUDIT_HOOK_INSTALLED = True


async def _measure_idle_open_rate(
    *,
    base_url: str,
    session_id: str,
    bridge_dir: Path,
    settle_s: float,
) -> tuple[float, dict[str, int]]:
    """Run the real forwarder and measure idle-window bridge file opens.

    Starts the production ``forward_claude_transcript_to_session`` loop at its
    default poll interval, lets it settle for *settle_s* (initial transcript
    forwarding, conversation PATCH, first status sync), verifies it genuinely
    engaged with this bridge (the seeded sentinel reached the server), then
    counts file opens under the bridge directory for ``_MEASURE_WINDOW_S``
    while nothing changes.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation the forwarder mirrors into.
    :param bridge_dir: Seeded native Claude bridge directory.
    :param settle_s: Warmup before the measured idle window.
    :returns: ``(opens_per_second, per_path_open_counts)``.
    """
    import omnigent.harnesses.claude_native.forwarder as fwd

    _install_audit_hook()
    counter: dict[str, Any] = {
        "active": False,
        "prefixes": (str(bridge_dir), str(bridge_dir.resolve())),
        "total": 0,
        "by_path": {},
    }
    _OPEN_COUNTERS.append(counter)
    task = asyncio.create_task(
        fwd.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
        )
    )
    try:
        await asyncio.sleep(settle_s)
        # Sanity: the real loop engaged with this bridge (the rate assertion
        # would be vacuous if the forwarder crashed or never resolved the
        # transcript -- both would masquerade as a quiet idle window).
        if task.done():
            raise AssertionError(f"forwarder loop exited during warmup: {task.exception()!r}")
        forwarded = _count_marker(base_url, session_id, _MARKER_IDLE)
        assert forwarded >= 1, (
            "forwarder never delivered the seeded transcript sentinel; the "
            "idle-window measurement would be vacuous"
        )
        counter["total"] = 0
        counter["by_path"] = {}
        counter["active"] = True
        started = time.monotonic()
        await asyncio.sleep(_MEASURE_WINDOW_S)
        counter["active"] = False
        elapsed = time.monotonic() - started
        if task.done():
            raise AssertionError(
                f"forwarder loop exited during the idle window: {task.exception()!r}"
            )
        return counter["total"] / elapsed, dict(counter["by_path"])
    finally:
        _OPEN_COUNTERS.remove(counter)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Fixture: minimal Omnigent server subprocess (shared by both facets)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def idle_poll_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start a minimal Omnigent server subprocess; yield its base URL.

    Self-contained: does not use the session-scoped ``live_server`` fixture
    (which needs ``--llm-api-key``). Uses the same ``omnigent.cli server``
    entrypoint the production server uses, backed by a throw-away SQLite DB.

    :yields: Base URL, e.g. ``"http://127.0.0.1:<port>"``.
    """
    tmp = tmp_path_factory.mktemp("idle-poll-server")
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_handle = (tmp / "server.log").open("w")
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
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
                f"sqlite:///{tmp / 'chat.db'}",
                "--artifact-location",
                str(tmp / "artifacts"),
            ],
            env=_localhost_env({}),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)
        yield base_url
    finally:
        _terminate(proc)
        log_handle.close()


# ---------------------------------------------------------------------------
# Facet A: idle session, no backoff
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_idle_session_forwarder_backs_off(idle_poll_server: str, tmp_path: Path) -> None:
    """A fully idle session's forwarder must not poll its bridge at 4 Hz forever.

    Journey (the reporter's): start a claude-native session, run one exchange,
    and leave the session idle -- no turn running, nothing changing on disk.
    The forwarder keeps re-opening the bridge hook-state JSON, the hooks log,
    and the transcript every 0.25 s indefinitely, burning CPU on every idle
    session a runner has accumulated.

    Expected: after the settle window, an idle session's steady-state bridge
    open rate is at or below ``_IDLE_OPEN_BUDGET_PER_S``. Buggy behavior: the
    fixed-cadence loop measures ~10-30 opens/s and this test FAILS with the
    measured rate.

    :param idle_poll_server: Live server base URL.
    :param tmp_path: Per-test temp dir (session workspace).
    """
    session_id = _create_claude_native_session(idle_poll_server)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

    bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)
    try:
        _seed_idle_session(bridge_dir, stopped=False)
        rate, by_path = asyncio.run(
            _measure_idle_open_rate(
                base_url=idle_poll_server,
                session_id=session_id,
                bridge_dir=bridge_dir,
                settle_s=_IDLE_SETTLE_S,
            )
        )
        assert rate <= _IDLE_OPEN_BUDGET_PER_S, (
            "An idle claude-native session's forwarder re-opened its bridge "
            f"files at {rate:.1f} opens/s (budget "
            f"{_IDLE_OPEN_BUDGET_PER_S:.1f}/s) during a "
            f"{_MEASURE_WINDOW_S:.0f}s window in which nothing changed: the "
            "poll loop runs at a fixed 0.25s cadence with no idle backoff, so "
            "CPU burn scales with accumulated idle sessions. Per-file opens: "
            f"{sorted(by_path.items())}"
        )
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Facet B: stopped harness never quiesces
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_stopped_harness_forwarder_quiesces(idle_poll_server: str, tmp_path: Path) -> None:
    """A stopped harness's forwarder must quiesce, not poll at full rate forever.

    Journey (the reporter's): a claude-native session finishes its turn and
    the harness stops -- the bridge hook state records a terminal
    ``last_hook_event_name: "Stop"``. Live-locals capture on production
    runners showed the forwarder still polling such sessions at the full
    0.25 s cadence indefinitely.

    Expected: once the harness is stopped and nothing is changing, the
    forwarder's bridge open rate settles at or below
    ``_IDLE_OPEN_BUDGET_PER_S`` (whether by backoff or teardown). Buggy
    behavior: the fixed-cadence loop measures ~10-30 opens/s and this test
    FAILS with the measured rate.

    :param idle_poll_server: Live server base URL.
    :param tmp_path: Per-test temp dir (session workspace).
    """
    session_id = _create_claude_native_session(idle_poll_server)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

    bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)
    try:
        _seed_idle_session(bridge_dir, stopped=True)
        rate, by_path = asyncio.run(
            _measure_idle_open_rate(
                base_url=idle_poll_server,
                session_id=session_id,
                bridge_dir=bridge_dir,
                settle_s=_STOP_SETTLE_S,
            )
        )
        assert rate <= _IDLE_OPEN_BUDGET_PER_S, (
            "A stopped claude-native harness's forwarder re-opened its bridge "
            f"files at {rate:.1f} opens/s (budget "
            f"{_IDLE_OPEN_BUDGET_PER_S:.1f}/s) during a "
            f"{_MEASURE_WINDOW_S:.0f}s window after the terminal Stop hook: "
            "the poll loop neither backs off nor tears down when the harness "
            "has stopped. Per-file opens: "
            f"{sorted(by_path.items())}"
        )
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)
