"""Instructions tests for Codex app server."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from omnigent.harnesses.codex_native import app_server, launch_args
from omnigent.harnesses.codex_native.app_server import (
    _materialize_codex_profile_for_start,
    _sync_codex_developer_instructions,
)
from omnigent.inner.codex_executor import (
    _populate_codex_home_config,
)


def test_sync_developer_instructions_preserves_and_restores_user_config(tmp_path: Path) -> None:
    """Framework instructions append without replacing the user's Codex guidance."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        'model = "gpt-5.5"\ndeveloper_instructions = "Keep user guidance."\n',
        encoding="utf-8",
    )

    _sync_codex_developer_instructions(codex_home, "Rename the session.")
    _sync_codex_developer_instructions(codex_home, "Rename the session.")

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["model"] == "gpt-5.5"
    assert config["developer_instructions"] == ("Keep user guidance.\n\nRename the session.")

    _sync_codex_developer_instructions(codex_home, None)

    resumed_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert resumed_config["developer_instructions"] == "Keep user guidance."


def test_sync_developer_instructions_survives_reseeded_config(tmp_path: Path) -> None:
    """A persisted sidecar restores the original base after config reseeding."""
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        'developer_instructions = "Keep original guidance."\n',
        encoding="utf-8",
    )
    (source_home / "config.toml").write_text(
        'developer_instructions = "New shared guidance."\n',
        encoding="utf-8",
    )

    _sync_codex_developer_instructions(codex_home, "Rename the session.")
    config_path.unlink()
    _populate_codex_home_config(codex_home, source_home)

    reseeded = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert reseeded["developer_instructions"] == "New shared guidance."

    _sync_codex_developer_instructions(codex_home, None)

    resumed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert resumed["developer_instructions"] == "Keep original guidance."


