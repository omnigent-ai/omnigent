"""Tests for crash-safe native Codex process registry reconciliation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native import process_registry as registry
from tests._helpers.polling import poll_until

fcntl = pytest.importorskip("fcntl")
_REAL_PROCESS_START_IDENTITY = registry._process_start_identity
_REAL_PROCESS_GROUP_MATCHES_ENTRY = registry._process_group_matches_entry


@pytest.fixture(autouse=True)
def _fake_processes_have_no_kernel_identity(monkeypatch) -> None:
    """Keep synthetic PID fixtures independent of processes on the test host."""
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: None)
    monkeypatch.setattr(registry, "_process_group_matches_entry", lambda _entry: True)


def _registry_payload(path: Path) -> list[dict[str, object]]:
    """
    Return the raw JSON registry payload.

    :param path: Registry file path.
    :returns: Parsed registry entries.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def test_registry_add_remove_round_trip(tmp_path: Path) -> None:
    """Registry writes and removes a tagged codex child entry."""
    path = tmp_path / "registry.json"

    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        tmux_session_name="omnigent-codex-123",
        session_tag="tag-123",
        owner_lock_path=tmp_path / "owner.lock",
        registry_path=path,
    )

    assert _registry_payload(path) == [
        {
            "pid": 123,
            "pgid": 456,
            "tmux_session_name": "omnigent-codex-123",
            "session_tag": "tag-123",
            "owner_lock_path": str(tmp_path / "owner.lock"),
        }
    ]

    registry.unregister_codex_native_process("tag-123", registry_path=path)

    assert _registry_payload(path) == []


def test_reconciliation_reaps_alive_tagged_process(tmp_path: Path, monkeypatch) -> None:
    """A live process with the matching cmdline tag is reaped by process group."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == [(456, signal.SIGTERM)]
    assert _registry_payload(path) == []


def test_reconciliation_skips_pid_reuse_without_matching_tag(tmp_path: Path, monkeypatch) -> None:
    """A reused PID is never killed when the cmdline lacks the session tag."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(registry, "_process_cmdline", lambda _pid: "python unrelated.py")
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == []


def test_reconciliation_uses_process_start_identity_after_argv0_is_lost(
    tmp_path: Path, monkeypatch
) -> None:
    """A matching process birth identity survives the Codex npm shim."""
    path = tmp_path / "registry.json"
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: "linux:boot:123")
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_process_cmdline", lambda _pid: "codex app-server")
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == [(456, signal.SIGTERM)]
    assert _registry_payload(path) == []


def test_reconciliation_skips_reused_pid_with_different_process_start_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """A recycled PID cannot make reconciliation kill the replacement process."""
    path = tmp_path / "registry.json"
    identities = iter(("linux:boot:123", "linux:boot:456"))
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: next(identities))
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == []


def test_reconciliation_retains_alive_process_when_identity_is_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    """A transient identity read failure must not discard a live child."""
    path = tmp_path / "registry.json"
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: "linux:boot:123")
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: None)
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: True)
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path)[0]["process_start_identity"] == "linux:boot:123"


def test_reconciliation_retains_process_when_pgid_changed(tmp_path: Path, monkeypatch) -> None:
    """A matching PID is not signaled through a stale process-group id."""
    path = tmp_path / "registry.json"
    monkeypatch.setattr(registry, "_process_start_identity", lambda _pid: "linux:boot:123")
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    monkeypatch.setattr(registry, "_process_group_matches_entry", lambda _entry: False)
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path)[0]["pgid"] == 456


def test_reconciliation_reaps_real_process_after_argv0_marker_is_lost(
    tmp_path: Path, monkeypatch
) -> None:
    """Kernel identity reaps a live process whose command line has no tag."""
    monkeypatch.setattr(registry, "_process_start_identity", _REAL_PROCESS_START_IDENTITY)
    monkeypatch.setattr(
        registry,
        "_process_group_matches_entry",
        _REAL_PROCESS_GROUP_MATCHES_ENTRY,
    )
    path = tmp_path / "registry.json"
    sleeper = "import time; time.sleep(300)"
    wrapper = (
        "import os,sys,time; time.sleep(0.2); "
        f"os.execv(sys.executable, [sys.executable, '-c', {sleeper!r}, 'app-server'])"
    )
    victim = subprocess.Popen(
        ["python omnigent_crash_teardown_tag=lost-after-exec", "-c", wrapper],
        executable=sys.executable,
        start_new_session=True,
    )
    try:
        registry.register_codex_native_process(
            pid=victim.pid,
            pgid=os.getpgid(victim.pid),
            session_tag="marker-lost-by-shim",
            owner_lock_path=None,
            registry_path=path,
        )
        deadline = time.monotonic() + 5.0
        while "omnigent_crash_teardown_tag" in registry._process_cmdline(victim.pid):
            assert time.monotonic() < deadline, "wrapper did not exec the marker-free process"
            time.sleep(0.01)

        registry.reconcile_codex_native_process_registry(registry_path=path)

        victim.wait(timeout=5.0)
        assert _registry_payload(path) == []
    finally:
        with contextlib.suppress(ProcessLookupError):
            victim.kill()
        with contextlib.suppress(Exception):
            victim.wait(timeout=5.0)


