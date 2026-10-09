"""Fault injection for claude-native tmux advertisements, shared by browser and runner tests."""

from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.claude_native.bridge import (
    _BRIDGE_ROOT,
    _TMUX_FILE,
    read_active_session_id,
)


def remove_tmux_advertisement(session_id: str, *, bridge_root: Path = _BRIDGE_ROOT) -> str:
    """Remove the ``tmux.json`` advertisement owned by *session_id*.

    Exactly one bridge directory under *bridge_root* must name the session as
    active, so an unrelated session's pane is never disturbed.

    :param session_id: Session whose pane should lose its advertisement.
    :param bridge_root: Root holding the per-session bridge directories.
    :returns: The path of the removed advertisement.
    """
    matches = [
        path
        for path in bridge_root.glob(f"*/{_TMUX_FILE}")
        if read_active_session_id(path.parent) == session_id
    ]
    assert len(matches) == 1, "expected exactly one advertisement for the fixture session"
    target = matches[0]
    target.unlink()
    assert not target.exists()
    return str(target)
