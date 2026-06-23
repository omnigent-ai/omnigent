"""Tests for native Antigravity bridge state helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.antigravity_native_bridge as _mod
from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR,
    ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    AntigravityNativeBridgeState,
    build_antigravity_native_spawn_env,
    clear_bridge_state,
    ensure_agy_onboarding_complete,
    inject_user_message_via_tui,
    prepare_bridge_dir,
    read_bridge_state,
    read_tmux_info,
    update_conversation_id,
    write_bridge_state,
    write_tmux_target,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_active_turn(bridge_dir: Path, active_turn_id: str | None) -> None:
    """
    Write bridge state with a given active turn id.

    :param bridge_dir: Native Antigravity bridge directory.
    :param active_turn_id: Active turn id to seed, e.g. ``"turn_1"``,
        or ``None`` for no running turn.
    :returns: None.
    """
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_test",
            conversation_id="conv_agy_test",
            active_turn_id=active_turn_id,
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Create an isolated bridge directory rooted under ``tmp_path``.

    :param tmp_path: pytest temp directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: Prepared bridge directory.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    return prepare_bridge_dir("bridge_test")


# ---------------------------------------------------------------------------
# State roundtrip
# ---------------------------------------------------------------------------


def test_state_roundtrip_full(bridge_dir: Path) -> None:
    """
    Written state reads back equal, including active_turn_id when set.

    Guards the full field set: a dropped or renamed field would cause the
    harness or forwarder to read stale/None values and lose track of the
    conversation.
    """
    state = AntigravityNativeBridgeState(
        session_id="conv_abc123",
        conversation_id="agy_conv_xyz",
        active_turn_id="turn_abc123",
    )
    write_bridge_state(bridge_dir, state)
    read_back = read_bridge_state(bridge_dir)
    assert read_back == state


def test_state_roundtrip_active_turn_none(bridge_dir: Path) -> None:
    """
    Written state with active_turn_id=None reads back with active_turn_id=None.

    Guards the idle state: the default field value must survive the JSON
    roundtrip so the forwarder does not wrongly report a running turn.
    """
    state = AntigravityNativeBridgeState(
        session_id="conv_abc123",
        conversation_id="agy_conv_xyz",
        active_turn_id=None,
    )
    write_bridge_state(bridge_dir, state)
    read_back = read_bridge_state(bridge_dir)
    assert read_back == state
    assert read_back is not None
    assert read_back.active_turn_id is None


def test_state_roundtrip_conversation_id(bridge_dir: Path) -> None:
    """
    The conversation_id field roundtrips verbatim.

    The forwarder discovers agy's real UUID and writes it here; a dropped or
    mangled value would break resume (which reads this id back to pass to
    ``agy --conversation``).
    """
    state = AntigravityNativeBridgeState(
        session_id="conv_abc",
        conversation_id="68caaeac-2eaf-4e2c-9b95-721b022f4903",
    )
    write_bridge_state(bridge_dir, state)
    read_back = read_bridge_state(bridge_dir)
    assert read_back is not None
    assert read_back.conversation_id == "68caaeac-2eaf-4e2c-9b95-721b022f4903"


# ---------------------------------------------------------------------------
# legacy agy_pid key (removed field)
# ---------------------------------------------------------------------------


def test_read_bridge_state_ignores_legacy_agy_pid_key(bridge_dir: Path) -> None:
    """
    A legacy state.json carrying the removed ``agy_pid`` key reads back fine.

    The ``agy_pid`` field was removed (agy is launched under
    ``tmux_start_on_attach`` so no pid exists at launch; the executor always
    discovers the connect-RPC port at injection time). A state file written by
    an older build must not crash the reader; the obsolete key is ignored and
    the remaining fields parse normally.

    :param bridge_dir: Isolated bridge directory fixture.
    :returns: None.
    """
    (bridge_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": "conv_abc",
                "conversation_id": "agy_conv",
                "active_turn_id": "turn_1",
                "agy_pid": 72753,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.session_id == "conv_abc"
    assert state.conversation_id == "agy_conv"
    assert state.active_turn_id == "turn_1"
    assert not hasattr(state, "agy_pid")


def test_write_bridge_state_omits_agy_pid_key(bridge_dir: Path) -> None:
    """
    Written state.json never contains an ``agy_pid`` key.

    The field was removed, so a fresh write must not re-introduce it (a stray
    key would mislead any tooling that still inspects the file).

    :param bridge_dir: Isolated bridge directory fixture.
    :returns: None.
    """
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_abc", conversation_id="agy_conv"),
    )
    raw = json.loads((bridge_dir / "state.json").read_text(encoding="utf-8"))
    assert "agy_pid" not in raw


# ---------------------------------------------------------------------------
# read_bridge_state — None branches
# ---------------------------------------------------------------------------


def test_read_bridge_state_missing_dir_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Reading from a non-existent bridge directory returns None.

    Guards startup safety: if the harness starts before the wrapper has
    written state, the caller must gracefully wait rather than crash.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    missing = prepare_bridge_dir("never_written")
    # Don't write any state — the directory exists but state.json does not.
    assert read_bridge_state(missing) is None


def test_read_bridge_state_corrupt_json_returns_none(bridge_dir: Path) -> None:
    """
    Corrupt JSON in state.json returns None, not an exception.

    Guards partial-write resilience: if the file is truncated mid-write
    the reader must degrade gracefully rather than crash the harness.
    """
    (bridge_dir / "state.json").write_text("{not valid json", encoding="utf-8")
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_missing_required_fields_returns_none(bridge_dir: Path) -> None:
    """
    JSON missing required fields returns None.

    Guards schema enforcement: an incomplete state (e.g. old format) must
    not be returned as a partially-valid object that causes attribute errors.
    """
    (bridge_dir / "state.json").write_text('{"session_id": "conv_abc"}\n', encoding="utf-8")
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_missing_conversation_id_returns_none(bridge_dir: Path) -> None:
    """
    JSON missing conversation_id returns None.

    conversation_id is required (the forwarder/resume both depend on it); a
    state lacking it must be rejected rather than returned with an empty id.
    """
    (bridge_dir / "state.json").write_text(
        json.dumps({"session_id": "conv_abc", "active_turn_id": None}) + "\n",
        encoding="utf-8",
    )
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_empty_conversation_id_returns_none(bridge_dir: Path) -> None:
    """
    An empty-string conversation_id returns None.

    Guards the non-empty contract: an empty id never names a real agy brain
    dir, so it must be rejected rather than handed to resume.
    """
    (bridge_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": "conv_abc",
                "conversation_id": "",
                "active_turn_id": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_bridge_state(bridge_dir) is None


def test_read_bridge_state_ignores_legacy_sidecar_fields(bridge_dir: Path) -> None:
    """
    Legacy state.json carrying removed fields still reads back via the current
    (reduced) schema.

    Guards forward-compat across the Task 12 cutover: a state file written by a
    prior build must not crash the reader. This covers the removed sidecar_port /
    data_dir fields AND the durable read cursor (forwarded_step_index /
    forwarded_steps) the transcript forwarder persisted before the RPC reader
    superseded it — all obsolete keys are simply ignored.
    """
    (bridge_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": "conv_abc",
                "sidecar_port": 9000,
                "conversation_id": "agy_conv",
                "data_dir": "/tmp/data",
                "active_turn_id": None,
                # Retired durable cursor keys (Task 12 cutover): a forwarder-era
                # state.json carries these; the reader must tolerate + drop them.
                "forwarded_step_index": 14,
                "forwarded_steps": [0, 2, 14],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.session_id == "conv_abc"
    assert state.conversation_id == "agy_conv"
    assert state.active_turn_id is None
    # The retired cursor fields are gone from the dataclass entirely.
    assert not hasattr(state, "forwarded_step_index")
    assert not hasattr(state, "forwarded_steps")


# ---------------------------------------------------------------------------
# build_antigravity_native_spawn_env
# ---------------------------------------------------------------------------


def test_build_spawn_env_sets_both_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    build_antigravity_native_spawn_env sets both required env vars.

    The harness executor depends on both env vars being present to locate the
    bridge directory and correlate the request session. A missing var would
    leave the executor unable to read state or match the session.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    env = build_antigravity_native_spawn_env("conv_abc123")
    assert ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR in env
    assert ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR in env
    assert env[ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR] == "conv_abc123"


def test_build_spawn_env_bridge_id_defaults_to_conversation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When bridge_id is None, the conversation_id is used as the bridge_id.

    Guards the default behaviour: the bridge dir must be deterministic even
    when no explicit bridge_id label is attached to the conversation.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    env_default = build_antigravity_native_spawn_env("conv_abc123")
    env_explicit = build_antigravity_native_spawn_env("conv_abc123", bridge_id="conv_abc123")
    assert (
        env_default[ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR]
        == env_explicit[ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR]
    )


def test_build_spawn_env_explicit_bridge_id_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An explicit bridge_id produces a different bridge dir than the conversation_id.

    Guards bridge isolation: two conversations sharing a bridge_id (label-based
    routing) must resolve to the same directory, while distinct ids must not
    collide.
    """
    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    env_conv = build_antigravity_native_spawn_env("conv_abc123")
    env_bridge = build_antigravity_native_spawn_env("conv_abc123", bridge_id="bridge_xyz")
    assert (
        env_conv[ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR]
        != env_bridge[ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR]
    )