_MODEL_PROBE_LAUNCHER = """
import asyncio, json, os, sys
from pathlib import Path

from omnigent.harnesses.codex_native import app_server


async def main() -> None:
    codex_path, listen_url, probe_home = sys.argv[1:4]
    discovery = await app_server._start_codex_model_discovery_process(
        codex_path=codex_path,
        listen_url=listen_url,
        env=app_server._clean_codex_env() | {"CODEX_HOME": probe_home},
        cwd=Path(probe_home),
    )
    pid = discovery.process.pid
    print(json.dumps({"pid": pid, "pgid": os.getpgid(pid)}), flush=True)
    await asyncio.sleep(300)


asyncio.run(main())"""


def _process_has_exited(pid: int) -> bool:
    """Return whether *pid* is gone or a zombie nobody has reaped yet."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        state = stat.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    return state in {"Z", "X"}


def _fake_codex(tmp_path: Path) -> str:
    """Write a stand-in ``codex`` that stays alive whatever arguments it gets."""
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexec sleep 300\n", encoding="utf-8")
    codex.chmod(0o755)
    return str(codex)


def _read_probe_identity(launcher: subprocess.Popen[str], *, timeout: float) -> dict[str, int]:
    assert launcher.stdout is not None
    ready, _, _ = select.select([launcher.stdout], [], [], timeout)
    line = launcher.stdout.readline() if ready else ""
    if not line:
        launcher.kill()
        _, stderr = launcher.communicate(timeout=5.0)
        pytest.fail(f"launcher never reported its model probe: {stderr.strip()}")
    return json.loads(line)


def test_reconciliation_reaps_crash_leftover_model_probe_app_server(
    tmp_path: Path, monkeypatch
) -> None:
    """A fresh Host reaps the model-probe app-server a crashed Host left behind."""
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(registry, "_process_start_identity", _REAL_PROCESS_START_IDENTITY)
    monkeypatch.setattr(
        registry, "_process_group_matches_entry", _REAL_PROCESS_GROUP_MATCHES_ENTRY
    )
    registry_path = registry.codex_native_process_registry_path()
    probe_home = tmp_path / "codex-model-probe"
    probe_home.mkdir()
    repo_root = Path(app_server.__file__).resolve().parents[3]
    launcher_env = {**os.environ, "PYTHONPATH": str(repo_root)}

    # Killing the separate launcher releases the probe's owner lock like a real
    # Host crash; this test process then plays the fresh Host.
    launcher = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _MODEL_PROBE_LAUNCHER,
            _fake_codex(tmp_path),
            f"ws://127.0.0.1:{app_server._allocate_loopback_port()}",
            str(probe_home),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=launcher_env,
    )
    probe_pgid: int | None = None
    try:
        probe = _read_probe_identity(launcher, timeout=30.0)
        probe_pid, probe_pgid = probe["pid"], probe["pgid"]
        assert not _process_has_exited(probe_pid), "probe exited before the Host could crash"
        recorded = [(entry.pid, entry.pgid) for entry in registry._read_registry(registry_path)]
        assert recorded == [(probe_pid, probe_pgid)], (
            "the discovery spawn did not record the probe in the crash-safe registry"
        )

        launcher.kill()
        launcher.wait(timeout=5.0)
        assert not _process_has_exited(probe_pid), "probe did not outlive the crashed Host"

        registry.reconcile_codex_native_process_registry()

        poll_until(lambda: _process_has_exited(probe_pid), timeout_s=10.0, interval_s=0.05)
        assert registry._read_registry(registry_path) == []
    finally:
        with contextlib.suppress(ProcessLookupError):
            launcher.kill()
        with contextlib.suppress(Exception):
            launcher.wait(timeout=5.0)
        if probe_pgid is not None and probe_pgid != os.getpgrp():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(probe_pgid, signal.SIGKILL)


def test_model_probe_stop_unregisters_and_releases_its_lock(tmp_path: Path, monkeypatch) -> None:
    """Stopping a probe normally removes its registry entry and owner lock."""
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    registry_path = registry.codex_native_process_registry_path()

    async def _probe_lifecycle() -> None:
        discovery = await app_server._start_codex_model_discovery_process(
            codex_path=_fake_codex(tmp_path),
            listen_url=f"ws://127.0.0.1:{app_server._allocate_loopback_port()}",
            env=app_server._clean_codex_env(),
            cwd=tmp_path,
        )
        try:
            entries = registry._read_registry(registry_path)
            assert [entry.pid for entry in entries] == [discovery.process.pid]
            owner_lock_path = entries[0].owner_lock_path
            assert owner_lock_path is not None
            assert registry._owner_lock_held(owner_lock_path)
        finally:
            await app_server._stop_codex_model_discovery_process(discovery)
        assert registry._read_registry(registry_path) == []
        assert not Path(owner_lock_path).exists()

    asyncio.run(_probe_lifecycle())


def test_reconciliation_skips_live_sibling_when_owner_lock_is_held(
    tmp_path: Path, monkeypatch
) -> None:
    """A healthy sibling child is not reaped while its launcher owns the lock."""
    path = tmp_path / "registry.json"
    owner_lock = tmp_path / "owner.lock"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=owner_lock,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_owner_lock_held", lambda value: value == str(owner_lock))
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == [
        {
            "pid": 123,
            "pgid": 456,
            "tmux_session_name": None,
            "session_tag": "tag-123",
            "owner_lock_path": str(owner_lock),
        }
    ]


def test_reconciliation_reaps_when_owner_lock_is_not_held(tmp_path: Path, monkeypatch) -> None:
    """A tagged child is reaped after its owning launcher lock is gone."""
    path = tmp_path / "registry.json"
    owner_lock = tmp_path / "owner.lock"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=owner_lock,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_owner_lock_held", lambda _value: False)
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda _pid: "codex omnigent_crash_teardown_tag=tag-123 app-server",
    )
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == [(456, signal.SIGTERM)]
    assert _registry_payload(path) == []


def test_reconciliation_drops_dead_pids(tmp_path: Path, monkeypatch) -> None:
    """Dead process entries are discarded without kill attempts."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        session_tag="tag-123",
        owner_lock_path=None,
        registry_path=path,
    )
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(registry.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed == []
    assert _registry_payload(path) == []


def test_tmux_session_reaped_only_when_recorded_name_exists(tmp_path: Path, monkeypatch) -> None:
    """Matching tagged process reaps only the recorded existing tmux session."""
    path = tmp_path / "registry.json"
    registry.register_codex_native_process(
        pid=123,
        pgid=456,
        tmux_session_name="omnigent-codex-live",
        session_tag="tag-live",
        owner_lock_path=None,
        registry_path=path,
    )
    registry.register_codex_native_process(
        pid=124,
        pgid=457,
        tmux_session_name="omnigent-codex-missing",
        session_tag="tag-missing",
        owner_lock_path=None,
        registry_path=path,
    )
    killed_tmux: list[str] = []
    monkeypatch.setattr(registry, "_pid_alive", lambda pid: pid in {123, 124})
    monkeypatch.setattr(
        registry,
        "_process_cmdline",
        lambda pid: (
            "codex "
            f"omnigent_crash_teardown_tag=tag-{'live' if pid == 123 else 'missing'} "
            "app-server"
        ),
    )
    monkeypatch.setattr(registry.os, "killpg", lambda _pgid, _sig: None)
    monkeypatch.setattr(
        registry,
        "_tmux_session_exists",
        lambda name: name == "omnigent-codex-live",
    )
    monkeypatch.setattr(registry, "_kill_tmux_session", lambda name: killed_tmux.append(name))

    registry.reconcile_codex_native_process_registry(registry_path=path)

    assert killed_tmux == ["omnigent-codex-live"]
    assert _registry_payload(path) == []


def test_owner_lock_liveness_round_trip(tmp_path: Path, monkeypatch) -> None:
    """A held owner lock reads as held; releasing it makes the entry reapable."""
    monkeypatch.setattr(registry, "_codex_native_state_root", lambda: tmp_path)
    lock = registry.acquire_codex_native_process_owner_lock()
    assert lock is not None
    assert registry._owner_lock_held(str(lock.path)) is True
    lock.close()
    assert registry._owner_lock_held(str(lock.path)) is False


def test_registry_lock_serializes_read_modify_write(tmp_path: Path) -> None:
    """The registry lock is exclusive across the read-modify-write window."""
    path = tmp_path / "registry.json"
    with registry._registry_lock(path):
        fd = registry.os.open(str(path) + ".lock", registry.os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            registry.os.close(fd)


def test_reap_state_dir_kills_matching_app_server_and_spares_others(tmp_path: Path) -> None:
    """
    Reaping by state dir kills only processes carrying dir + "app-server".

    The stale-holder incident shape: an app-server from a dead runner still
    runs with the session state dir in its command line. A process carrying
    the dir WITHOUT the "app-server" marker (e.g. the pytest process's own
    tree) must survive.
    """
    state_dir = tmp_path / "deadbeefdeadbeefdeadbeefdeadbeef"
    sleeper = "import time; time.sleep(300)"
    victim = subprocess.Popen(
        [sys.executable, "-c", sleeper, str(state_dir), "app-server"],
        start_new_session=True,
    )
    bystander = subprocess.Popen(
        [sys.executable, "-c", sleeper, str(state_dir)],
        start_new_session=True,
    )
    try:
        reaped = registry.reap_codex_native_processes_for_state_dir(state_dir, grace_s=1.0)
        assert reaped == 1, f"expected exactly the victim to match, got {reaped}"
        # SIGTERM (or the SIGKILL escalation) must actually end the process.
        victim.wait(timeout=5.0)
        assert bystander.poll() is None, "process without the app-server marker was killed"
    finally:
        for proc in (victim, bystander):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5.0)


def test_reap_state_dir_without_matches_is_a_noop(tmp_path: Path) -> None:
    """A state dir no live process references reaps nothing."""
    assert registry.reap_codex_native_processes_for_state_dir(tmp_path / "no-match") == 0
