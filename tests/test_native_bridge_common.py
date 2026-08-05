"""Tests for the shared native-bridge owner-pid marker + orphan prune."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent import native_bridge_common


def test_write_owner_pid_marker_records_current_pid(tmp_path: Path) -> None:
    """The marker names the process that prepared the dir (owner-pid invariant)."""
    native_bridge_common.write_owner_pid_marker(tmp_path)
    marker = tmp_path / native_bridge_common.OWNER_PID_FILENAME
    assert marker.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_write_owner_pid_marker_swallows_missing_dir(tmp_path: Path) -> None:
    """A best-effort write into a missing dir never raises (crash-time safety)."""
    # No exception even though the target dir does not exist.
    native_bridge_common.write_owner_pid_marker(tmp_path / "does-not-exist")


def test_prune_removes_dead_keeps_live_and_unmarked(tmp_path: Path) -> None:
    """Only provably-dead-owner dirs are pruned; live + unmarked survive."""
    root = tmp_path / "bridge-root"
    root.mkdir()

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_dir = root / "deadowner"
    dead_dir.mkdir()
    (dead_dir / native_bridge_common.OWNER_PID_FILENAME).write_text(
        str(dead.pid), encoding="utf-8"
    )

    live_dir = root / "liveowner"
    live_dir.mkdir()
    (live_dir / native_bridge_common.OWNER_PID_FILENAME).write_text(
        str(os.getpid()), encoding="utf-8"
    )

    unmarked_dir = root / "unmarked"
    unmarked_dir.mkdir()

    pruned = native_bridge_common.prune_orphaned_dirs(root)

    assert pruned == 1
    assert not dead_dir.exists()
    assert live_dir.exists()
    assert unmarked_dir.exists()


def test_prune_ignores_non_dir_entries_and_bad_markers(tmp_path: Path) -> None:
    """A stray file and an unparseable marker are left untouched (conservative)."""
    root = tmp_path / "bridge-root"
    root.mkdir()
    (root / "stray-file").write_text("not a dir", encoding="utf-8")
    bad = root / "badmarker"
    bad.mkdir()
    (bad / native_bridge_common.OWNER_PID_FILENAME).write_text("not-an-int", encoding="utf-8")

    assert native_bridge_common.prune_orphaned_dirs(root) == 0
    assert (root / "stray-file").exists()
    assert bad.exists()


def test_prune_missing_root_returns_zero(tmp_path: Path) -> None:
    """A never-created bridge root is a no-op, not an error."""
    assert native_bridge_common.prune_orphaned_dirs(tmp_path / "never-created") == 0


def test_reap_invokes_prune_for_every_native_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dynamic sweep calls each native agent's module-level prune once."""
    agents = (
        SimpleNamespace(key="claude"),
        SimpleNamespace(key="codex"),
        SimpleNamespace(key="antigravity"),
        SimpleNamespace(key="opencode"),
    )
    monkeypatch.setattr("omnigent.harness_plugins.native_agents", lambda: agents)

    called: list[str] = []

    def _recorder(key: str, count: int):
        def _prune() -> int:
            called.append(key)
            return count

        return _prune

    monkeypatch.setattr(
        "omnigent.claude_native_bridge.prune_orphaned_bridge_dirs", _recorder("claude", 1)
    )
    monkeypatch.setattr(
        "omnigent.codex_native_bridge.prune_orphaned_bridge_dirs", _recorder("codex", 2)
    )
    monkeypatch.setattr(
        "omnigent.antigravity_native_bridge.prune_orphaned_bridge_dirs",
        _recorder("antigravity", 3),
    )
    monkeypatch.setattr(
        "omnigent.opencode_native_bridge.prune_orphaned_bridge_dirs", _recorder("opencode", 4)
    )

    total = native_bridge_common.reap_orphaned_native_bridge_dirs()

    assert set(called) == {"claude", "codex", "antigravity", "opencode"}
    assert total == 1 + 2 + 3 + 4


def test_reap_skips_agents_without_a_prune_and_bad_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents whose module lacks a prune (or does not import) are skipped."""
    agents = (
        SimpleNamespace(key="pi"),  # module exists, no prune_orphaned_bridge_dirs
        SimpleNamespace(key="not_a_real_harness"),  # import fails
        SimpleNamespace(key="claude"),  # the one real pruner
    )
    monkeypatch.setattr("omnigent.harness_plugins.native_agents", lambda: agents)

    called: list[str] = []

    def _claude_prune() -> int:
        called.append("claude")
        return 5

    monkeypatch.setattr("omnigent.claude_native_bridge.prune_orphaned_bridge_dirs", _claude_prune)

    total = native_bridge_common.reap_orphaned_native_bridge_dirs()

    assert called == ["claude"]
    assert total == 5


def test_reap_isolates_a_failing_pruner(monkeypatch: pytest.MonkeyPatch) -> None:
    """One harness's prune raising must not abort the sweep of the others."""
    agents = (SimpleNamespace(key="codex"), SimpleNamespace(key="claude"))
    monkeypatch.setattr("omnigent.harness_plugins.native_agents", lambda: agents)

    def _boom() -> int:
        raise RuntimeError("boom")

    def _ok() -> int:
        return 7

    monkeypatch.setattr("omnigent.codex_native_bridge.prune_orphaned_bridge_dirs", _boom)
    monkeypatch.setattr("omnigent.claude_native_bridge.prune_orphaned_bridge_dirs", _ok)

    # codex raises but is swallowed; claude still runs and its count is returned.
    assert native_bridge_common.reap_orphaned_native_bridge_dirs() == 7


def test_reap_isolates_a_module_that_raises_on_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge module raising a non-ImportError at import time is skipped,
    never propagated — a broken transitive import must not crash startup."""
    agents = (SimpleNamespace(key="broken"), SimpleNamespace(key="claude"))
    monkeypatch.setattr("omnigent.harness_plugins.native_agents", lambda: agents)

    real_import = native_bridge_common.importlib.import_module

    def _fake_import(name: str, *args: object, **kwargs: object):
        if name == "omnigent.broken_native_bridge":
            raise RuntimeError("boom at import time")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(native_bridge_common.importlib, "import_module", _fake_import)

    called: list[str] = []

    def _claude_prune() -> int:
        called.append("claude")
        return 3

    monkeypatch.setattr("omnigent.claude_native_bridge.prune_orphaned_bridge_dirs", _claude_prune)

    # The broken module's import RuntimeError is swallowed; claude still runs.
    assert native_bridge_common.reap_orphaned_native_bridge_dirs() == 3
    assert called == ["claude"]
