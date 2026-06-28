"""Tests for native Codex bridge state helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    clear_active_turn_id_if_matches,
    clear_bridge_state,
    codex_home_for_bridge_dir,
    prepare_bridge_dir,
    read_bridge_startup_error,
    read_bridge_state,
    read_codex_config_model,
    read_policy_hook_config,
    write_bridge_startup_error,
    write_bridge_state,
    write_policy_hook_config,
)


def _seed_active_turn(bridge_dir: Path, active_turn_id: str | None) -> None:
    """
    Write bridge state with a given active turn id.

    :param bridge_dir: Native Codex bridge directory.
    :param active_turn_id: Active turn id to seed, e.g. ``"turn_1"``,
        or ``None`` for no running turn.
    :returns: None.
    """
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_test",
            socket_path=str(bridge_dir / "app-server.sock"),
            thread_id="thread_test",
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id=active_turn_id,
        ),
    )


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Create an isolated bridge directory rooted under ``tmp_path``.

    :param tmp_path: pytest temp directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: Prepared bridge directory.
    """
    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "codex-native")
    return prepare_bridge_dir("bridge_test")


def _write_config(bridge_dir: Path, body: str) -> None:
    """
    Write a ``config.toml`` into the bridge's per-session ``CODEX_HOME``.

    :param bridge_dir: Bridge dir whose ``codex-home/config.toml`` is written.
    :param body: Raw TOML body, e.g. ``'model = "gpt-5.4"\\n'``.
    """
    home = codex_home_for_bridge_dir(bridge_dir)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(body)


def test_read_codex_config_model_returns_top_level_model(bridge_dir: Path) -> None:
    """The top-level ``model`` key (what /model writes) is returned.

    This is the cost gate's source of truth read at evaluation time; if it
    returned the wrong key or None, a ``/model`` downgrade would never take
    effect on the next tool call.
    """
    _write_config(bridge_dir, 'model_provider = "databricks"\nmodel = "gpt-5.4"\n')

    assert read_codex_config_model(bridge_dir) == "gpt-5.4"


def test_read_codex_config_model_none_when_missing(bridge_dir: Path) -> None:
    """No ``config.toml`` → ``None`` (fail-safe), so the caller falls back."""
    assert read_codex_config_model(bridge_dir) is None


def test_read_codex_config_model_none_when_no_model_key(bridge_dir: Path) -> None:
    """A config without a top-level ``model`` key → ``None`` (no invented value)."""
    _write_config(bridge_dir, 'model_reasoning_effort = "medium"\n')

    assert read_codex_config_model(bridge_dir) is None


def test_read_codex_config_model_none_when_unparsable(bridge_dir: Path) -> None:
    """Malformed TOML → ``None``, not a crash (guards a partial write)."""
    _write_config(bridge_dir, 'model = "gpt-5.4\n[broken')

    assert read_codex_config_model(bridge_dir) is None


def test_policy_hook_config_round_trips(bridge_dir: Path) -> None:
    """
    Written Omnigent coordinates read back verbatim for the policy hook.

    The codex hook subprocess depends on this exact payload to reach the
    Omnigent server. A failure (dropped/renamed field) would leave the hook
    unable to POST, silently disabling enforcement.
    """
    write_policy_hook_config(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer abc"},
    )
    config = read_policy_hook_config(bridge_dir)
    assert config == {
        "ap_server_url": "http://127.0.0.1:8787",
        "ap_auth_headers": {"Authorization": "Bearer abc"},
    }


def test_policy_hook_config_absent_returns_none(bridge_dir: Path) -> None:
    """
    Reading before any write returns None (no Omnigent server configured).

    The hook treats None as "nothing to enforce" and no-ops. A failure
    (e.g. raising, or returning a partial dict) would crash the hook or
    make it POST to a missing URL.
    """
    assert read_policy_hook_config(bridge_dir) is None


