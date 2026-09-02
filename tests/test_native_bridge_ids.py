"""Tests for legacy bridge-id normalisation across the native harnesses."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.antigravity_native_bridge import (
    bridge_dir_for_bridge_id as antigravity_bridge_dir,
)
from omnigent.claude_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    REQUEST_SESSION_ID_ENV_VAR,
    build_claude_native_spawn_env,
)
from omnigent.claude_native_bridge import (
    bridge_dir_for_bridge_id as claude_bridge_dir,
)
from omnigent.codex_native_bridge import (
    bridge_dir_for_bridge_id as codex_bridge_dir,
)
from omnigent.native_bridge_ids import normalize_bridge_id
from omnigent.opencode_native_bridge import (
    bridge_dir_for_bridge_id as opencode_bridge_dir,
)

_SESSION_ID = "3066bdff8fbc4e9eafbd0978c4a61537"


@pytest.mark.parametrize(
    ("bridge_id", "expected"),
    [
        (f"conv_{_SESSION_ID}", _SESSION_ID),
        (_SESSION_ID, _SESSION_ID),
        (f"conv_{_SESSION_ID}-cleared", f"{_SESSION_ID}-cleared"),
        (f"{_SESSION_ID}-cleared", f"{_SESSION_ID}-cleared"),
    ],
)
def test_normalize_bridge_id_strips_only_the_legacy_prefix(
    bridge_id: str,
    expected: str,
) -> None:
    """The ``conv_`` prefix goes; the ``-cleared`` suffix and bare ids stay."""
    assert normalize_bridge_id(bridge_id) == expected


@pytest.mark.parametrize(
    "bridge_dir_for_bridge_id",
    [claude_bridge_dir, codex_bridge_dir, opencode_bridge_dir, antigravity_bridge_dir],
)
def test_legacy_and_bare_bridge_ids_share_one_dir(
    bridge_dir_for_bridge_id: Callable[[str], Path],
) -> None:
    """Both spellings of a session id must key the same rendezvous directory."""
    assert bridge_dir_for_bridge_id(f"conv_{_SESSION_ID}") == bridge_dir_for_bridge_id(_SESSION_ID)


def test_spawn_env_from_legacy_label_targets_the_terminals_bridge_dir() -> None:
    """
    A pre-migration ``conv_``-prefixed bridge-id label must not split the session.

    The terminal keys its bridge dir on the bare session id. When the executor's
    spawn env resolved the label verbatim it landed in the pre-migration dir,
    read a foreign ``active_session_id`` there, and rejected every turn as a
    stale post-``/clear`` session — the message never reached the pane.
    """
    spawn_env = build_claude_native_spawn_env(_SESSION_ID, bridge_id=f"conv_{_SESSION_ID}")

    assert spawn_env[BRIDGE_DIR_ENV_VAR] == str(claude_bridge_dir(_SESSION_ID))
    assert spawn_env[REQUEST_SESSION_ID_ENV_VAR] == _SESSION_ID
