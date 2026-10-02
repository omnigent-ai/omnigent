"""Measure idle Claude forwarder file opens through a real local server.

The tests create a native session and seed its bridge transcript. The normal
forwarder loop must keep sys.audit-observed file opens below 3/s after settling,
both with and without a terminal ``Stop`` hook. No LLM is invoked.
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

# Local HTTP must ignore ambient egress proxies.
_http = httpx.Client(trust_env=False)

# Resolve subprocess imports from this checkout and its SDKs.
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

# At 3/s, a coarse 1 s backoff passes but the ungated 4 Hz loop fails.
_IDLE_OPEN_BUDGET_PER_S = 3.0
# Outlast startup work and the forwarder's 8 s idle gate.
_IDLE_SETTLE_S = 12.0
_STOP_SETTLE_S = 12.0
_MEASURE_WINDOW_S = 5.0

# Its arrival proves the forwarder actually processed the seeded transcript.
_MARKER_IDLE = "idle-poll-sentinel-assistant-turn"


@pytest.fixture(autouse=True)
def _idle_throttle_at_production_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any ambient kill switch before measuring the production gate."""
    monkeypatch.delenv("OMNIGENT_CLAUDE_FORWARDER_IDLE_GATE", raising=False)


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Build an isolated local-server environment for this checkout."""
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # Keep the server out of login mode despite ambient auth settings.
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    # Strip ambient credentials/config that would alter server behavior.
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
    """Create a native session using the CLI's spec and wrapper labels."""
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
    """Count committed session items containing *marker*."""
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return sum(1 for item in resp.json()["data"] if marker in json.dumps(item))


def _seed_idle_session(bridge_dir: Path, *, stopped: bool) -> Path:
    """Seed a finished exchange and optionally a terminal ``Stop`` hook."""
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


# Audit hooks cannot be removed, so count only inside an active window.
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
    """Verify the seeded turn arrived, then count settled bridge opens."""
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
        # A crashed forwarder would otherwise look like a quiet idle window.
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


@pytest.fixture(scope="module")
def idle_poll_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start the production server entrypoint with temporary SQLite storage."""
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


@pytest.mark.timeout(300)
def test_idle_session_forwarder_backs_off(idle_poll_server: str, tmp_path: Path) -> None:
    """An idle session's forwarder must not poll its bridge at 4 Hz forever."""
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


@pytest.mark.timeout(300)
def test_stopped_harness_forwarder_quiesces(idle_poll_server: str, tmp_path: Path) -> None:
    """A stopped harness's forwarder must quiesce below the idle budget."""
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
