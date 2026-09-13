"""Tests for native Codex bridge state helpers."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    cancel_pending_mcp_startup,
    clear_active_turn_id_if_matches,
    clear_bridge_state,
    codex_home_for_bridge_dir,
    codex_mcp_config_overrides,
    mcp_startup_waiting_detail,
    pending_mcp_servers,
    prepare_bridge_dir,
    read_bridge_startup_error,
    read_bridge_startup_timeout,
    read_bridge_state,
    read_codex_config_effort,
    read_codex_config_model,
    read_codex_home_config_effort,
    read_codex_home_config_model,
    read_mcp_startup,
    read_policy_hook_config,
    settle_pending_mcp_startup,
    update_active_turn_id,
    update_mcp_server_startup,
    write_bridge_startup_error,
    write_bridge_startup_timeout,
    write_bridge_state,
    write_codex_config_effort,
    write_codex_config_model,
    write_policy_hook_config,
)


def test_codex_mcp_config_overrides_isolate_the_bridge_interpreter(tmp_path: Path) -> None:
    """codex launches serve-mcp with ``-I`` so the workspace can't shadow omnigent.

    The MCP server starts in the session workspace, and without ``-I`` python puts
    that cwd on ``sys.path``, so a workspace that is an omnigent checkout supplies
    the bridge's own package. Every other native bridge passes ``-I`` here.

    :param tmp_path: Stands in for the per-session bridge dir.
    """
    overrides = codex_mcp_config_overrides(tmp_path)

    prefix = "mcp_servers.omnigent.args="
    raw = next(o[len(prefix) :] for o in overrides if o.startswith(prefix))
    assert json.loads(raw)[:4] == [
        "-I",
        "-m",
        "omnigent.harnesses.claude_native.bridge",
        "serve-mcp",
    ]


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
            cwd=str(bridge_dir),
        ),
    )


def test_bridge_state_preserves_native_working_directory(tmp_path: Path) -> None:
    """Bridge state retains the cwd used for web-driven Codex turns."""
    _seed_active_turn(tmp_path, "turn_1")

    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.cwd == str(tmp_path)

    clear_active_turn_id_if_matches(tmp_path, "turn_1")

    updated = read_bridge_state(tmp_path)
    assert updated is not None
    assert updated.cwd == str(tmp_path)


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Create an isolated bridge directory rooted under ``tmp_path``.

    :param tmp_path: pytest temp directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: Prepared bridge directory.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "codex-native"
    )
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


def test_read_codex_home_config_model_reads_a_codex_home_directly(bridge_dir: Path) -> None:
    """A ``CODEX_HOME`` path yields the same model as the bridge-dir reader.

    The runner's model-options endpoint holds the live bridge state's
    ``codex_home``, not the bridge dir, and needs the session's model to
    mark which catalog row this session actually launched on.
    """
    _write_config(bridge_dir, 'model = "gpt-5.6-luna"\n')

    assert read_codex_home_config_model(codex_home_for_bridge_dir(bridge_dir)) == "gpt-5.6-luna"


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


def test_read_codex_config_effort_returns_top_level_effort(bridge_dir: Path) -> None:
    """The top-level ``model_reasoning_effort`` key (what /model writes) is returned.

    This is the forwarder's source of truth for the effort the terminal runs
    at; if it returned the wrong key or ``None``, an in-TUI effort change
    would never mirror to the chat composer.
    """
    _write_config(bridge_dir, 'model = "gpt-5.4"\nmodel_reasoning_effort = "high"\n')

    assert read_codex_config_effort(bridge_dir) == "high"


def test_read_codex_home_config_effort_reads_a_codex_home_directly(bridge_dir: Path) -> None:
    """A ``CODEX_HOME`` path yields the same effort as the bridge-dir reader."""
    _write_config(bridge_dir, 'model_reasoning_effort = "low"\n')

    assert read_codex_home_config_effort(codex_home_for_bridge_dir(bridge_dir)) == "low"


def test_read_codex_config_effort_none_when_missing_or_absent(bridge_dir: Path) -> None:
    """No file, no key, or a non-string value → ``None`` (no invented effort)."""
    assert read_codex_config_effort(bridge_dir) is None

    _write_config(bridge_dir, 'model = "gpt-5.4"\n')
    assert read_codex_config_effort(bridge_dir) is None

    _write_config(bridge_dir, "model_reasoning_effort = 3\n")
    assert read_codex_config_effort(bridge_dir) is None


def test_read_codex_config_effort_none_when_unparsable(bridge_dir: Path) -> None:
    """Malformed TOML → ``None``, not a crash (guards a partial write)."""
    _write_config(bridge_dir, 'model_reasoning_effort = "high\n[broken')

    assert read_codex_config_effort(bridge_dir) is None


def test_write_codex_config_model_replaces_top_level_key(bridge_dir: Path) -> None:
    """The existing top-level ``model`` line is replaced, sections untouched.

    An Omnigent-initiated switch (routing / web picker) must land on the same
    key an in-TUI ``/model`` writes, or the forwarder's next config re-read
    mirrors the stale launch model back and reverts the switch.
    """
    _write_config(
        bridge_dir,
        'model = "databricks-gpt-5-5"\n'
        'model_provider = "databricks"\n'
        "[model_providers.databricks]\n"
        'model = "section-model-not-touched"\n',
    )

    assert write_codex_config_model(bridge_dir, "gpt-5.6-luna") is True
    assert read_codex_config_model(bridge_dir) == "gpt-5.6-luna"
    body = (codex_home_for_bridge_dir(bridge_dir) / "config.toml").read_text()
    assert 'model = "section-model-not-touched"' in body
    assert 'model_provider = "databricks"' in body


def test_write_codex_config_model_inserts_when_absent(bridge_dir: Path) -> None:
    """A config with no top-level ``model`` gains one at the top."""
    _write_config(bridge_dir, 'model_provider = "databricks"\n')

    assert write_codex_config_model(bridge_dir, "gpt-5.6-luna") is True
    assert read_codex_config_model(bridge_dir) == "gpt-5.6-luna"


def test_write_codex_config_model_creates_missing_file(bridge_dir: Path) -> None:
    """No codex-home/config.toml yet → the writer creates it (best-effort)."""
    assert write_codex_config_model(bridge_dir, "gpt-5.6-luna") is True
    assert read_codex_config_model(bridge_dir) == "gpt-5.6-luna"


def test_write_codex_config_effort_replaces_top_level_key(bridge_dir: Path) -> None:
    """The existing top-level ``model_reasoning_effort`` line is replaced.

    An Omnigent-initiated effort change (web composer gear) must land on the
    same key an in-TUI ``/model`` writes, or a fresh forwarder state (thread
    resume / reconnect) re-reads the stale launch effort and mirrors it back,
    silently reverting the composer's pick.
    """
    _write_config(
        bridge_dir,
        'model = "gpt-5.5"\n'
        'model_reasoning_effort = "medium"\n'
        "[model_providers.databricks]\n"
        'model_reasoning_effort = "section-effort-not-touched"\n',
    )

    assert write_codex_config_effort(bridge_dir, "high") is True
    assert read_codex_config_effort(bridge_dir) == "high"
    body = (codex_home_for_bridge_dir(bridge_dir) / "config.toml").read_text()
    assert 'model_reasoning_effort = "section-effort-not-touched"' in body
    assert 'model = "gpt-5.5"' in body


def test_write_codex_config_effort_inserts_when_absent(bridge_dir: Path) -> None:
    """A config with no top-level effort key gains one at the top."""
    _write_config(bridge_dir, 'model = "gpt-5.5"\n')

    assert write_codex_config_effort(bridge_dir, "low") is True
    assert read_codex_config_effort(bridge_dir) == "low"
    assert read_codex_config_model(bridge_dir) == "gpt-5.5"


def test_write_codex_config_effort_creates_missing_file(bridge_dir: Path) -> None:
    """No codex-home/config.toml yet → the writer creates it (best-effort)."""
    assert write_codex_config_effort(bridge_dir, "high") is True
    assert read_codex_config_effort(bridge_dir) == "high"


def test_write_codex_config_effort_replaces_key_after_multiline_array(bridge_dir: Path) -> None:
    """A top-level multiline array must not end the top-level scan early.

    Its continuation lines can begin with ``[`` (nested arrays); mistaking one
    for a table header would miss the existing effort key below the array and
    insert a duplicate at the top — invalid TOML that ``tomllib`` (and codex
    itself) reject, corrupting the mirror rather than just staling it.
    """
    # The continuation line sits at column 0 — valid TOML, and the shape a
    # naive ``startswith("[")`` break mistakes for a table header.
    _write_config(
        bridge_dir,
        "notify = [\n"
        '["notify-send", "Codex"],\n'
        "]\n"
        'model = "gpt-5.5"\n'
        'model_reasoning_effort = "medium"\n',
    )

    assert write_codex_config_effort(bridge_dir, "high") is True
    assert read_codex_config_effort(bridge_dir) == "high"
    body = (codex_home_for_bridge_dir(bridge_dir) / "config.toml").read_text()
    assert body.count("model_reasoning_effort") == 1


def test_write_codex_config_model_replaces_key_after_multiline_array(bridge_dir: Path) -> None:
    """The model writer shares the array-aware scan (same duplicate-key hazard)."""
    _write_config(
        bridge_dir,
        'notify = [\n["notify-send", "Codex"],\n]\nmodel = "databricks-gpt-5-5"\n',
    )

    assert write_codex_config_model(bridge_dir, "gpt-5.6-luna") is True
    assert read_codex_config_model(bridge_dir) == "gpt-5.6-luna"
    body = (codex_home_for_bridge_dir(bridge_dir) / "config.toml").read_text()
    assert len([line for line in body.splitlines() if line.startswith("model =")]) == 1


def test_write_codex_config_effort_replaces_key_after_bracket_in_string(bridge_dir: Path) -> None:
    """Brackets inside string values/comments must not derail the upsert.

    A line-scanning heuristic that counts brackets sees the lone ``[`` in
    ``notify = ["["]`` as an unclosed array and skips every following key,
    inserting a duplicate — invalid TOML. The tomlkit-based upsert parses the
    document, so string/comment content cannot be mistaken for structure.
    """
    _write_config(
        bridge_dir,
        'notify = ["["]\n'
        'model = "gpt-5.5"  # experimental [beta\n'
        'model_reasoning_effort = "medium"\n',
    )

    assert write_codex_config_effort(bridge_dir, "high") is True
    assert read_codex_config_effort(bridge_dir) == "high"
    body = (codex_home_for_bridge_dir(bridge_dir) / "config.toml").read_text()
    assert body.count("model_reasoning_effort") == 1
    # The style-preserving rewrite keeps unrelated lines (and comments) intact.
    assert 'notify = ["["]' in body
    assert "# experimental [beta" in body


def test_write_codex_config_model_clamps_stale_effort_for_capped_model(bridge_dir: Path) -> None:
    """Switching onto a capped model clamps a too-high stale effort line.

    The switched-to thread inherits config.toml's effort; one above the new
    model's ladder would 400 the next turn, so the model write clamps it.
    """
    _write_config(bridge_dir, 'model = "gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n')

    assert write_codex_config_model(bridge_dir, "databricks-glm-5-2") is True
    assert read_codex_config_model(bridge_dir) == "databricks-glm-5-2"
    assert read_codex_config_effort(bridge_dir) == "medium"


def test_write_codex_config_model_replaces_quoted_key(bridge_dir: Path) -> None:
    """A quoted top-level ``"model"`` key is the same key — replaced, not duplicated."""
    _write_config(bridge_dir, '"model" = "databricks-gpt-5-5"\n')

    assert write_codex_config_model(bridge_dir, "gpt-5.6-luna") is True
    assert read_codex_config_model(bridge_dir) == "gpt-5.6-luna"


def test_write_codex_config_effort_false_on_undecodable_or_malformed_file(
    bridge_dir: Path,
) -> None:
    """Undecodable or malformed files → ``False`` (best-effort), never made worse."""
    home = codex_home_for_bridge_dir(bridge_dir)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_bytes(b"\xff\xfe\x00broken")

    assert write_codex_config_effort(bridge_dir, "high") is False
    assert write_codex_config_model(bridge_dir, "gpt-5.6-luna") is False

    # Malformed TOML (e.g. a torn partial write) is refused rather than
    # rewritten into something even a lenient reader cannot recover.
    (home / "config.toml").write_text('model_reasoning_effort = "high\n[broken')
    assert write_codex_config_effort(bridge_dir, "high") is False
    assert write_codex_config_model(bridge_dir, "gpt-5.6-luna") is False


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


def test_active_turn_compare_and_clear_is_atomic_with_concurrent_update(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing turn A cannot overwrite a concurrent turn-B state update."""
    _seed_active_turn(bridge_dir, "turn_a")
    original_write = codex_native_bridge._write_bridge_state_unlocked
    clear_write_entered = threading.Event()
    release_clear_write = threading.Event()
    update_finished = threading.Event()
    clear_result: list[bool] = []

    def blocking_write(path: Path, state: CodexNativeBridgeState) -> None:
        """Hold turn A's clear after its read while it owns the state lock."""
        if state.active_turn_id is None:
            clear_write_entered.set()
            assert release_clear_write.wait(timeout=5.0)
        original_write(path, state)

    monkeypatch.setattr(codex_native_bridge, "_write_bridge_state_unlocked", blocking_write)

    def clear_turn_a() -> None:
        """Clear turn A through the compare-and-swap helper."""
        clear_result.append(clear_active_turn_id_if_matches(bridge_dir, "turn_a"))

    def publish_turn_b() -> None:
        """Publish the newer turn B and signal when its locked update lands."""
        update_active_turn_id(bridge_dir, "turn_b")
        update_finished.set()

    clear_thread = threading.Thread(target=clear_turn_a)
    update_thread = threading.Thread(target=publish_turn_b)
    clear_thread.start()
    assert clear_write_entered.wait(timeout=5.0)
    update_thread.start()
    try:
        assert not update_finished.wait(timeout=0.1), (
            "turn B wrote while turn A's compare-and-clear still held the state lock"
        )
    finally:
        release_clear_write.set()
        clear_thread.join(timeout=5.0)
        update_thread.join(timeout=5.0)

    assert not clear_thread.is_alive()
    assert not update_thread.is_alive()
    assert clear_result == [True]
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.active_turn_id == "turn_b"


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


def test_bridge_startup_timeout_round_trips_and_is_cleared(bridge_dir: Path) -> None:
    """The configured-command marker is bounded and cleared before a new launch."""
    assert read_bridge_startup_timeout(bridge_dir) is None

    write_bridge_startup_timeout(bridge_dir, 120.0)
    assert read_bridge_startup_timeout(bridge_dir) == 120.0

    clear_bridge_state(bridge_dir)
    assert read_bridge_startup_timeout(bridge_dir) is None


@pytest.mark.parametrize("timeout", [0.0, -1.0, 120.1, float("inf"), float("nan")])
def test_bridge_startup_timeout_rejects_unbounded_values(
    bridge_dir: Path,
    timeout: float,
) -> None:
    """Invalid marker values cannot weaken the executor's bounded wait."""
    with pytest.raises(ValueError, match="finite, positive"):
        write_bridge_startup_timeout(bridge_dir, timeout)


@pytest.mark.parametrize("value", [True, "120", 120.1, 10**1000, None])
def test_bridge_startup_timeout_ignores_malformed_file_values(
    bridge_dir: Path,
    value: object,
) -> None:
    """Executor-visible marker parsing fails closed to the legacy wait."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "startup_timeout.json").write_text(
        json.dumps({"timeout_seconds": value}),
        encoding="utf-8",
    )

    assert read_bridge_startup_timeout(bridge_dir) is None


def test_bridge_startup_timeout_ignores_invalid_utf8(bridge_dir: Path) -> None:
    """A marker that is not UTF-8 fails closed to the legacy wait."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "startup_timeout.json").write_bytes(b"\xff")

    assert read_bridge_startup_timeout(bridge_dir) is None


def test_bridge_startup_timeout_ignores_overlong_integer(bridge_dir: Path) -> None:
    """A marker larger than the payload cap fails closed."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "startup_timeout.json").write_text(
        '{"timeout_seconds": ' + "1" * 5000 + "}",
        encoding="utf-8",
    )

    assert read_bridge_startup_timeout(bridge_dir) is None


def test_bridge_startup_timeout_ignores_deeply_nested_json(bridge_dir: Path) -> None:
    """A recursively nested marker cannot escape the fail-closed boundary."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "startup_timeout.json").write_text(
        "[" * 10_000 + "0" + "]" * 10_000,
        encoding="utf-8",
    )

    assert read_bridge_startup_timeout(bridge_dir) is None


def test_mcp_startup_updates_round_trip(bridge_dir: Path) -> None:
    """
    Per-server MCP startup updates accumulate and read back (issue #2058).

    The executor's first-turn gate and the runner's Stop handler both key
    off this map; the ``pending``/``waiting`` views must name exactly the
    servers whose latest status is ``starting``.
    """
    assert read_mcp_startup(bridge_dir) == {}
    assert pending_mcp_servers({}) == []
    assert mcp_startup_waiting_detail({}) is None

    update_mcp_server_startup(bridge_dir, "safe", "starting")
    update_mcp_server_startup(bridge_dir, "storage-console", "starting")
    servers = update_mcp_server_startup(bridge_dir, "safe", "failed", error="handshake failed")

    assert servers == read_mcp_startup(bridge_dir)
    assert read_mcp_startup(bridge_dir) == {
        "safe": {"status": "failed", "error": "handshake failed"},
        "storage-console": {"status": "starting", "error": None},
    }
    # Only still-starting servers are pending; the failed one settled.
    assert pending_mcp_servers(read_mcp_startup(bridge_dir)) == ["storage-console"]
    assert (
        mcp_startup_waiting_detail(read_mcp_startup(bridge_dir))
        == "MCP startup still waiting on storage-console"
    )


def test_cancel_pending_mcp_startup_flips_only_starting(bridge_dir: Path) -> None:
    """
    Stop's local cancel flips ``starting`` servers to ``cancelled`` only.

    Settled servers (ready/failed) must keep their state — rewriting them
    would misreport what actually happened; a second cancel is a no-op so
    a repeated Stop doesn't claim it cancelled anything.
    """
    update_mcp_server_startup(bridge_dir, "safe", "ready")
    update_mcp_server_startup(bridge_dir, "testman", "failed", error="boom")
    update_mcp_server_startup(bridge_dir, "storage-console", "starting")

    assert cancel_pending_mcp_startup(bridge_dir) == ["storage-console"]
    assert read_mcp_startup(bridge_dir) == {
        "safe": {"status": "ready", "error": None},
        "testman": {"status": "failed", "error": "boom"},
        "storage-console": {"status": "cancelled", "error": None},
    }
    # Nothing pending anymore → repeat cancel reports nothing flipped.
    assert cancel_pending_mcp_startup(bridge_dir) == []


def test_settle_pending_mcp_startup_drops_only_starting(bridge_dir: Path) -> None:
    """
    Settling drops unresolved ``starting`` entries and keeps terminal ones.

    Codex never delivers per-server outcomes to Omnigent's observer
    connection, so at settle the unresolved entries are removed rather
    than guessed; a locally-cancelled server must survive so the web band
    can keep saying it was cancelled. A second settle is a no-op.
    """
    update_mcp_server_startup(bridge_dir, "safe", "starting")
    update_mcp_server_startup(bridge_dir, "storage-console", "cancelled")

    servers, changed = settle_pending_mcp_startup(bridge_dir)

    assert changed is True
    assert servers == {"storage-console": {"status": "cancelled", "error": None}}
    assert read_mcp_startup(bridge_dir) == servers
    # Fully settled → nothing to drop, nothing rewritten.
    assert settle_pending_mcp_startup(bridge_dir) == (servers, False)


def test_read_mcp_startup_ignores_malformed_entries(bridge_dir: Path) -> None:
    """
    Malformed or unknown-status entries are dropped on read.

    A corrupt file must degrade to "no state" rather than crash the
    executor gate or feed a bogus status into the web UI.
    """
    (bridge_dir / "mcp_startup.json").write_text("not json")
    assert read_mcp_startup(bridge_dir) == {}

    (bridge_dir / "mcp_startup.json").write_text(
        '{"servers": {"ok": {"status": "ready"}, "bad": {"status": "exploded"},'
        ' "": {"status": "ready"}}}'
    )
    assert read_mcp_startup(bridge_dir) == {"ok": {"status": "ready", "error": None}}


def test_clear_bridge_state_removes_mcp_startup(bridge_dir: Path) -> None:
    """
    ``clear_bridge_state`` drops the MCP startup map with the other
    runtime state, so a relaunch never gates its first turn on a prior
    app-server's startup round.
    """
    update_mcp_server_startup(bridge_dir, "safe", "starting")

    clear_bridge_state(bridge_dir)

    assert read_mcp_startup(bridge_dir) == {}


# ── owner-pid marker + orphan prune (bridge-dir reaping) ────────────────────


def test_prepare_bridge_dir_writes_owner_pid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_bridge_dir records the creating pid so the periodic sweep can
    prune the dir only when its owner is provably dead."""
    import os

    from omnigent.harnesses.codex_native.bridge import prepare_bridge_dir

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "codex-native"
    )

    bridge_dir = prepare_bridge_dir("bridge_owner")

    assert (bridge_dir / "owner.pid").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_prune_orphaned_bridge_dirs_retains_recent_dead_owner_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recent dead-owner bridges remain fully intact during the grace period."""
    root = tmp_path / "codex-native"
    root.mkdir(parents=True)
    monkeypatch.setattr("omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", root)
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    now = 2_000_000_000.0
    monkeypatch.setattr(codex_native_bridge.time, "time", lambda: now)

    dead_dir = root / "deadowner"
    dead_dir.mkdir()
    owner_marker = dead_dir / "owner.pid"
    owner_marker.write_text("999999", encoding="utf-8")
    rollout = (
        dead_dir
        / "codex-home"
        / "sessions"
        / "2026"
        / "09"
        / "09"
        / "rollout-2026-09-09T00-00-00-thread.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    bridge_config = dead_dir / "bridge.json"
    bridge_config.write_text("secret", encoding="utf-8")
    policy_config = dead_dir / "policy_hook.json"
    policy_config.write_text("secret", encoding="utf-8")
    ephemeral_dir = dead_dir / "mcp-runtime"
    ephemeral_dir.mkdir()
    runtime_token = ephemeral_dir / "token"
    runtime_token.write_text("secret", encoding="utf-8")
    os.utime(owner_marker, (now - 60, now - 60))

    def _unexpected_walk(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("recent owner activity should skip rollout scanning")

    monkeypatch.setattr(codex_native_bridge.os, "walk", _unexpected_walk)

    assert codex_native_bridge.prune_orphaned_bridge_dirs() == 0
    assert owner_marker.read_text(encoding="utf-8") == "999999"
    assert rollout.read_text(encoding="utf-8") == '{"type":"session_meta"}\n'
    assert bridge_config.read_text(encoding="utf-8") == "secret"
    assert policy_config.read_text(encoding="utf-8") == "secret"
    assert runtime_token.read_text(encoding="utf-8") == "secret"


def test_prune_orphaned_bridge_dirs_removes_expired_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead-owner bridge inactive for 7 days is removed wholesale."""
    root = tmp_path / "codex-native"
    root.mkdir(parents=True)
    monkeypatch.setattr("omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", root)
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    now = 2_000_000_000.0
    monkeypatch.setattr(codex_native_bridge.time, "time", lambda: now)

    dead_dir = root / "deadowner"
    rollout = (
        dead_dir
        / "codex-home"
        / "sessions"
        / "2026"
        / "09"
        / "09"
        / "rollout-2026-09-09T00-00-00-thread.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    unrelated_jsonl = rollout.parent / "metadata.jsonl"
    unrelated_jsonl.write_text('{"recent":true}\n', encoding="utf-8")
    owner_marker = dead_dir / "owner.pid"
    owner_marker.write_text("999999", encoding="utf-8")
    (dead_dir / "bridge.json").write_text("secret", encoding="utf-8")
    expired_at = now - codex_native_bridge._ORPHAN_RETENTION_SECONDS
    for activity_path in (owner_marker, rollout):
        os.utime(activity_path, (expired_at, expired_at))
    os.utime(unrelated_jsonl, (now - 60, now - 60))

    assert codex_native_bridge.prune_orphaned_bridge_dirs() == 1
    assert not dead_dir.exists()


def test_prune_orphaned_bridge_dirs_uses_latest_rollout_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recent rollout keeps a bridge whose launch and owner marker are old."""
    root = tmp_path / "codex-native"
    root.mkdir(parents=True)
    monkeypatch.setattr("omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", root)
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    now = 2_000_000_000.0
    monkeypatch.setattr(codex_native_bridge.time, "time", lambda: now)

    dead_dir = root / "deadowner"
    rollout = (
        dead_dir
        / "codex-home"
        / "sessions"
        / "2026"
        / "09"
        / "09"
        / "rollout-2026-09-09T00-00-00-thread.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    owner_marker = dead_dir / "owner.pid"
    owner_marker.write_text("999999", encoding="utf-8")
    expired_at = now - codex_native_bridge._ORPHAN_RETENTION_SECONDS - 1
    os.utime(owner_marker, (expired_at, expired_at))
    os.utime(rollout, (now - 60, now - 60))

    assert codex_native_bridge.prune_orphaned_bridge_dirs() == 0
    assert dead_dir.exists()
    assert rollout.exists()


def test_prune_orphaned_bridge_dirs_retains_bridge_when_rollout_scan_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete rollout scan fails closed instead of deleting the bridge."""
    root = tmp_path / "codex-native"
    root.mkdir(parents=True)
    monkeypatch.setattr("omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", root)
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    now = 2_000_000_000.0
    monkeypatch.setattr(codex_native_bridge.time, "time", lambda: now)

    dead_dir = root / "deadowner"
    sessions_dir = dead_dir / "codex-home" / "sessions"
    sessions_dir.mkdir(parents=True)
    owner_marker = dead_dir / "owner.pid"
    owner_marker.write_text("999999", encoding="utf-8")
    expired_at = now - codex_native_bridge._ORPHAN_RETENTION_SECONDS - 1
    os.utime(owner_marker, (expired_at, expired_at))

    def _failed_walk(
        _root: Path,
        *,
        onerror: object,
    ) -> list[tuple[str, list[str], list[str]]]:
        assert callable(onerror)
        onerror(PermissionError("rollout directory unreadable"))
        return []

    monkeypatch.setattr(codex_native_bridge.os, "walk", _failed_walk)

    assert codex_native_bridge.prune_orphaned_bridge_dirs() == 0
    assert dead_dir.exists()


def test_prune_orphaned_bridge_dirs_keeps_live_and_unmarked_bridges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live-owner and unmarked bridges remain even when old."""
    root = tmp_path / "codex-native"
    root.mkdir(parents=True)
    monkeypatch.setattr("omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", root)
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda pid: pid == os.getpid())
    now = 2_000_000_000.0
    monkeypatch.setattr(codex_native_bridge.time, "time", lambda: now)
    expired_at = now - codex_native_bridge._ORPHAN_RETENTION_SECONDS - 1

    dead_dir = root / "deadowner"
    dead_dir.mkdir()
    dead_marker = dead_dir / "owner.pid"
    dead_marker.write_text("999999", encoding="utf-8")
    os.utime(dead_marker, (expired_at, expired_at))

    live_dir = root / "liveowner"
    live_dir.mkdir()
    live_marker = live_dir / "owner.pid"
    live_marker.write_text(str(os.getpid()), encoding="utf-8")
    os.utime(live_marker, (expired_at, expired_at))

    unmarked_dir = root / "unmarked"
    unmarked_dir.mkdir()

    assert codex_native_bridge.prune_orphaned_bridge_dirs() == 1
    assert not dead_dir.exists()
    assert live_dir.exists()
    assert unmarked_dir.exists()