# ---------------------------------------------------------------------------
# prepare_bridge_dir permissions
# ---------------------------------------------------------------------------


def test_prepare_bridge_dir_creates_0o700_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    prepare_bridge_dir creates the directory with mode 0o700.

    Guards filesystem privacy: bridge dirs contain auth tokens and session
    ids. A wider mode (e.g. 0o755) would expose those to other local users.
    """
    import stat

    monkeypatch.setattr(_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    bd = prepare_bridge_dir("bridge_perms_test")
    assert bd.is_dir()
    mode = stat.S_IMODE(bd.stat().st_mode)
    assert mode == 0o700


# ---------------------------------------------------------------------------
# update_conversation_id
# ---------------------------------------------------------------------------


def test_update_conversation_id_mutates_correctly(bridge_dir: Path) -> None:
    """
    update_conversation_id updates the conversation_id and active_turn_id.

    Guards conversation rotation: when agy creates a fresh conversation while
    the Omnigent session stays the same, the bridge must reflect the new id.
    """
    _seed_active_turn(bridge_dir, "turn_old")
    assert update_conversation_id(bridge_dir, "agy_conv_new", active_turn_id="turn_fresh") is True
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == "agy_conv_new"
    assert state.active_turn_id == "turn_fresh"


def test_update_conversation_id_clears_active_turn_by_default(bridge_dir: Path) -> None:
    """
    update_conversation_id with no active_turn_id clears the active turn.

    Guards the default: a new conversation starts with no active turn unless
    the caller explicitly supplies one.
    """
    _seed_active_turn(bridge_dir, "turn_old")
    assert update_conversation_id(bridge_dir, "agy_conv_new") is True
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.conversation_id == "agy_conv_new"
    assert state.active_turn_id is None


def test_update_conversation_id_returns_false_and_warns_when_no_state(
    bridge_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    update_conversation_id returns False and WARNs when there is no state.

    Guards observability (Fix C): on a missing/invalid state file the real
    cascade id must NOT be silently dropped — the function returns ``False`` and
    logs a WARNING naming the dropped id so the cold-start caller can report that
    the reader will stay bound to the ``agy_conv_*`` placeholder.
    """
    with caplog.at_level(logging.WARNING, logger="omnigent.antigravity_native_bridge"):
        result = update_conversation_id(bridge_dir, "agy_conv_new")  # empty bridge dir
    assert result is False
    assert read_bridge_state(bridge_dir) is None
    # The WARNING names the dropped conversation id.
    assert any(
        record.levelno == logging.WARNING and "agy_conv_new" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# clear_bridge_state
# ---------------------------------------------------------------------------


def test_clear_bridge_state_removes_state(bridge_dir: Path) -> None:
    """
    clear_bridge_state removes state.json so read_bridge_state returns None.

    Guards stale-state cleanup: before a new agy launch the old state must
    be removed so the harness waits for the new state instead of reading
    stale sidecar coordinates.
    """
    _seed_active_turn(bridge_dir, None)
    assert read_bridge_state(bridge_dir) is not None
    clear_bridge_state(bridge_dir)
    assert read_bridge_state(bridge_dir) is None


def test_clear_bridge_state_noop_when_absent(bridge_dir: Path) -> None:
    """
    clear_bridge_state is a no-op when state.json does not exist.

    Guards idempotency: calling clear before the first write must not raise.
    """
    clear_bridge_state(bridge_dir)  # must not raise


# ---------------------------------------------------------------------------
# Onboarding marker (ensure_agy_onboarding_complete)
# ---------------------------------------------------------------------------

_COMPLETE_STATE = {
    "consumerOnboardingComplete": True,
    "enterpriseOnboardingComplete": False,
    "onboardingComplete": True,
}


@pytest.fixture
def agy_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Redirect the agy onboarding marker into an isolated temp HOME.

    :param tmp_path: pytest temp directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The (not-yet-created) marker path under ``tmp_path``.
    """
    marker = tmp_path / ".gemini" / "antigravity-cli" / "cache" / "onboarding.json"
    monkeypatch.setattr(_mod, "_AGY_ONBOARDING_MARKER", marker)
    return marker


def test_ensure_onboarding_writes_marker_and_parents_when_absent(agy_marker: Path) -> None:
    """
    A fresh host (no marker, no cache dir) gets the complete-state file written.

    Guards the core wizard-suppression: agy reads ``onboardingComplete: true`` and
    skips the first-run TUI, and the nested ``cache/`` parents are created.
    """
    assert not agy_marker.exists()
    ensure_agy_onboarding_complete()
    assert agy_marker.is_file()
    assert json.loads(agy_marker.read_text(encoding="utf-8")) == _COMPLETE_STATE


def test_ensure_onboarding_is_idempotent_no_rewrite(agy_marker: Path) -> None:
    """
    When all three keys are already complete the file is left byte-for-byte intact.

    Pre-write a non-canonical encoding (custom key order, no trailing newline); a
    returning user's agy state must not be churned, so an early return — not a
    re-serialize — is required.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    original = '{"onboardingComplete": true, "consumerOnboardingComplete": true, "enterpriseOnboardingComplete": false}'  # noqa: E501
    agy_marker.write_text(original, encoding="utf-8")
    ensure_agy_onboarding_complete()
    assert agy_marker.read_text(encoding="utf-8") == original


def test_ensure_onboarding_preserves_unknown_keys_and_completes(agy_marker: Path) -> None:
    """
    A partial/older marker is upgraded: unknown keys survive, the gate flips True.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_text(json.dumps({"custom": 7, "onboardingComplete": False}), encoding="utf-8")
    ensure_agy_onboarding_complete()
    result = json.loads(agy_marker.read_text(encoding="utf-8"))
    assert result["custom"] == 7
    assert result["onboardingComplete"] is True
    assert result["consumerOnboardingComplete"] is True
    assert result["enterpriseOnboardingComplete"] is False


def test_ensure_onboarding_overwrites_corrupt_file(agy_marker: Path) -> None:
    """
    A non-JSON marker (regenerable, non-secret cache) is overwritten, not raised on.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_text("not json {{{", encoding="utf-8")
    ensure_agy_onboarding_complete()
    assert json.loads(agy_marker.read_text(encoding="utf-8")) == _COMPLETE_STATE


def test_ensure_onboarding_overwrites_non_object_json(agy_marker: Path) -> None:
    """
    Valid-but-non-object JSON (e.g. a list) is treated as absent and replaced.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_text("[1, 2, 3]", encoding="utf-8")
    ensure_agy_onboarding_complete()
    assert json.loads(agy_marker.read_text(encoding="utf-8")) == _COMPLETE_STATE


def test_ensure_onboarding_normalises_numeric_truthy_values(agy_marker: Path) -> None:
    """
    A marker storing numeric 1/0 (which ``==`` True/False in Python) is rewritten
    to real JSON booleans — the early-return uses identity, not equality, so a
    numeric file is not mistaken for the boolean complete-state.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_text(
        json.dumps(
            {
                "consumerOnboardingComplete": 1,
                "enterpriseOnboardingComplete": 0,
                "onboardingComplete": 1,
            }
        ),
        encoding="utf-8",
    )
    ensure_agy_onboarding_complete()
    result = json.loads(agy_marker.read_text(encoding="utf-8"))
    assert result["onboardingComplete"] is True
    assert result["consumerOnboardingComplete"] is True
    assert result["enterpriseOnboardingComplete"] is False


def test_ensure_onboarding_raises_when_marker_dir_unwritable(agy_marker: Path) -> None:
    """
    An un-creatable marker directory surfaces OSError (fail-loud), not a silent
    skip — a missing marker would otherwise hang agy on the wizard.
    """
    # Put a regular FILE where the ``cache`` directory must be so mkdir() fails.
    agy_marker.parent.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.parent.write_text("not a dir", encoding="utf-8")
    with pytest.raises(OSError):
        ensure_agy_onboarding_complete()


def test_ensure_onboarding_overwrites_non_utf8_file(agy_marker: Path) -> None:
    """
    A non-UTF-8 marker is treated as corrupt and regenerated, not raised on —
    UnicodeDecodeError is a ValueError, caught alongside JSONDecodeError.
    """
    agy_marker.parent.mkdir(parents=True, exist_ok=True)
    agy_marker.write_bytes(b"\xff\xfe\x00 not utf-8 \x80\x81")
    ensure_agy_onboarding_complete()
    assert json.loads(agy_marker.read_text(encoding="utf-8")) == _COMPLETE_STATE


# ---------------------------------------------------------------------------
# tmux target advertisement (write_tmux_target / read_tmux_info)
# ---------------------------------------------------------------------------


def test_write_read_tmux_target_roundtrip(tmp_path: Path) -> None:
    """A written tmux target reads back with its socket path and pane target."""
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/omnigent-x/tmux.sock"),
        tmux_target="main",
    )
    info = read_tmux_info(bridge_dir)
    assert info == {"socket_path": "/tmp/omnigent-x/tmux.sock", "tmux_target": "main"}


