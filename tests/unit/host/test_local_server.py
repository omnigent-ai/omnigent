"""Tests for the persistent background local Omnigent server helpers.

Covers ``omnigent.host.local_server``: reuse-vs-respawn detection
(:func:`local_server_url_if_healthy`) and the spawn wiring
(:func:`ensure_local_omnigent_server`). The connect daemon owns this server in
``--local`` mode; the CLI discovers it via the pidfile these helpers write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import pytest

from omnigent.host import local_server


def test_local_server_url_if_healthy_returns_url_when_alive_and_healthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live PID plus a 200 ``/health`` yields the reusable URL.

    Both conditions must hold: the recorded process is alive and the
    server answers health. This is the fast-reuse path that avoids
    spawning a second server per invocation.
    """
    pid_file = tmp_path / "local_server.pid"
    pid_file.write_text("4242\n8123\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    monkeypatch.setattr(local_server, "_pid_alive", lambda pid: pid == 4242)

    health_targets: list[str] = []

    class _Resp:
        status_code = 200

    def _fake_get(url: str, *, timeout: float) -> _Resp:
        health_targets.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.get", _fake_get)

    assert local_server.local_server_url_if_healthy() == "http://127.0.0.1:8123"
    # The probe must hit the recorded port's /health, proving the port from
    # the pidfile (not a hardcoded default) was used.
    assert health_targets == ["http://127.0.0.1:8123/health"]


def test_local_server_url_if_healthy_none_when_pid_dead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A dead recorded PID returns ``None`` without probing health.

    A stale pidfile (process gone) must not be reused. Health is never
    probed because the liveness check short-circuits first; the
    unconditional-raise stub below would fail the test if it were.
    """
    pid_file = tmp_path / "local_server.pid"
    pid_file.write_text("4242\n8123\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    monkeypatch.setattr(local_server, "_pid_alive", lambda pid: False)

    def _must_not_probe(url: str, *, timeout: float) -> Any:
        raise AssertionError("health probed despite dead PID")

    monkeypatch.setattr("httpx.get", _must_not_probe)

    assert local_server.local_server_url_if_healthy() is None


def test_local_server_url_if_healthy_none_when_no_pidfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No pidfile at all returns ``None`` (nothing to reuse)."""
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", tmp_path / "absent.pid")
    assert local_server.local_server_url_if_healthy() is None


def test_ensure_local_omnigent_server_reuses_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A healthy existing server with a matching config sig is reused.

    Reuse requires both health AND a config signature matching this
    invocation's. With the matching sig stamped, ``ensure_local_omnigent_server``
    must return the healthy URL without falling through to
    ``subprocess.Popen`` (the stub below fails the test if it spawns).
    """
    monkeypatch.setattr(
        local_server, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8123"
    )
    # Stamp the sidecar with the signature this invocation will compute, so
    # the reuse path sees a config match.
    sig_file = tmp_path / "local_server.sig"
    sig_file.write_text(local_server.server_config_signature() + "\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_LOG_REF_PATH", tmp_path / "local_server.logpath"
    )

    def _must_not_popen(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("spawned a new server despite a healthy one existing")

    monkeypatch.setattr(local_server.subprocess, "Popen", _must_not_popen)

    result = local_server.ensure_local_omnigent_server()
    assert result.url == "http://127.0.0.1:8123"
    # Reused an existing healthy server — did not start a new process.
    assert result.spawned is False


def test_ensure_local_omnigent_server_respawns_on_config_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A healthy server with a stale config sig is stopped and respawned.

    This is the auth-drift fix: when the running server was spawned under a
    different auth source (its sidecar sig differs from this invocation's),
    reuse must NOT happen — the old server is stopped and a fresh one
    spawned so the new config takes effect.
    """
    monkeypatch.setattr(
        local_server, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8123"
    )
    # Sidecar holds a signature that does not match this invocation's.
    sig_file = tmp_path / "local_server.sig"
    sig_file.write_text("stale-signature-0000\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_LOG_REF_PATH", tmp_path / "local_server.logpath"
    )
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", tmp_path / "local_server.pid")
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=8000: 8766)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    stopped: list[bool] = []
    monkeypatch.setattr(local_server, "stop_local_omnigent_server", lambda: stopped.append(True))

    spawned: list[bool] = []

    class _Proc:
        pid = 9100

        def __init__(self, args: list[str], *, env: dict[str, str], **_kwargs: object) -> None:
            spawned.append(True)

        def poll(self) -> None:
            return None

    monkeypatch.setattr(local_server.subprocess, "Popen", _Proc)
    monkeypatch.setattr(
        local_server,
        "_wait_for_local_omnigent_server",
        lambda base_url, proc, log_path, timeout=45.0: None,
    )
    # Ownership probe confirms our own child holds the port (no contention).
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: 9100)

    result = local_server.ensure_local_omnigent_server()

    assert result.url == "http://127.0.0.1:8766"
    # A config-drift respawn started a fresh server, so it is ours.
    assert result.spawned is True
    # The stale server was stopped before the fresh one spawned.
    assert stopped == [True]
    assert spawned == [True]
    # The fresh server's sidecar carries this invocation's signature.
    assert (tmp_path / "local_server.sig").read_text().strip() == (
        local_server.server_config_signature()
    )


