"""Tests for crash-safe native Codex process registry reconciliation."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from omnigent import codex_native_process_registry as registry

fcntl = pytest.importorskip("fcntl")


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