def test_read_tmux_info_missing_returns_none(tmp_path: Path) -> None:
    """A bridge dir with no ``tmux.json`` yields ``None`` (not an error)."""
    assert read_tmux_info(tmp_path / "bridge") is None


def test_read_tmux_info_rejects_malformed_json(tmp_path: Path) -> None:
    """A corrupt ``tmux.json`` is treated as absent rather than raising."""
    bridge_dir = prepare_bridge_dir("bridge_malformed")
    (bridge_dir / "tmux.json").write_text("{not json", encoding="utf-8")
    assert read_tmux_info(bridge_dir) is None


def test_read_tmux_info_rejects_missing_fields(tmp_path: Path) -> None:
    """A ``tmux.json`` lacking a non-empty target/socket is rejected."""
    bridge_dir = prepare_bridge_dir("bridge_partial")
    (bridge_dir / "tmux.json").write_text(json.dumps({"socket_path": "/s"}), encoding="utf-8")
    assert read_tmux_info(bridge_dir) is None
    (bridge_dir / "tmux.json").write_text(
        json.dumps({"socket_path": "", "tmux_target": "main"}), encoding="utf-8"
    )
    assert read_tmux_info(bridge_dir) is None


def test_clear_bridge_state_removes_tmux_json(tmp_path: Path) -> None:
    """
    Clearing runtime state also drops the advertised tmux pane.

    A relaunch opens a new pane (and, after a host restart, a new socket), so a
    surviving ``tmux.json`` would let the executor bootstrap the first turn
    against the prior run's pane.
    """
    bridge_dir = prepare_bridge_dir("bridge_clear")
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_1", conversation_id="cid-1"),
    )
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/a/tmux.sock"), tmux_target="main")
    assert read_tmux_info(bridge_dir) is not None
    clear_bridge_state(bridge_dir)
    assert read_bridge_state(bridge_dir) is None
    assert read_tmux_info(bridge_dir) is None