def test_server_config_signature_changes_with_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package-version bump changes the signature (auth held constant).

    This is what makes ``omni upgrade`` (and a manual ``uv tool upgrade``)
    cycle a running local server: the recorded signature no longer matches
    the upgraded CLI's, so ``ensure_local_omnigent_server`` respawns it on
    the new code through the existing config-drift path.
    """
    import importlib.metadata

    from omnigent.server import auth as auth_mod

    # Pin auth so only the version varies between the two signatures.
    monkeypatch.setattr(auth_mod, "resolve_auth_source", lambda: "noauth")

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.0.0")
    sig_old = local_server.server_config_signature()

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.0.1")
    sig_new = local_server.server_config_signature()

    assert sig_old != sig_new
    # Same version → stable signature (no spurious respawns on every call).
    assert sig_new == local_server.server_config_signature()


def test_ensure_local_omnigent_server_spawns_when_none_healthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no healthy server, spawn one on a loopback port and record it.

    Verifies the spawn path: a free port is bound, the server subprocess is
    launched as ``omnigent server`` on 127.0.0.1, the pidfile records
    ``pid\\nport``, and the chosen port is returned.
    The readiness poll is stubbed so the test does not depend on a real boot.
    """
    monkeypatch.setattr(local_server, "local_server_url_if_healthy", lambda: None)
    # ensure_local_omnigent_server picks the port via pick_local_port (prefers :8000,
    # falls back to a free one). Stub it to a fixed port so the assertion is
    # deterministic regardless of whether :8000 happens to be free on the box.
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=8000: 8765)
    pid_file = tmp_path / "local_server.pid"
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    sig_file = tmp_path / "local_server.sig"
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_LOG_REF_PATH", tmp_path / "local_server.logpath"
    )
    # Point the persistent data dir at tmp so the test does not write to the
    # developer's real ~/.omnigent.
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    # The spawned server inherits the parent env unmodified — there is no
    # profile flag anymore, so an ambient DATABRICKS_CONFIG_PROFILE must
    # pass through to the server env as-is (asserted below).
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "ambient")

    captured: dict[str, object] = {}

    class _Proc:
        pid = 9001

        def __init__(self, args: list[str], *, env: dict[str, str], **_kwargs: object) -> None:
            captured["args"] = args
            captured["env"] = env

        def poll(self) -> None:
            return None

    monkeypatch.setattr(local_server.subprocess, "Popen", _Proc)
    # Skip the real health poll; we assert on spawn wiring, not boot.
    monkeypatch.setattr(
        local_server,
        "_wait_for_local_omnigent_server",
        lambda base_url, proc, log_path, timeout=45.0: None,
    )
    # Ownership probe confirms our own child holds the port (no contention).
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: 9001)

    result = local_server.ensure_local_omnigent_server()

    assert result.url == "http://127.0.0.1:8765"
    # Spawned a fresh server (none was healthy) — reported as ours.
    assert result.spawned is True
    args = captured["args"]
    assert isinstance(args, list)
    assert "server" in args
    assert "127.0.0.1" in args
    assert "8765" in args
    # Pidfile records PID then port — the contract _read_local_server_pid_file
    # parses; a wrong order silently breaks reuse on the next run.
    assert pid_file.read_text() == "9001\n8765\n"
    # The config-signature sidecar is stamped so a later differently-configured
    # invocation respawns instead of reusing this server.
    assert sig_file.read_text().strip() == local_server.server_config_signature()
    env = captured["env"]
    assert isinstance(env, dict)
    # Ambient passthrough, no injection: the spawned server sees the
    # shell's own DATABRICKS_CONFIG_PROFILE, untouched.
    assert env["DATABRICKS_CONFIG_PROFILE"] == "ambient"


