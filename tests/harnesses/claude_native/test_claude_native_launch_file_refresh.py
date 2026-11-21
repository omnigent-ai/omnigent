"""A long-lived Claude pane keeps its launch-time files through temp-dir cleanup (#9900)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge

_DAY_S = 24 * 60 * 60.0
_NOW = 1_800_000_000.0


def _age(path: Path, seconds: float) -> None:
    os.utime(path, (_NOW - seconds, _NOW - seconds))


def _clean_like_dirhelper(root: Path, *, days: float = 3) -> None:
    """Delete regular files older than *days*, as macOS ``dirhelper`` does under ``$TMPDIR``."""
    cutoff = _NOW - days * _DAY_S
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_mtime < cutoff:
            path.unlink()


def _launch(tmp_path: Path, *, socket_dir_name: str = "omnigent-terminal-abc123") -> Path:
    """Lay out a bridge dir and terminal dir as a Claude pane launch leaves them."""
    terminal_dir = tmp_path / socket_dir_name
    (terminal_dir / "clipboard-bin").mkdir(parents=True)
    pbcopy = terminal_dir / "clipboard-bin" / "pbcopy"
    pbcopy.write_text("#!/bin/sh\n", encoding="utf-8")
    status = terminal_dir / "program-status.json"
    status.write_text("{}", encoding="utf-8")

    bridge_dir = tmp_path / "claude-native" / "0123456789abcdef0123456789abcdef"
    bridge_dir.mkdir(parents=True)
    (bridge_dir / "tmux.json").write_text(
        json.dumps({"socket_path": str(terminal_dir / "tmux.sock"), "tmux_target": "main"}),
        encoding="utf-8",
    )
    (bridge_dir / "permission_hook.json").write_text(
        json.dumps({"ap_server_url": "http://127.0.0.1:8787"}), encoding="utf-8"
    )
    (bridge_dir / "bridge.json").write_text("{}", encoding="utf-8")
    for path in (*bridge_dir.iterdir(), pbcopy, status):
        _age(path, 4 * _DAY_S)
    state = bridge_dir / "state.json"
    state.write_text("{}", encoding="utf-8")
    _age(state, 60.0)
    return bridge_dir


def test_unrefreshed_launch_files_age_out_and_web_delivery_fails(tmp_path: Path) -> None:
    """The failure in #9900: after cleanup the live pane is no longer advertised."""
    bridge_dir = _launch(tmp_path)

    _clean_like_dirhelper(tmp_path)

    assert not (bridge_dir / "tmux.json").exists()
    assert not (bridge_dir / "permission_hook.json").exists()
    with pytest.raises(bridge.TmuxSessionNotAdvertised):
        bridge._wait_for_tmux_info(bridge_dir, timeout_s=0.05)


def test_refreshed_launch_files_survive_cleanup(tmp_path: Path) -> None:
    """Refreshed bridge and terminal files outlast the cleaner; delivery still finds the pane."""
    bridge_dir = _launch(tmp_path)
    terminal_dir = tmp_path / "omnigent-terminal-abc123"

    refreshed = bridge.refresh_launch_files(bridge_dir, now=_NOW)
    _clean_like_dirhelper(tmp_path)

    # tmux.json, permission_hook.json, bridge.json, pbcopy, program-status.json.
    assert refreshed == 5
    assert (bridge_dir / "permission_hook.json").exists()
    assert (bridge_dir / "bridge.json").exists()
    assert (terminal_dir / "clipboard-bin" / "pbcopy").exists()
    assert bridge._wait_for_tmux_info(bridge_dir, timeout_s=0.05) == {
        "socket_path": str(terminal_dir / "tmux.sock"),
        "tmux_target": "main",
    }
    # A recently written file is left as it was.
    assert (bridge_dir / "state.json").stat().st_mtime == _NOW - 60.0


def test_refresh_leaves_a_shared_socket_dir_alone(tmp_path: Path) -> None:
    """A socket outside an ``omnigent-terminal-*`` dir never widens what gets touched."""
    bridge_dir = _launch(tmp_path, socket_dir_name="shared-tmp")
    neighbour = tmp_path / "shared-tmp" / "program-status.json"

    refreshed = bridge.refresh_launch_files(bridge_dir, now=_NOW)

    assert refreshed == 3
    assert neighbour.stat().st_mtime == _NOW - 4 * _DAY_S


def test_refresh_without_tmux_json_covers_the_bridge_dir_only(tmp_path: Path) -> None:
    """A bridge dir with no advertisement yet is still refreshed, and nothing else is."""
    bridge_dir = _launch(tmp_path)
    (bridge_dir / "tmux.json").unlink()

    refreshed = bridge.refresh_launch_files(bridge_dir, now=_NOW)

    assert refreshed == 2
    pbcopy = tmp_path / "omnigent-terminal-abc123" / "clipboard-bin" / "pbcopy"
    assert pbcopy.stat().st_mtime == _NOW - 4 * _DAY_S