# ---------------------------------------------------------------------------
# Paste payload encoding + submit needle
# ---------------------------------------------------------------------------


def test_paste_payload_bytes_maps_newlines_to_cr_and_strips_controls() -> None:
    """
    Newlines become CR (so the TUI keeps multi-line input as data under
    bracketed paste), tabs are kept, and other control bytes are dropped (a
    stray ESC would close the bracketed paste early).
    """
    payload = _mod._paste_payload_bytes("a\nb\r\nc\td\x1b\x00e")
    # \n and \r\n both collapse to a single CR; \t kept; ESC + NUL dropped.
    assert payload == b"a\rb\rc\tde"


def test_submit_needle_uses_first_substantial_line() -> None:
    """The needle is the first line with >=4 non-space chars, capped at 24."""
    needle = _mod._submit_needle("  \nHello there, this is a long first line")
    assert needle == "Hello there, this is a l"
    assert _mod._submit_needle("hi") == ""


# ---------------------------------------------------------------------------
# First-turn TUI delivery (inject_user_message_via_tui)
# ---------------------------------------------------------------------------


@pytest.fixture
def _fast_tmux_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the TUI delivery polls/timeouts so subprocess tests run fast."""
    monkeypatch.setattr(_mod, "_TMUX_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(_mod, "_PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr(_mod, "_PASTE_COMMIT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(_mod, "_SUBMIT_VERIFY_TIMEOUT_S", 0.1)


def test_inject_user_message_via_tui_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    The delivery clears, pastes via buffer, and submits with a verified Enter.

    The fake models agy's TUI: idle (``? for shortcuts``) before the paste, the
    draft visible after the paste, and a running turn (``esc to cancel``) once
    Enter submits — exercising the readiness, paste-commit, and submit-verify
    gates that a static pane would stall.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    content = "Reply with exactly WORKING"
    captured: list[list[str]] = []
    loaded: list[bytes] = []
    tui = {"pane": "> \n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Record delivery calls; drive the simulated agy input box."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "load-buffer" in cmd:
            loaded.append(Path(cmd[-1]).read_bytes())
        if "paste-buffer" in cmd:
            tui["pane"] = f"> {content}\n? for shortcuts"
        if cmd[-1] == "Enter":
            tui["pane"] = "> \nesc to cancel"
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content=content, timeout_s=0.5)

    # C-a, C-k (clear), load-buffer, paste-buffer, Enter.
    assert len(captured) == 5, f"expected 5 delivery calls, got {captured}"
    clear_home, clear_kill, load, paste, submit = captured
    assert clear_home[-1] == "C-a"
    assert clear_kill[-1] == "C-k"
    assert loaded == [b"Reply with exactly WORKING\r"]
    assert load == [
        "tmux",
        "-S",
        "/tmp/ex/tmux.sock",
        "load-buffer",
        "-b",
        "omnigent-agy-paste",
        load[-1],  # the temp paste file path (unlinked after the paste)
    ]
    assert paste == [
        "tmux",
        "-S",
        "/tmp/ex/tmux.sock",
        "paste-buffer",
        "-p",
        "-d",
        "-b",
        "omnigent-agy-paste",
        "-t",
        "main",
    ]
    assert submit == ["tmux", "-S", "/tmp/ex/tmux.sock", "send-keys", "-t", "main", "Enter"]


def test_inject_user_message_via_tui_resends_enter_when_coalesced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A first Enter folded into the paste burst is re-sent until the turn starts.

    The fake leaves the pane idle after the first Enter (the submit was
    swallowed) and starts the turn only on the second — the delivery must not
    give up after one Enter.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    enters = {"n": 0}
    tui = {"pane": "> hi there\n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Activate the turn only on the second Enter."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if cmd[-1] == "Enter":
            enters["n"] += 1
            if enters["n"] >= 2:
                tui["pane"] = "> \nesc to cancel"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content="hi there", timeout_s=0.5)
    assert enters["n"] == 2, "expected the submit Enter to be re-sent once"


def test_inject_user_message_via_tui_accepts_changed_active_footer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    The submit is confirmed when the footer leaves idle, even if the running
    footer text differs from the known marker.

    A future agy build could rename the running footer or a narrow pane could
    truncate it; as long as the idle ``? for shortcuts`` marker is gone from a
    non-empty pane, the turn is treated as started — so a working submit is not
    misread as stuck and falsely failed.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    tui = {"pane": "> hi\n? for shortcuts"}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Idle until Enter; then a non-idle footer that is NOT the known marker."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if cmd[-1] == "Enter":
            tui["pane"] = "> \n(generating response, press the cancel key to stop)"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    # Must not raise: the idle marker is gone, so the submit is confirmed.
    inject_user_message_via_tui(bridge_dir, content="hi", timeout_s=0.5)


def test_inject_user_message_via_tui_mid_turn_sends_one_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A steer delivered while agy is mid-turn sends exactly one Enter, no re-send.

    The footer is already ``esc to cancel`` (a turn is running), so the
    idle->running verification is unavailable and a second Enter could queue a
    spurious empty turn. The delivery must still pass readiness (active footer
    accepted, not waiting for an idle footer that will not appear), paste, and
    submit a single best-effort Enter without spinning the retry budget or
    raising.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    enters = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """agy is mid-turn the entire time: the footer never leaves ``esc to cancel``."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout="> steer me\nesc to cancel", stderr="")
        if cmd[-1] == "Enter":
            enters["n"] += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content="steer me", timeout_s=0.5)
    assert enters["n"] == 1, "mid-turn submit must send exactly one Enter (no re-send)"