def test_stop_local_omnigent_server_waits_for_process_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``stop_local_omnigent_server`` blocks until the server process dies.

    The bug: fire-and-forget SIGTERM left the port bound until the OS
    reaped the process, so the next ``ensure_local_omnigent_server`` or
    ``omnigent server`` failed to bind. The fix polls ``_pid_alive``
    until the process exits. This test verifies the poll loop runs and
    that both the pidfile and sig sidecar are cleaned up.
    """
    pid_file = tmp_path / "local_server.pid"
    sig_file = tmp_path / "local_server.sig"
    pid_file.write_text("7777\n8000\n")
    sig_file.write_text("somesig\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_LOG_REF_PATH", tmp_path / "local_server.logpath"
    )

    kill_signals: list[int] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kill_signals.append(sig)

    monkeypatch.setattr(local_server.os, "kill", _fake_kill)

    # Simulate the process dying after 2 alive checks.
    alive_calls = 0

    def _fake_pid_alive(pid: int) -> bool:
        nonlocal alive_calls
        alive_calls += 1
        # First two checks: alive (pre-SIGTERM guard + first poll iteration).
        # Third check: dead.
        return alive_calls <= 2

    monkeypatch.setattr(local_server, "_pid_alive", _fake_pid_alive)
    # Eliminate real sleeps — the poll interval is irrelevant in the test.
    monkeypatch.setattr(local_server.time, "sleep", lambda _s: None)

    local_server.stop_local_omnigent_server()

    import signal as signal_mod

    # SIGTERM was sent exactly once — the process exited before the
    # grace period, so SIGKILL was never needed.
    assert kill_signals == [signal_mod.SIGTERM], (
        f"Expected a single SIGTERM, got {kill_signals}. "
        f"If SIGKILL is present, the poll loop didn't detect the process exit."
    )
    # alive_calls >= 3 proves the poll loop ran (not just fire-and-forget).
    assert alive_calls >= 3, (
        f"_pid_alive called {alive_calls} time(s) — expected ≥3 "
        f"(guard + poll + exit-detect). If 1, the wait loop was skipped."
    )
    # Pidfile and sig sidecar cleaned up.
    assert not pid_file.exists()
    assert not sig_file.exists()


def test_stop_local_omnigent_server_escalates_to_sigkill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Grace period exhaustion escalates to SIGKILL so the port is freed.

    A wedged server that ignores SIGTERM must be force-killed, otherwise
    the port stays bound indefinitely. The test stubs ``time.monotonic``
    to simulate the grace period expiring, then verifies SIGKILL is sent.
    """
    pid_file = tmp_path / "local_server.pid"
    sig_file = tmp_path / "local_server.sig"
    pid_file.write_text("8888\n8000\n")
    sig_file.write_text("sig\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_LOG_REF_PATH", tmp_path / "local_server.logpath"
    )

    kill_signals: list[int] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kill_signals.append(sig)

    monkeypatch.setattr(local_server.os, "kill", _fake_kill)

    # Process stays alive through SIGTERM grace period, dies after SIGKILL.
    def _fake_pid_alive(pid: int) -> bool:
        import signal as signal_mod

        # Die only after SIGKILL has been sent.
        return signal_mod.SIGKILL not in kill_signals

    monkeypatch.setattr(local_server, "_pid_alive", _fake_pid_alive)

    # Fast-forward time so the grace period expires immediately.
    clock = [0.0]

    def _fake_monotonic() -> float:
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(local_server.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(local_server.time, "sleep", lambda _s: None)

    local_server.stop_local_omnigent_server()

    import signal as signal_mod

    # Both SIGTERM and SIGKILL must have been sent, in that order.
    assert signal_mod.SIGTERM in kill_signals, "SIGTERM was never sent"
    assert signal_mod.SIGKILL in kill_signals, (
        "SIGKILL was never sent — the grace period expiry didn't escalate"
    )
    assert kill_signals.index(signal_mod.SIGTERM) < kill_signals.index(signal_mod.SIGKILL), (
        "SIGKILL was sent before SIGTERM — escalation order is wrong"
    )
    assert not pid_file.exists()
    assert not sig_file.exists()


def test_local_data_dir_honors_data_dir_not_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_local_data_dir`` isolates the runtime DB via ``OMNIGENT_DATA_DIR`` only.

    Two worktrees sharing ``~/.omnigent/chat.db`` with divergent Alembic
    heads can't migrate the shared DB, so the daemon-backed server fails to
    boot. ``OMNIGENT_DATA_DIR`` is the purpose-built data-isolation knob.
    ``OMNIGENT_CONFIG_HOME`` MUST NOT move the DB — it isolates config only;
    overloading it broke HOME-based data isolation (the resumption e2e tests
    set ``HOME`` to control the DB while inheriting a shared CONFIG_HOME).
    """
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    # Default: ~/.omnigent.
    assert local_server._local_data_dir() == Path.home() / ".omnigent"
    # CONFIG_HOME does NOT move the data dir — a failure here means config
    # isolation is leaking back into data-dir selection.
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "cfg"))
    assert local_server._local_data_dir() == Path.home() / ".omnigent"
    # DATA_DIR is the data-isolation knob.
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    assert local_server._local_data_dir() == tmp_path / "data"


def test_pick_local_port_returns_preferred_when_free() -> None:
    """``pick_local_port`` returns the preferred port when it's bindable.

    The local server prefers a stable port (8000) so its URL is identical
    across ``omnigent server`` and daemon spawns. We use an
    OS-assigned free port as ``preferred`` here so the assertion is
    deterministic regardless of what's already bound on the host (8000
    is often busy on shared CI boxes).
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = int(probe.getsockname()[1])
    # Socket closed: the port is now free, so pick must hand it straight back.

    assert local_server.pick_local_port(free_port) == free_port


