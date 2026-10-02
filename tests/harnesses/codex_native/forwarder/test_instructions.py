"""Instructions tests for Codex forwarder."""

from __future__ import annotations

from pathlib import Path

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    codex_home_for_bridge_dir,
)
from tests.harnesses.codex_native.forwarder._support import (
    _write_session_config,
)


def test_default_collaboration_mode_refuses_when_developer_instructions_never_confirmed() -> None:
    """No confirmed developer_instructions read yet → refuse to build a
    payload at all, rather than risk sending an unconfirmed ``null``.

    Regression for the destructive-wipe class: if every config.toml read so
    far has been UNREADABLE, ``developer_instructions`` stays at its ``None``
    default but ``developer_instructions_known`` stays ``False`` — this must
    not be conflated with a confirmed ABSENT read (see the sibling test
    above), or a Default-mode turn would serialize a literal ``null`` over a
    value that genuinely exists but just hasn't been read successfully yet.
    """
    state = fwd._CodexForwarderState()
    state.model = "gpt-5.4"

    mode = fwd._default_collaboration_mode(state)

    assert mode is None


def test_default_collaboration_mode_sends_none_when_confirmed_absent() -> None:
    """A CONFIRMED absent read → explicit ``None``, letting Codex fill in its
    own built-in Default-mode instructions.

    Distinct from the never-confirmed case below: here
    ``developer_instructions_known`` is ``True`` (a real ABSENT read
    happened), so the explicit ``null`` is deliberate, not a guess.
    """
    state = fwd._CodexForwarderState()
    state.model = "gpt-5.4"
    state.developer_instructions_known = True

    mode = fwd._default_collaboration_mode(state)

    assert mode is not None
    assert mode["settings"]["developer_instructions"] is None


def test_default_collaboration_mode_reuses_current_developer_instructions() -> None:
    """A Default-mode ``turn/start`` must not silently wipe developer_instructions.

    Guards a self-inflicted overwrite: a ``_default_collaboration_mode`` that
    always sends ``developer_instructions: null`` has that null applied
    literally by Codex's app-server, clearing whatever
    ``build_codex_native_server`` persisted, the instant the user (or the
    plan-implementation flow) triggers a Default-mode turn.
    """
    state = fwd._CodexForwarderState()
    state.model = "gpt-5.4"
    state.developer_instructions = "Be a concise coding assistant."
    state.developer_instructions_known = True

    mode = fwd._default_collaboration_mode(state)

    assert mode is not None
    assert mode["settings"]["developer_instructions"] == "Be a concise coding assistant."


def test_note_thread_settings_updated_whitespace_nested_value_not_confirmed() -> None:
    """
    The real live ``thread/settings/updated`` fixture shape (nested under
    ``threadSettings.collaborationMode.settings``) with a whitespace-only
    ``developer_instructions`` must not be stored or marked confirmed.
    """
    state = fwd._CodexForwarderState()

    state.note_thread_settings_updated(
        {
            "threadSettings": {
                "model": "gpt-5.4-codex",
                "effort": "medium",
                "collaborationMode": {
                    "mode": "plan",
                    "settings": {
                        "model": "gpt-5.4-codex",
                        "reasoning_effort": "medium",
                        "developer_instructions": "   ",
                    },
                },
            }
        }
    )

    assert state.developer_instructions is None
    assert state.developer_instructions_known is False


def test_note_developer_instructions_fields_whitespace_flat_falls_through_to_nested() -> None:
    """
    A whitespace-only FLAT ``developer_instructions`` must not
    short-circuit the nested-shape check — it carries no real content, so
    a genuine nested value (the real live ``thread/settings/updated``
    shape) must still be found and used.
    """
    state = fwd._CodexForwarderState()

    state._note_developer_instructions_fields(
        {
            "developer_instructions": "   ",
            "collaborationMode": {
                "settings": {"developer_instructions": "Be a concise assistant."},
            },
        }
    )

    assert state.developer_instructions == "Be a concise assistant."
    assert state.developer_instructions_known is True