def test_submit_verify_ignores_single_transient_nonidle_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A lone non-idle capture (a mid-repaint frame) must not be read as submitted.

    The "idle marker gone" signal is only accepted on two CONSECUTIVE polls. A
    single transient frame with neither marker — followed by the idle footer
    again — resets the counter, so the submit is confirmed only once the active
    marker actually appears. Otherwise an early return on a redraw glitch could
    cost the caller a full state-wait before a "did not register" error.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    # capture-pane sequence after Enter: idle, one transient blank frame, idle
    # again, then the running footer. A correct 2-consecutive rule reaches index 3.
    panes = [
        "> hi\n? for shortcuts",  # idle
        "> \n",  # transient: neither marker (mid-repaint)
        "> hi\n? for shortcuts",  # idle again — resets the non-idle counter
        "> \nesc to cancel",  # turn running — the real confirmation
    ]
    captures = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Serve the scripted pane sequence; never empty so liveness stays true."""
        del kwargs
        if "has-session" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "capture-pane" in cmd:
            idx = min(captures["n"], len(panes) - 1)
            captures["n"] += 1
            return SimpleNamespace(returncode=0, stdout=panes[idx], stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message_via_tui(bridge_dir, content="hi", timeout_s=0.5)
    # Must have polled past the transient frame to the running footer (index 3),
    # i.e. it did NOT return early on the lone non-idle frame at index 1.
    assert captures["n"] >= 4


def test_inject_user_message_via_tui_raises_when_target_never_advertised(
    tmp_path: Path,
) -> None:
    """No ``tmux.json`` → a loud RuntimeError (the turn must not silently vanish)."""
    with pytest.raises(RuntimeError, match="tmux target was not advertised"):
        inject_user_message_via_tui(tmp_path / "bridge", content="hi", timeout_s=0.0)


def test_inject_user_message_via_tui_raises_when_session_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """A dead tmux pane (agy TUI exited) fails fast with a restart hint."""
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """has-session reports the pane is gone."""
        del kwargs
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="no longer running"):
        inject_user_message_via_tui(bridge_dir, content="hi", timeout_s=0.5)


def test_inject_user_message_via_tui_raises_when_session_dies_before_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    A TUI that exits between the readiness check and the submit fails fast.

    The entry liveness check passes, but ``_submit_and_verify`` re-checks before
    each Enter — so a pane that dies after the paste raises the specific
    "exited before the message could be submitted" error instead of spinning the
    full submit budget and then blaming paste-coalescing.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")
    has_session_calls = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Alive at the entry check; dead by the time the submit re-checks."""
        del kwargs
        if "has-session" in cmd:
            has_session_calls["n"] += 1
            # 1st call = entry gate (alive); later calls = per-submit re-check (dead).
            return SimpleNamespace(returncode=0 if has_session_calls["n"] == 1 else 1, stdout="")
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout="> draft\n? for shortcuts", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="exited before the message could be submitted"):
        inject_user_message_via_tui(bridge_dir, content="draft message", timeout_s=0.5)


def test_inject_user_message_via_tui_raises_when_turn_never_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fast_tmux_timeouts: None,
) -> None:
    """
    If no turn ever starts after the submit attempts, the delivery raises.

    The pane stays idle forever (Enter never takes), so after the bounded
    re-send budget the executor gets a clear error rather than a false success.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/ex/tmux.sock"), tmux_target="main")

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Pane never leaves idle — the submit never starts a turn."""
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout="> draft\n? for shortcuts", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="did not start a turn"):
        inject_user_message_via_tui(bridge_dir, content="draft message", timeout_s=0.5)


def test_inject_user_message_via_tui_rejects_empty_content(tmp_path: Path) -> None:
    """Empty content is a programming error, not something to type into the TUI."""
    with pytest.raises(RuntimeError, match="non-empty content"):
        inject_user_message_via_tui(tmp_path / "bridge", content="")