def test_sync_developer_instructions_recovers_legacy_augmented_config(tmp_path: Path) -> None:
    """A missing sidecar does not capture an existing framework suffix as user base."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        'developer_instructions = "Keep user guidance.\\n\\nRename the session."\n',
        encoding="utf-8",
    )

    _sync_codex_developer_instructions(codex_home, "Rename the session.")

    active = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert active["developer_instructions"] == "Keep user guidance.\n\nRename the session."

    _sync_codex_developer_instructions(codex_home, None)

    resumed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert resumed["developer_instructions"] == "Keep user guidance."


def test_sync_developer_instructions_skips_invalid_config(tmp_path: Path) -> None:
    """Optional title metadata never blocks Codex startup on malformed config."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text("invalid = [", encoding="utf-8")

    _sync_codex_developer_instructions(codex_home, "Rename the session.")

    assert config_path.read_text(encoding="utf-8") == "invalid = ["


@pytest.mark.parametrize(
    ("profiles", "expected"),
    [
        (("first", None), ("FIRST\n\nAGENT", "USER\n\nAGENT")),
        ((None, "first"), ("USER\n\nAGENT", "FIRST\n\nAGENT")),
        (("first", "second"), ("FIRST\n\nAGENT", "SECOND\n\nAGENT")),
    ],
)
def test_profile_and_agent_instruction_layers_follow_startup_sequence(
    tmp_path: Path,
    profiles: tuple[str | None, str | None],
    expected: tuple[str, str],
) -> None:
    """Selecting, switching, or removing a profile recomposes instructions."""
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    (codex_home / "config.toml").write_text('developer_instructions = "USER"\n', encoding="utf-8")
    (source_home / "first.config.toml").write_text(
        'developer_instructions = "FIRST"\n', encoding="utf-8"
    )
    (source_home / "second.config.toml").write_text(
        'developer_instructions = "SECOND"\n', encoding="utf-8"
    )

    for profile, expected_instructions in zip(profiles, expected, strict=True):
        compose_profile_instructions = _materialize_codex_profile_for_start(
            codex_home,
            source_home,
            profile,
            codex_version=(0, 155, 0),
            agent_instructions="AGENT",
        )
        _sync_codex_developer_instructions(
            codex_home,
            "AGENT",
            use_current_base=compose_profile_instructions,
        )
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        assert config["developer_instructions"] == expected_instructions


def test_profile_instructions_equal_to_agent_instructions_remain_a_separate_layer(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    (codex_home / "config.toml").write_text('developer_instructions = "USER"\n')
    (source_home / "same.config.toml").write_text('developer_instructions = "AGENT"\n')

    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        "same",
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    _sync_codex_developer_instructions(
        codex_home, "AGENT", use_current_base=compose_profile_instructions
    )

    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert config["developer_instructions"] == "AGENT\n\nAGENT"


def test_pending_profile_update_recovers_before_instruction_base_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    (codex_home / "config.toml").write_text('developer_instructions = "USER"\n')
    (source_home / "first.config.toml").write_text('developer_instructions = "FIRST"\n')
    (source_home / "second.config.toml").write_text('developer_instructions = "SECOND"\n')

    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        "first",
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    _sync_codex_developer_instructions(
        codex_home, "AGENT", use_current_base=compose_profile_instructions
    )
    replace = launch_args.os.replace
    replacements = 0

    def fail_final_state_replace(source: str, destination: Path) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == 3:
            raise OSError("injected final-state failure")
        replace(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(launch_args.os, "replace", fail_final_state_replace)
        with pytest.raises(OSError, match="injected final-state failure"):
            _materialize_codex_profile_for_start(
                codex_home,
                source_home,
                "second",
                codex_version=(0, 155, 0),
                agent_instructions="AGENT",
            )

    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        "second",
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    _sync_codex_developer_instructions(
        codex_home, "AGENT", use_current_base=compose_profile_instructions
    )
    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert config["developer_instructions"] == "SECOND\n\nAGENT"


@pytest.mark.parametrize("failed_replace", [1, 2, 3])
def test_profile_instruction_update_recovers_after_interrupted_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_replace: int,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "USER"\n')
    (source_home / "first.config.toml").write_text('developer_instructions = "FIRST"\n')
    (source_home / "second.config.toml").write_text('developer_instructions = "SECOND"\n')

    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        "first",
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    _sync_codex_developer_instructions(
        codex_home, "AGENT", use_current_base=compose_profile_instructions
    )
    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        "second",
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    write_private_config = app_server._write_private_config
    replacements = 0

    def fail_instruction_write(path: Path, content: str) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == failed_replace:
            raise OSError("injected instruction-state failure")
        write_private_config(path, content)

    with monkeypatch.context() as fault:
        fault.setattr(app_server, "_write_private_config", fail_instruction_write)
        with pytest.raises(OSError, match="injected instruction-state failure"):
            _sync_codex_developer_instructions(
                codex_home, "AGENT", use_current_base=compose_profile_instructions
            )

    for profile, expected in (
        ("second", "SECOND\n\nAGENT"),
        (None, "USER\n\nAGENT"),
    ):
        compose_profile_instructions = _materialize_codex_profile_for_start(
            codex_home,
            source_home,
            profile,
            codex_version=(0, 155, 0),
            agent_instructions="AGENT",
        )
        _sync_codex_developer_instructions(
            codex_home, "AGENT", use_current_base=compose_profile_instructions
        )
        config = tomllib.loads(config_path.read_text())
        assert config["developer_instructions"] == expected


def test_private_instruction_edit_survives_profile_reapplication_and_removal(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "USER"\n')
    (source_home / "first.config.toml").write_text('developer_instructions = "FIRST"\n')

    for profile in ("first", "first", None):
        compose_profile_instructions = _materialize_codex_profile_for_start(
            codex_home,
            source_home,
            profile,
            codex_version=(0, 155, 0),
            agent_instructions="AGENT",
        )
        _sync_codex_developer_instructions(
            codex_home, "AGENT", use_current_base=compose_profile_instructions
        )
        if profile == "first" and not (codex_home / "private-edit-recorded").exists():
            config = tomlkit.parse(config_path.read_text())
            config["developer_instructions"] = "PRIVATE"
            config_path.write_text(tomlkit.dumps(config))
            (codex_home / "private-edit-recorded").touch()

    config = tomllib.loads(config_path.read_text())
    assert config["developer_instructions"] == "PRIVATE\n\nAGENT"


def test_appended_private_instruction_edit_survives_profile_removal(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "USER"\n')
    (source_home / "first.config.toml").write_text('developer_instructions = "FIRST"\n')

    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        "first",
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    _sync_codex_developer_instructions(
        codex_home, "AGENT", use_current_base=compose_profile_instructions
    )
    config = tomlkit.parse(config_path.read_text())
    config["developer_instructions"] = "FIRST\n\nAGENT\n\nPRIVATE"
    config_path.write_text(tomlkit.dumps(config))

    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        None,
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    _sync_codex_developer_instructions(
        codex_home, "AGENT", use_current_base=compose_profile_instructions
    )

    config = tomllib.loads(config_path.read_text())
    assert config["developer_instructions"] == "USER\n\nPRIVATE\n\nAGENT"


def test_legacy_instruction_sidecar_preserves_appended_edit_on_first_profile_selection(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "USER\\n\\nAGENT\\n\\nPRIVATE"\n')
    (codex_home / ".omnigent-developer-instructions-base").write_text("USER")
    (source_home / "first.config.toml").write_text('developer_instructions = "FIRST"\n')

    for profile in ("first", None):
        compose_profile_instructions = _materialize_codex_profile_for_start(
            codex_home,
            source_home,
            profile,
            codex_version=(0, 155, 0),
            agent_instructions="AGENT",
        )
        _sync_codex_developer_instructions(
            codex_home, "AGENT", use_current_base=compose_profile_instructions
        )

    config = tomllib.loads(config_path.read_text())
    assert config["developer_instructions"] == "USER\n\nPRIVATE\n\nAGENT"


@pytest.mark.parametrize(
    ("profile", "old_base", "old_applied", "current", "expected"),
    [
        (
            None,
            "FIRST\n\nAGENT",
            "FIRST\n\nAGENT",
            "FIRST\n\nAGENT",
            "USER\n\nAGENT",
        ),
        (
            "second",
            "FIRST\n\nAGENT",
            "SECOND",
            "FIRST\n\nAGENT",
            "SECOND\n\nAGENT",
        ),
        (
            None,
            "USER",
            "FIRST",
            "FIRST\n\nAGENT\n\nPRIVATE",
            "USER\n\nPRIVATE\n\nAGENT",
        ),
        (
            None,
            "FIRST\n\nAGENT",
            "FIRST",
            "FIRST\n\nAGENT\n\nPRIVATE",
            "USER\n\nPRIVATE\n\nAGENT",
        ),
    ],
)
def test_migrates_instruction_state_corrupted_by_older_profile_startup(
    tmp_path: Path,
    profile: str | None,
    old_base: str,
    old_applied: str,
    current: str,
    expected: str,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    (source_home / "config.toml").write_text('developer_instructions = "USER"\n')
    (source_home / "second.config.toml").write_text('developer_instructions = "SECOND"\n')
    (codex_home / "config.toml").write_text(tomlkit.dumps({"developer_instructions": current}))
    (codex_home / ".omnigent-developer-instructions-base").write_text("FIRST")
    (codex_home / ".omnigent-config-profile.toml").write_text(
        tomlkit.dumps(
            {
                "base": {"developer_instructions": old_base},
                "applied": {"developer_instructions": old_applied},
            }
        )
    )

    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        profile,
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    _sync_codex_developer_instructions(
        codex_home, "AGENT", use_current_base=compose_profile_instructions
    )

    config = tomllib.loads((codex_home / "config.toml").read_text())
    assert config["developer_instructions"] == expected
    profile_state = tomllib.loads((codex_home / ".omnigent-config-profile.toml").read_text())
    assert "developer_instructions" not in profile_state["base"]
    assert "developer_instructions" not in profile_state["applied"]


@pytest.mark.parametrize("failed_write", [1, 2, 3])
def test_instruction_migration_recovers_after_interrupted_journal_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_write: int,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "FIRST\\n\\nAGENT"\n')
    (source_home / "config.toml").write_text('developer_instructions = "USER"\n')
    (codex_home / ".omnigent-developer-instructions-base").write_text("FIRST")
    (codex_home / ".omnigent-config-profile.toml").write_text(
        tomlkit.dumps(
            {
                "base": {"developer_instructions": "FIRST\n\nAGENT"},
                "applied": {"developer_instructions": "FIRST\n\nAGENT"},
            }
        )
    )
    write_private_config = app_server._write_private_config
    writes = 0

    def fail_migration_write(path: Path, content: str) -> None:
        nonlocal writes
        writes += 1
        if writes == failed_write:
            raise OSError("injected migration failure")
        write_private_config(path, content)

    with monkeypatch.context() as fault:
        fault.setattr(app_server, "_write_private_config", fail_migration_write)
        with pytest.raises(OSError, match="injected migration failure"):
            _materialize_codex_profile_for_start(
                codex_home,
                source_home,
                None,
                codex_version=(0, 155, 0),
                agent_instructions="AGENT",
            )

    compose_profile_instructions = _materialize_codex_profile_for_start(
        codex_home,
        source_home,
        None,
        codex_version=(0, 155, 0),
        agent_instructions="AGENT",
    )
    _sync_codex_developer_instructions(
        codex_home, "AGENT", use_current_base=compose_profile_instructions
    )
    config = tomllib.loads(config_path.read_text())
    assert config["developer_instructions"] == "USER\n\nAGENT"


def test_invalid_pending_profile_state_does_not_run_instruction_migration(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "unexpected"\ndeveloper_instructions = "CURRENT"\n')
    base_path = codex_home / ".omnigent-developer-instructions-base"
    base_path.write_text("SAVED")
    state_path = codex_home / ".omnigent-config-profile.toml"
    state_path.write_text(
        tomlkit.dumps(
            {
                "base": {"model": "base", "developer_instructions": "OLD"},
                "applied": {"model": "applied", "developer_instructions": "OLD"},
                "pending": {"model": "pending", "developer_instructions": "NEW"},
            }
        )
    )
    original_config = config_path.read_text()
    original_state = state_path.read_text()

    with pytest.raises(ValueError, match="incomplete profile update"):
        _materialize_codex_profile_for_start(
            codex_home, source_home, None, codex_version=(0, 155, 0)
        )

    assert config_path.read_text() == original_config
    assert state_path.read_text() == original_state
    assert base_path.read_text() == "SAVED"


def test_migration_with_invalid_source_config_fails_without_mutation(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "FIRST\\n\\nAGENT"\n')
    source_path = source_home / "config.toml"
    source_path.write_text("invalid = [")
    base_path = codex_home / ".omnigent-developer-instructions-base"
    base_path.write_text("FIRST")
    state_path = codex_home / ".omnigent-config-profile.toml"
    state_path.write_text(
        tomlkit.dumps(
            {
                "base": {"developer_instructions": "FIRST\n\nAGENT"},
                "applied": {"developer_instructions": "FIRST\n\nAGENT"},
            }
        )
    )
    original_config = config_path.read_text()
    original_state = state_path.read_text()

    with pytest.raises(ValueError, match="Cannot migrate Codex profile instructions"):
        _materialize_codex_profile_for_start(
            codex_home, source_home, None, codex_version=(0, 155, 0)
        )

    assert config_path.read_text() == original_config
    assert state_path.read_text() == original_state
    assert base_path.read_text() == "FIRST"


@pytest.mark.parametrize("profile_content", [None, "invalid = ["])
def test_invalid_selected_profile_fails_before_instruction_restore(
    tmp_path: Path,
    profile_content: str | None,
) -> None:
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "USER"\n')
    _sync_codex_developer_instructions(codex_home, "AGENT")
    if profile_content is not None:
        (source_home / "missing.config.toml").write_text(profile_content)
    instruction_state = codex_home / ".omnigent-developer-instructions-state.toml"
    original_config = config_path.read_text()
    original_state = instruction_state.read_text()

    with pytest.raises((FileNotFoundError, tomlkit.exceptions.TOMLKitError)):
        _materialize_codex_profile_for_start(
            codex_home,
            source_home,
            "missing",
            codex_version=(0, 155, 0),
            agent_instructions="AGENT",
        )

    assert config_path.read_text() == original_config
    assert instruction_state.read_text() == original_state


def test_routed_spawn_note_appends_then_restores_the_user_base(tmp_path: Path) -> None:
    """The codex routed-spawn note rides ``developer_instructions``, reversibly.

    It must be additive to the user's own guidance on a fresh auto-harness
    launch and gone again on a resumed / pinned launch, which is what keeps a
    session that leaves auto-harness mode from carrying stale routing framing.
    """
    from omnigent.inner.hook_scripts.subagent_router import smart_routing_spawn_note

    note = smart_routing_spawn_note("codex-native")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "Keep user guidance."\n', encoding="utf-8")

    _sync_codex_developer_instructions(codex_home, note)

    active = tomllib.loads(config_path.read_text(encoding="utf-8"))["developer_instructions"]
    assert active == f"Keep user guidance.\n\n{note}"
    assert "sys_session_create" in active
    # Codex takes bare MCP names plus a namespace, never a prefixed spelling.
    assert "mcp__omnigent__" not in active

    _sync_codex_developer_instructions(codex_home, None)

    resumed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert resumed["developer_instructions"] == "Keep user guidance."