def test_pick_local_port_falls_back_when_preferred_taken() -> None:
    """A bound preferred port forces a fallback to a different free port.

    Holding a listener on ``preferred`` makes its bind-test fail; the
    helper must return some OTHER, still-usable port rather than raising
    or returning the busy one — this is what lets the fallback never
    break daemon discovery (which keys off the pidfile, not the port).
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy_port = int(taken.getsockname()[1])

        chosen = local_server.pick_local_port(busy_port)

        assert chosen != busy_port
        # The fallback must itself be bindable, not just a different number.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as verify:
            verify.bind(("127.0.0.1", chosen))


def test_register_then_clear_local_server_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``register_local_server`` writes our pid+port; ``clear`` removes it.

    This is the handshake that lets a foreground ``omnigent server``
    advertise itself to the daemon: register writes ``<pid>\\n<port>\\n``
    so :func:`local_server_url_if_healthy` can discover it, and the
    shutdown clear leaves no stale record behind.
    """
    import os

    pid_file = tmp_path / "local_server.pid"
    sig_file = tmp_path / "local_server.sig"
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_LOG_REF_PATH", tmp_path / "local_server.logpath"
    )

    local_server.register_local_server(8000)
    assert pid_file.read_text() == f"{os.getpid()}\n8000\n"
    # The sig sidecar is written alongside the pidfile.
    assert sig_file.exists()

    local_server.clear_local_server_record()
    # Both files die together — a stale sig must not outlive the pidfile.
    assert not pid_file.exists()
    assert not sig_file.exists()