@pytest.mark.parametrize(
    ("active_turn_id", "completed_turn_id", "expected_return", "expected_active_after"),
    [
        # Matching terminal: the active turn really ended → clear + report
        # cleared, so the forwarder posts idle.
        ("turn_1", "turn_1", True, None),
        # Stale terminal for an older turn while a newer one is live → ignore,
        # leaving the newer turn intact (no premature idle).
        ("turn_1", "turn_2", False, "turn_1"),
        # No-id terminal while a turn is live is ambiguous → ignore. This is
        # the fix: clearing here posted a premature idle that hid the
        # "working" spinner mid-turn while Codex kept streaming.
        ("turn_1", None, False, "turn_1"),
        # No-id terminal with no active turn: nothing to protect → clear is a
        # no-op and reports cleared (the session is already idle).
        (None, None, True, None),
        # Id terminal with no active turn: it matches nothing → ignore.
        (None, "turn_1", False, None),
    ],
)
def test_clear_active_turn_id_if_matches(
    bridge_dir: Path,
    active_turn_id: str | None,
    completed_turn_id: str | None,
    expected_return: bool,
    expected_active_after: str | None,
) -> None:
    """
    Terminal events only clear the active turn when they belong to it.

    Guards the spinner/steering invariant: a terminal event clears the
    active turn (and lets the forwarder post idle) only when it matches
    the live turn. A stale id, or an ambiguous id-less event while a turn
    is live, must be ignored so a still-running turn is not marked idle.

    :param bridge_dir: Isolated bridge directory fixture.
    :param active_turn_id: Active turn id seeded before the call, e.g.
        ``"turn_1"``, or ``None`` for no running turn.
    :param completed_turn_id: Terminal event's turn id, e.g. ``"turn_1"``,
        or ``None`` when Codex omitted it.
    :param expected_return: Expected ``clear_active_turn_id_if_matches``
        return — ``True`` means the forwarder will post idle.
    :param expected_active_after: Expected ``active_turn_id`` afterward.
    :returns: None.
    """
    _seed_active_turn(bridge_dir, active_turn_id)

    result = clear_active_turn_id_if_matches(bridge_dir, completed_turn_id)

    # Return value drives whether the forwarder posts idle. A wrong True on
    # the (active="turn_1", completed=None) row is the spinner bug: idle
    # posted mid-turn. A wrong False on the matching row would leave the
    # spinner stuck on after the turn really ended.
    assert result is expected_return
    state = read_bridge_state(bridge_dir)
    assert state is not None
    # The cleared/preserved active turn id also governs steering: a turn
    # wrongly cleared here means later web messages stop steering it.
    assert state.active_turn_id == expected_active_after


def test_clear_active_turn_id_if_matches_no_state_returns_true(bridge_dir: Path) -> None:
    """
    With no bridge state on disk, clearing is a no-op that reports cleared.

    A missing state file means there is no turn to protect, so the helper
    returns True (nothing to ignore). A failure (returning False) would
    make the forwarder treat a normal terminal as stale and never post
    idle, hanging the spinner.
    """
    # bridge_dir exists (fixture) but no state.json was written.
    assert clear_active_turn_id_if_matches(bridge_dir, "turn_1") is True


def test_bridge_startup_error_round_trips_and_is_cleared(bridge_dir: Path) -> None:
    """
    The startup-error breadcrumb round-trips, and ``clear_bridge_state``
    drops it before each launch so stale failures don't linger (issue #59).
    """
    assert read_bridge_startup_error(bridge_dir) is None

    write_bridge_startup_error(bridge_dir, "thread never started (TimeoutError)")
    assert read_bridge_startup_error(bridge_dir) == "thread never started (TimeoutError)"

    clear_bridge_state(bridge_dir)
    assert read_bridge_startup_error(bridge_dir) is None


def test_prepare_bridge_dir_writes_owner_pid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_bridge_dir records the creating process pid so the
    periodic sweep can prune the dir only when its owner is dead (O6)."""
    import os

    from omnigent.codex_native_bridge import prepare_bridge_dir

    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "codex-native")
    bridge_dir = prepare_bridge_dir("bridge_owner")
    marker = bridge_dir / "owner.pid"
    assert marker.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_cleanup_bridge_dir_removes_the_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cleanup_bridge_dir rmtrees the per-session dir (O6)."""
    from omnigent.codex_native_bridge import cleanup_bridge_dir, prepare_bridge_dir

    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "codex-native")
    bridge_dir = prepare_bridge_dir("bridge_cleanup")
    assert bridge_dir.exists()

    removed = cleanup_bridge_dir("bridge_cleanup")

    assert removed is True
    assert not bridge_dir.exists()
    # Idempotent: a second call is a safe no-op.
    assert cleanup_bridge_dir("bridge_cleanup") is False


def test_prune_orphaned_bridge_dirs_only_dead_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prune removes only dirs whose owner pid is provably dead;
    live-owner and unmarked dirs survive (owner-pid invariant, O6)."""
    import os
    import subprocess
    import sys

    from omnigent.codex_native_bridge import (
        bridge_root,
        prune_orphaned_bridge_dirs,
    )

    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "codex-native")
    root = bridge_root()
    root.mkdir(parents=True, exist_ok=True)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_dir = root / "deadowner"
    dead_dir.mkdir()
    (dead_dir / "owner.pid").write_text(str(dead.pid), encoding="utf-8")

    live_dir = root / "liveowner"
    live_dir.mkdir()
    (live_dir / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")

    unmarked_dir = root / "unmarked"
    unmarked_dir.mkdir()

    pruned = prune_orphaned_bridge_dirs()

    assert pruned == 1
    assert not dead_dir.exists()
    assert live_dir.exists()
    assert unmarked_dir.exists()