def test_note_developer_instructions_fields_whitespace_flat_value_not_confirmed() -> None:
    """
    A whitespace-only FLAT ``developer_instructions`` in a live payload
    must not be stored or marked confirmed — the same malformed-shape
    class the config.toml tri-state reader treats as UNREADABLE, not
    PRESENT. Bare truthiness is ``True`` for whitespace, which would wrongly
    store it and mark ``developer_instructions_known``. The config.toml
    reader guards against this; this live-notification path is a parallel
    code path that has to guard against it independently.
    """
    state = fwd._CodexForwarderState()

    state._note_developer_instructions_fields({"developer_instructions": "   \n\t "})

    assert state.developer_instructions is None
    assert state.developer_instructions_known is False


def test_note_thread_settings_updated_reads_nested_developer_instructions() -> None:
    """The real live ``thread/settings/updated`` shape nests
    ``developer_instructions`` under ``threadSettings.collaborationMode.settings``,
    not as a top-level ``threadSettings`` key (mirrors the fixture in
    ``test_thread_settings_updated_records_effort_and_collaboration_mode``).
    Regression: a flat-only lookup never updates state.developer_instructions
    on the real live notification path.
    """
    state = fwd._CodexForwarderState()

    state.note_thread_settings_updated(
        {
            "threadSettings": {
                "model": "gpt-5.4-codex",
                "effort": "medium",
                "collaborationMode": {
                    "mode": "plan",
                    "settings": {
                        "model": "gpt-5.4-codex",
                        "reasoning_effort": "medium",
                        "developer_instructions": "Be a concise assistant.",
                    },
                },
            }
        }
    )

    assert state.developer_instructions == "Be a concise assistant."


def test_note_developer_instructions_fields_updates_from_settings_payload() -> None:
    """A flat top-level settings payload updates developer_instructions too."""
    state = fwd._CodexForwarderState()

    state._note_developer_instructions_fields({"developer_instructions": "New instructions."})

    assert state.developer_instructions == "New instructions."
    # A live notification observing a real value is itself a confirmed
    # read, resolving the never-yet-confirmed ambiguity independent of
    # any config.toml read.
    assert state.developer_instructions_known is True


def test_read_developer_instructions_collapsed_wrapper_matches_tri_state_value(
    tmp_path: Path,
) -> None:
    """The Optional[str]-returning wrapper collapses PRESENT/ABSENT/UNREADABLE
    to their .value.

    No production caller currently uses this collapsed form — both the
    forwarder and the runner's plan-mode settings send consume
    read_codex_config_developer_instructions_state[_from_home] directly and
    handle all three states explicitly, because a collapsed None genuinely
    lost information they each needed (the forwarder must actively CLEAR on
    a confirmed ABSENT read, not just never-overwrite-on-falsy; the runner
    must refuse/503 on UNREADABLE rather than guess). The subject here is the
    wrapper's own remaining collapsing behavior in isolation."""
    from omnigent.harnesses.codex_native.bridge import (
        read_codex_config_developer_instructions_from_home,
    )

    assert read_codex_config_developer_instructions_from_home(tmp_path) is None
    (tmp_path / "config.toml").write_text('developer_instructions = "Present value."\n')
    assert read_codex_config_developer_instructions_from_home(tmp_path) == "Present value."


def test_read_developer_instructions_state_unreadable_bad_encoding(tmp_path: Path) -> None:
    """Non-UTF-8 bytes read UNREADABLE, not ABSENT — the failure this whole
    tri-state exists to distinguish from genuine absence."""
    from omnigent.harnesses.codex_native.bridge import (
        DeveloperInstructionsReadState,
        read_codex_config_developer_instructions_state_from_home,
    )

    (tmp_path / "config.toml").write_bytes(b"\xff\xfe not valid utf-8")
    result = read_codex_config_developer_instructions_state_from_home(tmp_path)

    assert result.state is DeveloperInstructionsReadState.UNREADABLE
    assert result.value is None