def test_register_local_server_stamps_matching_sig(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A foreground server's sidecar matches what connect/run compute.

    The regression this guards against: ``register_local_server`` wrote only the
    pidfile, so a foreground ``omnigent server`` presented no sig and the
    next ``connect``/``run`` saw ``None != desired`` and stopped + respawned
    it. With the sig stamped, the reuse path in ``ensure_local_omnigent_server``
    short-circuits to the healthy URL WITHOUT spawning — proving the
    foreground server is now reusable. Both sides compute the signature
    from the same resolved auth source, so the two signatures agree.
    """
    pid_file = tmp_path / "local_server.pid"
    sig_file = tmp_path / "local_server.sig"
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_LOG_REF_PATH", tmp_path / "local_server.logpath"
    )

    # Foreground `omnigent server` advertises itself in the pidfile + sig.
    local_server.register_local_server(8000)
    assert sig_file.read_text().strip() == local_server.server_config_signature()

    # A later connect/run under the same config finds it healthy and reuses
    # it — Popen must NOT fire (that would be the stop-and-respawn bug).
    monkeypatch.setattr(
        local_server, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8000"
    )

    def _must_not_popen(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("respawned a foreground server with a matching sig")

    monkeypatch.setattr(local_server.subprocess, "Popen", _must_not_popen)

    result = local_server.ensure_local_omnigent_server()
    assert result.url == "http://127.0.0.1:8000"
    # Reused the foreground server — no respawn (the prior respawn regression).
    assert result.spawned is False


def test_clear_local_server_record_leaves_other_pids_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Clear is a no-op when the pidfile points at a different process.

    The foreground server must never delete the record of a
    daemon-spawned server (or vice versa) on its own shutdown — only the
    process that registered itself may clear the file. We seed a foreign
    pid and assert the file survives.
    """
    import os

    pid_file = tmp_path / "local_server.pid"
    # A pid that is not ours; the value need not be alive for this check.
    pid_file.write_text(f"{os.getpid() + 1}\n9100\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)

    local_server.clear_local_server_record()

    assert pid_file.exists()
    assert pid_file.read_text() == f"{os.getpid() + 1}\n9100\n"


# ---------------------------------------------------------------------------
# Server log-path sidecar — so `server start`/`status` name the exact log
# ---------------------------------------------------------------------------


def test_ensure_local_omnigent_server_spawn_records_and_returns_log_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A spawned server returns its captured-log path and records it for status.

    ``omnigent server start`` used to be a black box — it printed only the
    URL. The spawn now threads the captured stdout/stderr log file out via
    ``LocalServerStartup.log_path`` AND into the log-path sidecar, so both
    the spawning call and a later ``server status`` can name the exact file.
    """
    monkeypatch.setattr(local_server, "local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=8000: 8765)
    pid_file = tmp_path / "local_server.pid"
    sig_file = tmp_path / "local_server.sig"
    log_ref = tmp_path / "local_server.logpath"
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_LOG_REF_PATH", log_ref)
    # Point the persistent data dir at tmp so logs/server lands under tmp.
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    class _Proc:
        pid = 9001

        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def poll(self) -> None:
            return None

    monkeypatch.setattr(local_server.subprocess, "Popen", _Proc)
    monkeypatch.setattr(
        local_server,
        "_wait_for_local_omnigent_server",
        lambda base_url, proc, log_path, timeout=45.0: None,
    )
    # Ownership probe confirms our own child holds the port (no contention).
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: 9001)

    result = local_server.ensure_local_omnigent_server()

    assert result.spawned is True
    assert result.log_path is not None
    # The captured log lives under the per-user server log dir as a .log file.
    assert result.log_path.parent == tmp_path / ".omnigent" / "logs" / "server"
    assert result.log_path.suffix == ".log"
    assert result.log_path.name.startswith("local-server-")
    # Recorded in the sidecar so a later status/reuse names the same file.
    assert log_ref.read_text().strip() == str(result.log_path)

    # `server status` (health stub) surfaces the exact recorded log path —
    # not just the directory — proving the read path works end to end.
    monkeypatch.setattr(
        local_server, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8765"
    )
    info = local_server.local_server_status()
    assert info.running is True
    assert info.log_path == result.log_path


def test_ensure_local_omnigent_server_reuse_reads_log_path_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reusing a healthy server reports its log file from the sidecar.

    The reuse path never sees the original spawn's ``log_path`` variable, so
    it must read the recorded path back from the sidecar — otherwise a
    ``server start`` that reuses an existing background server could not name
    its log. Popen must not fire (the stub fails the test if it does).
    """
    monkeypatch.setattr(
        local_server, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8123"
    )
    sig_file = tmp_path / "local_server.sig"
    sig_file.write_text(local_server.server_config_signature() + "\n")
    log_ref = tmp_path / "local_server.logpath"
    recorded = tmp_path / ".omnigent" / "logs" / "server" / "local-server-cd34.log"
    log_ref.write_text(str(recorded) + "\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_LOG_REF_PATH", log_ref)

    def _must_not_popen(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("spawned despite a healthy reusable server")

    monkeypatch.setattr(local_server.subprocess, "Popen", _must_not_popen)

    result = local_server.ensure_local_omnigent_server()

    assert result.spawned is False
    assert result.log_path == recorded


def test_register_local_server_clears_stale_log_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A foreground server clears any stale background log-path sidecar.

    A foreground ``omnigent server`` streams logs to its own terminal, not a
    file. If a prior background server left a log-ref sidecar behind, a later
    ``server status`` for the foreground one must NOT report that defunct
    file — register clears it so the log path resolves to ``None``.
    """
    pid_file = tmp_path / "local_server.pid"
    sig_file = tmp_path / "local_server.sig"
    log_ref = tmp_path / "local_server.logpath"
    log_ref.write_text(str(tmp_path / "stale-background.log") + "\n")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", pid_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", sig_file)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_LOG_REF_PATH", log_ref)

    local_server.register_local_server(8000)
    assert not log_ref.exists()

    monkeypatch.setattr(
        local_server, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8000"
    )
    info = local_server.local_server_status()
    assert info.log_path is None


# ---------------------------------------------------------------------------
# stop_untracked_local_server — the off-switch's orphan sweep
# ---------------------------------------------------------------------------


class _FakeHealthResp:
    """Minimal stand-in for an httpx response from ``GET /health``."""

    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        """Return the canned decoded body."""
        return self._body


def _fake_subprocess(stdout: str | None = None, raises: BaseException | None = None) -> Any:
    """Build a stand-in for the ``subprocess`` module exposing ``run``.

    Replaces ``local_server.subprocess`` (the module's name binding) rather
    than mutating the real ``subprocess.run``, so nothing leaks across tests.

    :param stdout: ``run().stdout`` to return (the lsof ``-t`` PID lines).
    :param raises: If set, ``run`` raises this instead (e.g. lsof missing).
    """

    class _Completed:
        def __init__(self) -> None:
            self.stdout = stdout

    class _Sub:
        SubprocessError = Exception

        @staticmethod
        def run(*_args: object, **_kwargs: object) -> Any:
            if raises is not None:
                raise raises
            return _Completed()

    return _Sub


def test_stop_untracked_local_server_kills_orphan_on_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live Omnigent server on :8000 with no pidfile entry is found and stopped.

    This is the reported bug: the pidfile was lost while the server lived, so
    ``stop_local_omnigent_server`` (pidfile-scoped) couldn't see it. The sweep must
    confirm it's our server via ``/health``, resolve its PID via lsof, and
    terminate it — returning the PID so the off-switch can report it.
    """
    monkeypatch.setattr(
        "httpx.get", lambda url, *, timeout: _FakeHealthResp(200, {"status": "ok"})
    )
    monkeypatch.setattr(local_server, "subprocess", _fake_subprocess(stdout="93359\n93360\n"))
    monkeypatch.setattr(local_server, "_pid_alive", lambda pid: True)
    terminated: list[int] = []
    monkeypatch.setattr(local_server, "_terminate_pid", terminated.append)

    result = local_server.stop_untracked_local_server(port=8000)

    # The first lsof PID is the listener; it must actually be terminated.
    assert result == 93359
    assert terminated == [93359]


def test_stop_untracked_local_server_noop_when_nothing_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``/health`` responder → nothing killed, and lsof is never consulted.

    Guards against the off-switch killing whatever happens to hold the port:
    if there's no Omnigent server answering, we must not even look up a PID.
    """
    import httpx

    def _refused(url: str, *, timeout: float) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.get", _refused)
    monkeypatch.setattr(
        local_server,
        "subprocess",
        _fake_subprocess(raises=AssertionError("lsof consulted despite no Omnigent server")),
    )
    monkeypatch.setattr(local_server, "_terminate_pid", _raise_if_called)

    assert local_server.stop_untracked_local_server(port=8000) is None


def test_stop_untracked_local_server_noop_on_non_omnigent_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 that isn't ``{"status": "ok"}`` is some other app — never killed."""
    monkeypatch.setattr(
        "httpx.get", lambda url, *, timeout: _FakeHealthResp(200, {"hello": "world"})
    )
    monkeypatch.setattr(local_server, "_terminate_pid", _raise_if_called)

    assert local_server.stop_untracked_local_server(port=8000) is None


def test_stop_untracked_local_server_noop_when_lsof_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live Omnigent server but no resolvable PID (lsof missing) → degrade, no kill.

    Without a PID we can't terminate, so the sweep returns ``None`` rather
    than crashing — the off-switch then leaves a manual hint to the user.
    """
    monkeypatch.setattr(
        "httpx.get", lambda url, *, timeout: _FakeHealthResp(200, {"status": "ok"})
    )
    monkeypatch.setattr(
        local_server, "subprocess", _fake_subprocess(raises=FileNotFoundError("lsof"))
    )
    monkeypatch.setattr(local_server, "_terminate_pid", _raise_if_called)

    assert local_server.stop_untracked_local_server(port=8000) is None


def _raise_if_called(pid: int) -> None:
    """A ``_terminate_pid`` stub that fails the test if a kill is attempted."""
    raise AssertionError(f"_terminate_pid({pid}) called when nothing should be stopped")


def _patch_spawn_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the pidfile/sig/log-ref/home at *tmp_path* for a spawn test.

    Shared setup for the port-ownership tests below; mirrors the
    per-file patching the earlier ensure_* tests do inline.

    :param monkeypatch: The test's monkeypatch fixture.
    :param tmp_path: The test's tmp dir standing in for ``$HOME``.
    """
    monkeypatch.setattr(local_server, "local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_PID_PATH", tmp_path / "local_server.pid")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_SIG_PATH", tmp_path / "local_server.sig")
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_LOG_REF_PATH", tmp_path / "local_server.logpath"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(
        local_server,
        "_wait_for_local_omnigent_server",
        lambda base_url, proc, log_path, timeout=45.0: None,
    )


def _proc_cls_with_pids(pids: list[int], polls: list[int | None] | None = None) -> type:
    """Build a fake ``Popen`` class assigning the given pids in spawn order.

    :param pids: Pids for successive spawns, e.g. ``[9001, 9002]``.
    :param polls: Per-spawn ``poll()`` results, e.g. ``[1, None]`` for an
        exited first child and a live second. Defaults to every child
        reporting exited (returncode 1), matching the bind-race loser's
        natural EADDRINUSE death that ``_await_doomed_child_exit`` waits
        for without sleeping.
    :returns: A class usable as a ``subprocess.Popen`` monkeypatch target.
    """
    pid_iter = iter(pids)
    poll_iter = iter(polls if polls is not None else [1] * len(pids))

    class _Proc:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.pid = next(pid_iter)
            self._poll = next(poll_iter)

        def poll(self) -> int | None:
            return self._poll

    return _Proc


def test_ensure_respawns_on_free_port_when_foreign_server_owns_preferred_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A foreign listener on the preferred port forces a free-port respawn.

    The stable-port preference lets two concurrent spawners (another HOME,
    a parallel test worker) race the same port: the loser's child dies
    EADDRINUSE while the winner's server answers the loser's /health
    probe. Adopting that foreign server means running against the wrong
    owner's DB and crashing when the owner stops it (the e2e
    session_id_pins flake). The spawn must instead let its doomed child
    exit naturally and respawn on an OS-assigned free port.
    """
    _patch_spawn_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=6767: 6767)
    monkeypatch.setattr(local_server, "_find_free_local_port", lambda: 9111)
    monkeypatch.setattr(local_server.subprocess, "Popen", _proc_cls_with_pids([9001, 9002]))
    # Preferred port is owned by a foreign pid; the respawn port is ours.
    monkeypatch.setattr(
        local_server, "_pid_listening_on_port", lambda port: {6767: 4242, 9111: 9002}[port]
    )
    # The doomed child reports exited (default poll result), so the
    # terminate backstop must NOT fire: a SIGTERM mid-migration is exactly
    # the half-migrated-DB bug this flow exists to avoid.
    monkeypatch.setattr(local_server, "_terminate_pid", _raise_if_called)

    result = local_server.ensure_local_omnigent_server()

    # The returned URL is the respawn's free port, NOT the contended one —
    # the preferred-port spawn was never adopted.
    assert result.url == "http://127.0.0.1:9111"
    assert result.spawned is True
    # The pidfile records the respawn, so later reuse finds the real server.
    assert (tmp_path / "local_server.pid").read_text() == "9002\n9111\n"


def test_ensure_retries_when_child_dies_and_foreign_owner_holds_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A child that dies during startup retries when a foreign owner holds the port.

    The bind-race loser can die fast enough that the readiness wait raises
    (child exited) before /health is ever answered. That startup failure
    is retryable when, and only when, a foreign listener owns the port —
    the failure was the race, not the server.
    """
    _patch_spawn_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=6767: 6767)
    monkeypatch.setattr(local_server, "_find_free_local_port", lambda: 9111)
    monkeypatch.setattr(local_server.subprocess, "Popen", _proc_cls_with_pids([9001, 9002]))
    monkeypatch.setattr(
        local_server, "_pid_listening_on_port", lambda port: {6767: 4242, 9111: 9002}[port]
    )
    # The doomed child already exited (that's why the wait raised); the
    # terminate backstop must not fire on an already-dead child.
    monkeypatch.setattr(local_server, "_terminate_pid", _raise_if_called)

    waits: list[str] = []

    def _wait(base_url: str, proc: object, log_path: object, timeout: float = 45.0) -> None:
        """First (contended) wait raises like a died-at-startup child."""
        waits.append(base_url)
        if "6767" in base_url:
            raise click.ClickException("Background local server failed to start")

    monkeypatch.setattr(local_server, "_wait_for_local_omnigent_server", _wait)

    result = local_server.ensure_local_omnigent_server()

    assert result.url == "http://127.0.0.1:9111"
    # Both spawns were awaited: the contended one (raised), then the retry.
    assert waits == ["http://127.0.0.1:6767", "http://127.0.0.1:9111"]


def test_ensure_startup_failure_without_contention_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A genuine startup failure (no foreign owner) still fails loud.

    The retry is reserved for port contention. When the child dies and
    nothing foreign owns the port (a real boot failure: bad spec, import
    error), the original error must propagate after ONE spawn — retrying
    would just fail again and hide the error for another timeout.
    """
    _patch_spawn_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=6767: 6767)
    spawn_count: list[int] = []
    proc_cls = _proc_cls_with_pids([9001])

    class _CountingProc(proc_cls):  # type: ignore[valid-type,misc]
        def __init__(self, *args: object, **kwargs: object) -> None:
            spawn_count.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(local_server.subprocess, "Popen", _CountingProc)
    # Nothing is listening on the port (the child died without binding).
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: None)
    monkeypatch.setattr(local_server, "_terminate_pid", _raise_if_called)

    def _wait(base_url: str, proc: object, log_path: object, timeout: float = 45.0) -> None:
        """Simulate the child dying before ever answering /health."""
        raise click.ClickException("Background local server failed to start")

    monkeypatch.setattr(local_server, "_wait_for_local_omnigent_server", _wait)

    with pytest.raises(click.ClickException) as excinfo:
        local_server.ensure_local_omnigent_server()

    assert "failed to start" in str(excinfo.value)
    # Exactly one spawn: a non-contention failure must not retry.
    assert spawn_count == [1]


def test_ensure_fails_loud_when_port_contention_persists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Contention surviving the free-port respawn is an error, not a loop.

    A foreign owner on an OS-assigned free port means lsof is lying or
    something is racing us pathologically; respawning forever would hang
    the CLI. One retry, then fail loud naming the squatter pid.
    """
    _patch_spawn_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=6767: 6767)
    monkeypatch.setattr(local_server, "_find_free_local_port", lambda: 9111)
    monkeypatch.setattr(local_server.subprocess, "Popen", _proc_cls_with_pids([9001, 9002]))
    # A foreign pid owns BOTH the preferred and the respawn port. Both
    # children report exited (default poll result), so no backstop kill.
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: 4242)
    monkeypatch.setattr(local_server, "_terminate_pid", _raise_if_called)

    with pytest.raises(click.ClickException) as excinfo:
        local_server.ensure_local_omnigent_server()

    assert "contention persists" in str(excinfo.value)
    assert "4242" in str(excinfo.value)


def test_ensure_backstop_terminates_doomed_child_that_never_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A doomed child that never exits is terminated after the grace period.

    The natural-exit wait exists to avoid SIGTERM mid-migration, but a
    wedged child (hung import, blocked I/O) must not stall the respawn
    forever: after the grace the terminate backstop fires and the respawn
    proceeds.
    """
    _patch_spawn_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=6767: 6767)
    monkeypatch.setattr(local_server, "_find_free_local_port", lambda: 9111)
    # First child never exits (poll None); the respawn reports exited but
    # owns its port so its poll is irrelevant.
    monkeypatch.setattr(
        local_server.subprocess, "Popen", _proc_cls_with_pids([9001, 9002], polls=[None, 1])
    )
    monkeypatch.setattr(
        local_server, "_pid_listening_on_port", lambda port: {6767: 4242, 9111: 9002}[port]
    )
    # Zero grace so the backstop fires immediately instead of waiting 45s.
    monkeypatch.setattr(local_server, "_DOOMED_CHILD_EXIT_GRACE_S", 0.0)
    terminated: list[int] = []
    monkeypatch.setattr(local_server, "_terminate_pid", terminated.append)

    result = local_server.ensure_local_omnigent_server()

    assert result.url == "http://127.0.0.1:9111"
    # Exactly the wedged first child was backstop-killed; the respawn
    # survived. [] would mean the wedged child leaked past the respawn.
    assert terminated == [9001]


def test_ensure_does_not_advertise_pidfile_before_ownership_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The pidfile is written only AFTER health + ownership are confirmed.

    Discoverers poll ``local_server_url_if_healthy`` concurrently (the
    CLI's daemon-wait loop, every 0.2s). A record written at spawn time
    advertises a port the spawn may abandon: during the contended-port
    window a foreign server answers /health for the recorded port, the
    CLI adopts that URL, and crashes when the foreign owner stops it
    (the residual e2e session_id_pins failure after the adoption fix).
    """
    _patch_spawn_env(monkeypatch, tmp_path)
    pid_file = tmp_path / "local_server.pid"
    monkeypatch.setattr(local_server, "pick_local_port", lambda preferred=6767: 6767)
    monkeypatch.setattr(local_server.subprocess, "Popen", _proc_cls_with_pids([9001]))
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: 9001)

    def _wait(base_url: str, proc: object, log_path: object, timeout: float = 45.0) -> None:
        """Stand-in for the readiness window: the record must not exist yet."""
        assert not pid_file.exists(), (
            "pidfile was written before ownership confirmation; a concurrent "
            "discoverer could adopt a contended port the spawn later abandons"
        )

    monkeypatch.setattr(local_server, "_wait_for_local_omnigent_server", _wait)

    result = local_server.ensure_local_omnigent_server()

    assert result.url == "http://127.0.0.1:6767"
    # Once confirmed, the record IS advertised for reuse/discovery.
    assert pid_file.read_text() == "9001\n6767\n"