def test_read_developer_instructions_state_absent_missing_file(tmp_path: Path) -> None:
    """A missing config.toml reads ABSENT, not UNREADABLE.

    The tri-state separates "a value may be there and cannot be seen" from
    "there is demonstrably nothing there". A missing file is the second:
    ``_sync_codex_developer_instructions`` writes the key into this file or
    nowhere at all, so with the file gone nothing is persisted and a caller
    that sends no instructions overwrites nothing.

    Reading it as UNREADABLE refused every plan-mode toggle on a bridge whose
    config had not been written yet.
    """
    from omnigent.harnesses.codex_native.bridge import (
        DeveloperInstructionsReadState,
        read_codex_config_developer_instructions_state_from_home,
    )

    result = read_codex_config_developer_instructions_state_from_home(tmp_path)

    assert result.state is DeveloperInstructionsReadState.ABSENT
    assert result.value is None


def test_read_developer_instructions_state_whitespace_only_is_unreadable(
    tmp_path: Path,
) -> None:
    """A whitespace-only ``developer_instructions`` also reads UNREADABLE.

    Bare truthiness (``instructions`` alone) is ``True`` for ``"   "``, which
    would misclassify this PRESENT — the same whitespace-is-not-content
    hazard ``AgentSpec.instructions`` has to handle. The PRESENT check must
    use ``.strip()``, not truthiness.
    """
    from omnigent.harnesses.codex_native.bridge import (
        DeveloperInstructionsReadState,
        read_codex_config_developer_instructions_state_from_home,
    )

    (tmp_path / "config.toml").write_text('developer_instructions = "   \\n\\t "\n')
    result = read_codex_config_developer_instructions_state_from_home(tmp_path)

    assert result.state is DeveloperInstructionsReadState.UNREADABLE
    assert result.value is None


def test_read_developer_instructions_state_empty_string_is_unreadable(
    tmp_path: Path,
) -> None:
    """An empty-string ``developer_instructions`` also reads UNREADABLE —
    the writer never writes this shape either, so it's malformed too."""
    from omnigent.harnesses.codex_native.bridge import (
        DeveloperInstructionsReadState,
        read_codex_config_developer_instructions_state_from_home,
    )

    (tmp_path / "config.toml").write_text('developer_instructions = ""\n')
    result = read_codex_config_developer_instructions_state_from_home(tmp_path)

    assert result.state is DeveloperInstructionsReadState.UNREADABLE
    assert result.value is None


def test_read_developer_instructions_state_malformed_shape_is_unreadable(
    tmp_path: Path,
) -> None:
    """A present-but-non-string ``developer_instructions`` reads UNREADABLE,
    not ABSENT.

    ``_sync_codex_developer_instructions`` (the writer, in
    ``codex_native_app_server.py``) only ever writes a non-empty string or
    deletes the key outright — it never writes an empty string or a
    non-string value. A present-but-malformed shape can only mean external
    corruption, not a genuine "no instructions configured" state; treating
    it as ABSENT would let a plan-mode settings send serialize
    developer_instructions: null over a value that might still be real.
    """
    from omnigent.harnesses.codex_native.bridge import (
        DeveloperInstructionsReadState,
        read_codex_config_developer_instructions_state_from_home,
    )

    (tmp_path / "config.toml").write_text("developer_instructions = 12345\n")
    result = read_codex_config_developer_instructions_state_from_home(tmp_path)

    assert result.state is DeveloperInstructionsReadState.UNREADABLE
    assert result.value is None


def test_read_developer_instructions_state_absent(tmp_path: Path) -> None:
    """A config with no top-level key reads ABSENT, distinct from unreadable."""
    from omnigent.harnesses.codex_native.bridge import (
        DeveloperInstructionsReadState,
        read_codex_config_developer_instructions_state_from_home,
    )

    (tmp_path / "config.toml").write_text('[mcp_servers.fast]\ncommand = "x"\n')
    result = read_codex_config_developer_instructions_state_from_home(tmp_path)

    assert result.state is DeveloperInstructionsReadState.ABSENT
    assert result.value is None


def test_read_developer_instructions_state_present(tmp_path: Path) -> None:
    """A config with the key set reads PRESENT with the value."""
    from omnigent.harnesses.codex_native.bridge import (
        DeveloperInstructionsRead,
        DeveloperInstructionsReadState,
        read_codex_config_developer_instructions_state_from_home,
    )

    (tmp_path / "config.toml").write_text(
        'developer_instructions = "Be a concise coding assistant."\n'
    )
    result = read_codex_config_developer_instructions_state_from_home(tmp_path)

    assert result == DeveloperInstructionsRead(
        DeveloperInstructionsReadState.PRESENT, "Be a concise coding assistant."
    )


def test_refresh_developer_instructions_from_config_present_to_absent_transition(
    tmp_path: Path,
) -> None:
    """A live PRESENT→ABSENT transition across two refreshes actually clears.

    The forwarder must not just handle a single read correctly, but must
    correctly transition when the underlying config changes between calls.
    """
    _write_session_config(tmp_path, 'developer_instructions = "Be a concise coding assistant."\n')
    state = fwd._CodexForwarderState()
    fwd._refresh_developer_instructions_from_config(tmp_path, state)
    assert state.developer_instructions == "Be a concise coding assistant."

    _write_session_config(tmp_path, '[mcp_servers.fast]\ncommand = "x"\n')
    fwd._refresh_developer_instructions_from_config(tmp_path, state)
    assert state.developer_instructions is None


def test_refresh_developer_instructions_from_config_preserves_on_unreadable(
    tmp_path: Path,
) -> None:
    """A transient/unreadable config read preserves the prior known value.

    Distinct from genuine absence above: UNREADABLE means the read itself
    failed (bad encoding here), not that the key is confirmed gone — a
    transient glitch must never regress an already-known value to unknown.

    The bad bytes go where the reader looks — the private ``CODEX_HOME``
    under the bridge dir, as ``_write_session_config`` places a good config.
    Written at the bridge dir itself the reader finds no file at all, which
    is ABSENT, and the test would be asserting the absent path under an
    unreadable name.
    """
    home = codex_home_for_bridge_dir(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_bytes(b"\xff\xfe not valid utf-8")
    state = fwd._CodexForwarderState()
    state.developer_instructions = "Prior known instructions."

    fwd._refresh_developer_instructions_from_config(tmp_path, state)

    assert state.developer_instructions == "Prior known instructions."


def test_refresh_developer_instructions_from_config_clears_on_genuine_absence(
    tmp_path: Path,
) -> None:
    """A config with no top-level key CLEARS a previously known value.

    Genuine absence (the top-level key is genuinely gone — a real, distinct
    tri-state result, not a collapsed truthy check) is real, actionable
    information — e.g. the user's Omnigent-appended directive was removed —
    and must be reflected, not preserved as stale. This is the corrected
    expectation: a naive "no-op unless truthy" implementation cannot tell
    genuine absence apart from a transient read failure and would keep
    re-sending a stale value forever after a real removal.
    """
    _write_session_config(tmp_path, '[mcp_servers.fast]\ncommand = "x"\n')
    state = fwd._CodexForwarderState()
    state.developer_instructions = "Prior known instructions."

    fwd._refresh_developer_instructions_from_config(tmp_path, state)

    assert state.developer_instructions is None


def test_refresh_developer_instructions_from_config_reads_current_value(
    tmp_path: Path,
) -> None:
    """The forwarder's known developer_instructions comes from config.toml."""
    _write_session_config(
        tmp_path,
        'developer_instructions = "Be a concise coding assistant."\n',
    )
    state = fwd._CodexForwarderState()

    fwd._refresh_developer_instructions_from_config(tmp_path, state)

    assert state.developer_instructions == "Be a concise coding assistant."
