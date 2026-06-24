"""Tests for omnigent.spec.parser."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import discover_host_skills, parse
from omnigent.spec.types import ApiKeyAuth, DatabricksAuth, ProviderAuth


@pytest.fixture()
def agent_dir(tmp_path: Path) -> Path:
    """Create a minimal valid agent image directory."""
    config = {"spec_version": 1, "name": "test-agent"}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


def test_parse_minimal(agent_dir: Path) -> None:
    spec = parse(agent_dir)
    assert spec.spec_version == 1
    assert spec.name == "test-agent"
    assert spec.description is None
    assert spec.llm is None
    assert spec.interaction.conversational is True
    assert spec.interaction.modalities.input == ["text"]
    assert spec.interaction.modalities.output == ["text"]
    assert spec.tools.agents == []
    assert spec.params == {}
    assert spec.instructions is None
    assert spec.skills == []
    assert spec.mcp_servers == []
    assert spec.local_tools == []
    assert spec.sub_agents == []


def test_parse_missing_config_yaml(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"config.yaml not found"):
        parse(tmp_path)


def test_parse_non_mapping_config(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("- just a list")
    with pytest.raises(OmnigentError, match=r"must be a YAML mapping"):
        parse(tmp_path)


def test_parse_missing_spec_version(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(yaml.dump({"name": "no-version"}))
    with pytest.raises(OmnigentError, match=r"missing required field: spec_version"):
        parse(tmp_path)


def test_parse_full_config(tmp_path: Path) -> None:
    config = {
        "spec_version": 1,
        "name": "full-agent",
        "description": "A fully configured agent.",
        "llm": {
            "model": "openai/gpt-5.4",
            "max_completion_tokens": 4096,
            "reasoning_effort": "medium",
        },
        "interaction": {
            "conversational": True,
            "modalities": {
                "input": ["text", "image", "file"],
                "output": ["text"],
            },
        },
        "tools": {"agents": ["researcher", "critic"]},
        "params": {"max_results": 10, "prefer_recent": True},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))

    spec = parse(tmp_path)
    assert spec.name == "full-agent"
    assert spec.description == "A fully configured agent."
    assert spec.llm is not None
    assert spec.llm.model == "openai/gpt-5.4"
    # executor.model is the canonical source — verify consolidation
    assert spec.executor.model == "openai/gpt-5.4"
    assert spec.llm.extra == {
        "max_completion_tokens": 4096,
        "reasoning_effort": "medium",
    }
    assert spec.interaction.conversational is True
    assert spec.interaction.modalities.input == ["text", "image", "file"]
    assert spec.interaction.modalities.output == ["text"]
    assert spec.tools.agents == ["researcher", "critic"]
    assert spec.params == {"max_results": 10, "prefer_recent": True}


def test_parse_llm_missing_model(tmp_path: Path) -> None:
    config = {"spec_version": 1, "llm": {"max_completion_tokens": 100}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"missing required field: model"):
        parse(tmp_path)


def test_parse_llm_arbitrary_extra_keys(tmp_path: Path) -> None:
    """All non-model keys in the llm block are collected into extra."""
    config = {
        "spec_version": 1,
        "llm": {
            "model": "anthropic/claude-sonnet-4-20250514",
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 2048,
            "stop": ["\n\n"],
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.llm is not None
    assert spec.llm.model == "anthropic/claude-sonnet-4-20250514"
    assert spec.llm.extra == {
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 2048,
        "stop": ["\n\n"],
    }


def test_parse_llm_model_only(tmp_path: Path) -> None:
    """LLM block with only model has empty extra and no connection."""
    config = {"spec_version": 1, "llm": {"model": "openai/gpt-4o"}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.llm is not None
    assert spec.llm.model == "openai/gpt-4o"
    assert spec.llm.extra == {}
    assert spec.llm.connection is None


def test_parse_llm_connection_block(tmp_path: Path) -> None:
    """The connection sub-block is parsed into LLMConfig.connection."""
    config = {
        "spec_version": 1,
        "llm": {
            "model": "databricks/databricks-gpt-5-4",
            "temperature": 0.5,
            "connection": {
                "api_key": "dapi_test_key",
                "base_url": "https://my-workspace.databricks.com/serving-endpoints",
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.llm is not None
    assert spec.llm.model == "databricks/databricks-gpt-5-4"
    assert spec.llm.extra == {"temperature": 0.5}
    assert spec.llm.connection == {
        "api_key": "dapi_test_key",
        "base_url": "https://my-workspace.databricks.com/serving-endpoints",
    }


def test_parse_llm_connection_expands_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``${VAR}`` references in connection values are expanded."""
    monkeypatch.setenv("MY_API_KEY", "sk-secret-123")
    config = {
        "spec_version": 1,
        "llm": {
            "model": "openai/gpt-5.4",
            "connection": {"api_key": "${MY_API_KEY}"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.llm is not None
    assert spec.llm.connection == {"api_key": "sk-secret-123"}


def test_parse_llm_connection_unresolved_var_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Unresolved ``${VAR}`` in LLM connection raises ValueError.

    :param tmp_path: Temporary directory for config files.
    :param monkeypatch: Pytest monkeypatch for env vars.
    """
    monkeypatch.delenv("MY_API_KEY", raising=False)
    config = {
        "spec_version": 1,
        "llm": {
            "model": "openai/gpt-4o",
            "connection": {"api_key": "${MY_API_KEY}"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"Unresolved environment variable"):
        parse(tmp_path)


def test_parse_expand_env_false_keeps_var_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``expand_env=False`` keeps ``${VAR}`` references as literal strings.

    Used during scaffolding/validation (e.g. ``omnigent create``) where
    env vars may not yet be set in the current process.
    """
    monkeypatch.delenv("MY_API_KEY", raising=False)
    config = {
        "spec_version": 1,
        "llm": {
            "model": "openai/gpt-4o",
            "connection": {"api_key": "${MY_API_KEY}"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path, expand_env=False)
    assert spec.llm is not None
    assert spec.llm.connection == {"api_key": "${MY_API_KEY}"}


def test_parse_instructions_multiline_inline(tmp_path: Path) -> None:
    """Multiline inline instructions are not treated as file paths."""
    config = {
        "spec_version": 1,
        "instructions": "Line one.\nLine two.\nLine three.",
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.instructions == "Line one.\nLine two.\nLine three."


def test_parse_agents_md_fallback(agent_dir: Path) -> None:
    """No instructions key in config -> falls back to AGENTS.md."""
    (agent_dir / "AGENTS.md").write_text("You are a helpful research assistant.")
    spec = parse(agent_dir)
    assert spec.instructions == "You are a helpful research assistant."


def test_parse_instructions_inline(tmp_path: Path) -> None:
    """instructions key with inline text (not a file path)."""
    config = {"spec_version": 1, "instructions": "Be concise and helpful."}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.instructions == "Be concise and helpful."


def test_parse_instructions_file_reference(agent_dir: Path) -> None:
    """instructions key pointing to an existing file."""
    (agent_dir / "SYSTEM.md").write_text("Custom system prompt from file.")
    config = {"spec_version": 1, "name": "test-agent", "instructions": "SYSTEM.md"}
    (agent_dir / "config.yaml").write_text(yaml.dump(config))
    spec = parse(agent_dir)
    assert spec.instructions == "Custom system prompt from file."


def test_parse_instructions_rejects_path_traversal(tmp_path: Path) -> None:
    """An ``instructions`` value escaping the bundle is treated as literal text.

    A crafted/uploaded bundle could set ``instructions: ../secret.txt`` to make
    the runner read a file outside the bundle root and fold it into the agent's
    system prompt (W7 spec-injection). The parser must NOT read an out-of-root
    target — it falls back to treating the value as inline text, so the file's
    contents never enter the spec. If this regresses, ``spec.instructions``
    would contain the secret file's body.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET RUNNER FILE")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    config = {"spec_version": 1, "name": "evil", "instructions": "../secret.txt"}
    (bundle / "config.yaml").write_text(yaml.dump(config))

    spec = parse(bundle)

    # The out-of-root target is never read — its contents must not leak.
    assert "TOP SECRET" not in (spec.instructions or "")
    # Falls back to the literal value (the existing "missing file → inline" path).
    assert spec.instructions == "../secret.txt"


def test_parse_instructions_overrides_agents_md(agent_dir: Path) -> None:
    """Explicit instructions key takes precedence over AGENTS.md."""
    (agent_dir / "AGENTS.md").write_text("Fallback instructions.")
    config = {"spec_version": 1, "name": "test-agent", "instructions": "Inline wins."}
    (agent_dir / "config.yaml").write_text(yaml.dump(config))
    spec = parse(agent_dir)
    assert spec.instructions == "Inline wins."


def test_parse_instructions_file_overrides_agents_md(agent_dir: Path) -> None:
    """instructions pointing to a file takes precedence over AGENTS.md."""
    (agent_dir / "AGENTS.md").write_text("Fallback instructions.")
    (agent_dir / "CUSTOM.md").write_text("Custom file wins.")
    config = {"spec_version": 1, "name": "test-agent", "instructions": "CUSTOM.md"}
    (agent_dir / "config.yaml").write_text(yaml.dump(config))
    spec = parse(agent_dir)
    assert spec.instructions == "Custom file wins."


def test_parse_prompt_alias_inline(tmp_path: Path) -> None:
    """``prompt:`` is an alias for ``instructions:`` (inline text)."""
    config = {"spec_version": 1, "prompt": "Be concise and helpful."}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    # Without the alias, ``prompt:`` is ignored and instructions falls
    # back to None (no AGENTS.md here) — the silent generic-prompt bug.
    assert spec.instructions == "Be concise and helpful."


def test_parse_prompt_alias_multiline(tmp_path: Path) -> None:
    """A multiline ``prompt:`` block (the nessie config shape) loads."""
    config = {
        "spec_version": 1,
        "name": "nessie-like",
        "prompt": "You are an orchestrator.\nNever merge.\nDecompose first.",
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.instructions == ("You are an orchestrator.\nNever merge.\nDecompose first.")


def test_parse_prompt_alias_file_reference(agent_dir: Path) -> None:
    """``prompt:`` honors the same file-path resolution as instructions."""
    (agent_dir / "SYSTEM.md").write_text("Prompt body from file.")
    config = {"spec_version": 1, "name": "test-agent", "prompt": "SYSTEM.md"}
    (agent_dir / "config.yaml").write_text(yaml.dump(config))
    spec = parse(agent_dir)
    assert spec.instructions == "Prompt body from file."


def test_parse_prompt_alias_overrides_agents_md(agent_dir: Path) -> None:
    """``prompt:`` is consulted before the AGENTS.md auto-detect scan."""
    (agent_dir / "AGENTS.md").write_text("Fallback instructions.")
    config = {"spec_version": 1, "name": "test-agent", "prompt": "Prompt wins."}
    (agent_dir / "config.yaml").write_text(yaml.dump(config))
    spec = parse(agent_dir)
    assert spec.instructions == "Prompt wins."


def test_parse_instructions_wins_over_prompt(tmp_path: Path) -> None:
    """When both keys are set, ``instructions:`` takes precedence."""
    config = {
        "spec_version": 1,
        "instructions": "Canonical instructions.",
        "prompt": "Legacy prompt alias.",
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    # Precedence lock: ``instructions:`` is the canonical key and carries
    # file-path resolution; ``prompt:`` only fills in when it's absent.
    assert spec.instructions == "Canonical instructions."


def test_auto_detect_agents_md_first_priority(agent_dir: Path) -> None:
    """AGENTS.md is chosen over CLAUDE.md and .cursorrules."""
    (agent_dir / "AGENTS.md").write_text("FROM AGENTS")
    (agent_dir / "CLAUDE.md").write_text("FROM CLAUDE")
    (agent_dir / ".cursorrules").write_text("FROM CURSORRULES")
    spec = parse(agent_dir)
    assert spec.instructions == "FROM AGENTS"


def test_auto_detect_claude_md_when_no_agents_md(agent_dir: Path) -> None:
    """CLAUDE.md is chosen when AGENTS.md is absent."""
    (agent_dir / "CLAUDE.md").write_text("FROM CLAUDE")
    (agent_dir / ".cursorrules").write_text("FROM CURSORRULES")
    spec = parse(agent_dir)
    assert spec.instructions == "FROM CLAUDE"


def test_auto_detect_cursorrules_when_others_absent(agent_dir: Path) -> None:
    """.cursorrules is chosen when AGENTS.md and CLAUDE.md are absent."""
    (agent_dir / ".cursorrules").write_text("FROM CURSORRULES")
    spec = parse(agent_dir)
    assert spec.instructions == "FROM CURSORRULES"


def test_auto_detect_none_when_no_context_files(agent_dir: Path) -> None:
    """No context files present → instructions is None."""
    spec = parse(agent_dir)
    assert spec.instructions is None


def test_parse_skill(agent_dir: Path) -> None:
    skill_dir = agent_dir / "skills" / "deep-search"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: deep-search\n"
        "description: Search the web for sources.\n"
        "---\n"
        "When asked to research, use search.web."
    )
    spec = parse(agent_dir)
    assert len(spec.skills) == 1
    skill = spec.skills[0]
    assert skill.name == "deep-search"
    assert skill.description == "Search the web for sources."
    assert skill.content == "When asked to research, use search.web."
    assert skill.skill_dir == skill_dir
    # Absent ``user-invocable`` frontmatter defaults to invocable.
    assert skill.user_invocable is True


def test_parse_skill_user_invocable_false(agent_dir: Path) -> None:
    """``user-invocable: false`` frontmatter parses to ``user_invocable=False``."""
    skill_dir = agent_dir / "skills" / "internal-hook"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: internal-hook\n"
        "description: Internal orchestration skill.\n"
        "user-invocable: false\n"
        "---\n"
        "Body."
    )
    spec = parse(agent_dir)
    skill = next(s for s in spec.skills if s.name == "internal-hook")
    assert skill.user_invocable is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Quoted-string spellings (YAML keeps these as ``str``, not bool) —
        # the string branch of _falsey_flag, never exercised by the bare forms.
        ('"false"', False),
        ('"False"', False),
        ('"FALSE"', False),
        ('" false "', False),
        ('"no"', False),  # extended false spellings (quoted → str)
        ('"off"', False),
        ('"0"', False),
        ('"true"', True),
        ('"yes"', True),  # not in the false set
        ('"maybe"', True),
        # Genuine YAML booleans — PyYAML parses bare false/no/off to ``bool``.
        ("false", False),
        ("no", False),
        ("off", False),
        ("true", True),
    ],
)
def test_parse_skill_user_invocable_string_and_bool_spellings(
    agent_dir: Path, raw: str, expected: bool
) -> None:
    """Both the YAML bool ``false`` and the quoted string ``"false"`` parse falsey."""
    skill_dir = agent_dir / "skills" / "flag-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: flag-skill\ndescription: d\nuser-invocable: {raw}\n---\nBody."
    )
    spec = parse(agent_dir)
    skill = next(s for s in spec.skills if s.name == "flag-skill")
    assert skill.user_invocable is expected


def test_parse_skill_missing_frontmatter(agent_dir: Path) -> None:
    skill_dir = agent_dir / "skills" / "bad"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("No frontmatter here.")
    with pytest.raises(OmnigentError, match=r"missing YAML frontmatter"):
        parse(agent_dir)


def test_parse_skill_missing_name(agent_dir: Path) -> None:
    skill_dir = agent_dir / "skills" / "no-name"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\ndescription: Missing name.\n---\nContent.")
    with pytest.raises(OmnigentError, match=r"missing required field 'name'"):
        parse(agent_dir)


def test_parse_skill_missing_description(agent_dir: Path) -> None:
    skill_dir = agent_dir / "skills" / "no-desc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: no-desc\n---\nContent.")
    with pytest.raises(OmnigentError, match=r"missing required field 'description'"):
        parse(agent_dir)


def test_parse_skill_non_utf8_raises_omnigent_error(agent_dir: Path) -> None:
    """
    A non-UTF-8 SKILL.md must funnel through OmnigentError (not escape as a
    bare UnicodeDecodeError) so the lenient scanner / menu providers can
    catch it and skip the file instead of crashing.
    """
    skill_dir = agent_dir / "skills" / "bad-bytes"
    skill_dir.mkdir(parents=True)
    # 0xff is invalid UTF-8 — read_text() raises UnicodeDecodeError.
    (skill_dir / "SKILL.md").write_bytes(b"---\nname: bad-bytes\ndescription: \xff\n---\nx")
    with pytest.raises(OmnigentError, match=r"could not be read"):
        parse(agent_dir)


# Reproduces the exact ``argument-hint:`` line from the upstream
# Claude Code skill at
# https://github.com/databricks-field-eng/vibe/blob/main/plugins/fe-databricks-tools/skills/databricks-data-generation/SKILL.md
# which broke ``omnigent --harness codex`` REPL launch before the
# host-skill scanner was made tolerant. YAML reads ``[industry]``
# as a flow sequence and then chokes on the trailing ``[--rows N]``.
_UPSTREAM_BAD_ARGUMENT_HINT = (
    "argument-hint: [industry] [--rows N] [--catalog NAME] [--schema NAME]"
)


def test_parse_skill_invalid_yaml_frontmatter_in_bundle_raises(
    agent_dir: Path,
) -> None:
    """
    Agent-bundle skills are shipped with the spec and stay strict —
    a YAML parse error in the bundle's own ``skills/`` directory
    must fail loud, not silently drop the skill. ``parse()`` calls
    ``_discover_skills`` without the ``strict=False`` opt-in, so
    this test also pins the default behavior.
    """
    skill_dir = agent_dir / "skills" / "bad-yaml"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: bad-yaml\ndescription: x\n{_UPSTREAM_BAD_ARGUMENT_HINT}\n---\nContent."
    )
    with pytest.raises(OmnigentError, match=r"invalid YAML frontmatter"):
        parse(agent_dir)


def test_discover_host_skills_skips_invalid_yaml_frontmatter(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host skill directories are user-managed (``~/.claude/skills/``,
    ``.claude/skills/``) and may contain third-party skills whose
    frontmatter doesn't strictly parse as YAML. This test uses the
    literal upstream ``argument-hint:`` line from the
    ``databricks-data-generation`` Claude Code skill — the exact
    string that aborted ``omnigent --harness codex`` REPL launch
    in production.

    One bad skill must not break REPL launch: it must be logged
    (with the file path so the user knows what to fix and the YAML
    error so the cause is clear) and skipped, while the remaining
    skills continue to load.

    ``discover_host_skills`` scans two locations: walking up from
    ``agent_root`` and ``Path.home() / ".claude" / "skills"``. We
    pin ``$HOME`` at a fresh tmp dir to keep the developer's real
    ``~/.claude/skills/`` (which contains the actual offending
    skill) out of this test.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    host_skills = agent_root / ".claude" / "skills"
    host_skills.mkdir(parents=True)

    bad_dir = host_skills / "bad-skill"
    bad_dir.mkdir()
    bad_md = bad_dir / "SKILL.md"
    bad_md.write_text(
        f"---\nname: bad-skill\ndescription: x\n{_UPSTREAM_BAD_ARGUMENT_HINT}\n---\nContent."
    )

    good_dir = host_skills / "good-skill"
    good_dir.mkdir()
    (good_dir / "SKILL.md").write_text("---\nname: good-skill\ndescription: y\n---\nContent.")

    with caplog.at_level("WARNING", logger="omnigent.spec.parser"):
        result = discover_host_skills(agent_root, "all")

    names = [s.name for s in result]
    assert names == ["good-skill"], (
        "tolerant host-skill scan must drop the bad skill but keep "
        "every other skill in the same directory"
    )

    skip_records = [rec for rec in caplog.records if "Skipping skill" in rec.message]
    assert len(skip_records) == 1, "exactly one skip warning expected — one per bad skill"
    msg = skip_records[0].message
    # Warning must name the offending file so the user can fix it,
    # and must surface the YAML parser error so the cause is clear.
    assert str(bad_md) in msg, msg
    assert "invalid YAML frontmatter" in msg, msg


def test_discover_host_skills_skips_unreadable_skill_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    File IO errors (broken symlink, permission denied) on a host
    ``SKILL.md`` must funnel through the same tolerant path as
    malformed-frontmatter errors. A user with a stray broken
    symlink under ``~/.claude/skills/`` must not see the whole
    REPL launch abort.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    host_skills = agent_root / ".claude" / "skills"
    host_skills.mkdir(parents=True)

    # Broken symlink: ``SKILL.md`` exists (in the sense that
    # ``Path.exists()`` follows symlinks and returns False, but the
    # discoverer's ``skill_md.exists()`` check returns False too).
    # Use a directory we make read-then-unreadable instead so the
    # path exists but read_text() raises OSError.
    bad_dir = host_skills / "unreadable"
    bad_dir.mkdir()
    bad_md = bad_dir / "SKILL.md"
    bad_md.write_text("---\nname: unreadable\ndescription: x\n---\nbody")
    bad_md.chmod(0o000)

    good_dir = host_skills / "good"
    good_dir.mkdir()
    (good_dir / "SKILL.md").write_text("---\nname: good\ndescription: y\n---\nContent.")

    try:
        with caplog.at_level("WARNING", logger="omnigent.spec.parser"):
            result = discover_host_skills(agent_root, "all")
    finally:
        # Restore so pytest can clean tmp_path on teardown.
        bad_md.chmod(0o600)

    assert [s.name for s in result] == ["good"]
    skip_records = [rec for rec in caplog.records if "Skipping skill" in rec.message]
    assert len(skip_records) == 1
    msg = skip_records[0].message
    assert str(bad_md) in msg
    assert "could not be read" in msg


# ── top-level ``skills:`` field (host-skill filter) ──────────────


def test_parse_skills_filter_omitted_defaults_to_all(agent_dir: Path) -> None:
    """
    The top-level ``skills:`` field is optional. When omitted, the
    spec defaults to ``"all"`` — every host-discovered skill is
    exposed by default.

    Claim: a config.yaml without ``skills:`` produces
    ``spec.skills_filter == "all"``. A regression that flipped
    the default to ``"none"`` would silently turn every existing
    agent hermetic without warning.
    """
    (agent_dir / "config.yaml").write_text(yaml.dump({"spec_version": 1, "name": "x"}))
    spec = parse(agent_dir)
    assert spec.skills_filter == "all"


def test_parse_skills_filter_explicit_all(agent_dir: Path) -> None:
    """``skills: all`` round-trips as the string ``"all"``."""
    (agent_dir / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "x", "skills": "all"})
    )
    assert parse(agent_dir).skills_filter == "all"


def test_parse_skills_filter_none(agent_dir: Path) -> None:
    """``skills: none`` round-trips as the string ``"none"``."""
    (agent_dir / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "x", "skills": "none"})
    )
    assert parse(agent_dir).skills_filter == "none"


def test_parse_skills_filter_empty_list_normalizes_to_none(agent_dir: Path) -> None:
    """
    ``skills: []`` is an explicit "no skills" declaration —
    normalizes to ``"none"`` so the executor handles both the same
    way.

    Claim: empty list and ``"none"`` produce identical
    ``skills_filter`` values. A regression that distinguished the
    two would create a foot-gun (silent disagreement between two
    YAML shapes that look the same to the user).
    """
    (agent_dir / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "x", "skills": []})
    )
    assert parse(agent_dir).skills_filter == "none"


def test_parse_skills_filter_named_subset(agent_dir: Path) -> None:
    """A list of names round-trips as a list of names."""
    (agent_dir / "config.yaml").write_text(
        yaml.dump(
            {
                "spec_version": 1,
                "name": "x",
                "skills": ["foo", "bar:baz"],
            }
        )
    )
    assert parse(agent_dir).skills_filter == ["foo", "bar:baz"]


def test_parse_skills_filter_invalid_string_rejects(agent_dir: Path) -> None:
    """
    Strings other than ``"all"`` / ``"none"`` are rejected at
    parse time — no silent coercion of typos like ``"al"`` or
    ``"All"`` to a permissive default.
    """
    (agent_dir / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "x", "skills": "al"})
    )
    with pytest.raises(OmnigentError, match=r"\"all\".*\"none\""):
        parse(agent_dir)


def test_parse_skills_filter_non_string_list_item_rejects(agent_dir: Path) -> None:
    """
    Lists with non-string entries (numbers, dicts, nested lists)
    fail loud rather than coercing.
    """
    (agent_dir / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "x", "skills": ["foo", 42]})
    )
    with pytest.raises(OmnigentError, match=r"list items must be strings"):
        parse(agent_dir)


def test_parse_skills_filter_dict_rejects(agent_dir: Path) -> None:
    """
    Mappings (and other unsupported shapes — booleans, integers)
    are rejected. The field is a string or list, never a dict.
    """
    (agent_dir / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "x", "skills": {"all": True}})
    )
    with pytest.raises(OmnigentError, match=r"\"all\".*\"none\""):
        parse(agent_dir)


def test_parse_skills_filter_is_independent_of_bundled_skills_dir(
    agent_dir: Path,
) -> None:
    """
    ``spec.skills`` (bundled SkillSpec list) and ``spec.skills_filter``
    (host filter) are orthogonal: the bundle-side ``skills/`` dir
    and the YAML ``skills:`` field don't shadow each other.

    Claim: a bundle with a ``skills/researcher/SKILL.md`` AND a
    YAML ``skills: none`` field parses both: ``spec.skills`` has
    one entry (the bundled researcher), and ``spec.skills_filter``
    is ``"none"``. A regression that conflated them would lose
    bundled skills when the user opted out of host skills, or
    vice versa.
    """
    skill_dir = agent_dir / "skills" / "researcher"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: researcher\ndescription: Research things.\n---\nDo research.\n"
    )
    (agent_dir / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "x", "skills": "none"})
    )
    spec = parse(agent_dir)
    # Bundled skill is preserved.
    assert len(spec.skills) == 1
    assert spec.skills[0].name == "researcher"
    # And the host filter says "none" — bundled and host are
    # separate channels.
    assert spec.skills_filter == "none"


# ── lenient host-skill discovery ────────────────────


def test_discover_host_skills_skips_missing_frontmatter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Host skills with missing YAML frontmatter are skipped with a
    warning instead of crashing the CLI.

    :param tmp_path: Temporary directory for test fixtures.
    :param monkeypatch: Pytest monkeypatch for isolating ``Path.home()``.
    :param capsys: Pytest capture fixture for stderr assertions.
    """
    from omnigent.spec.parser import discover_host_skills

    # Use a separate home dir so the walk-up from agent_root
    # doesn't double-scan the same .claude/skills/ as Path.home().
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    skills_dir = fake_home / ".claude" / "skills"
    # Good skill.
    good = skills_dir / "good-skill"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text(
        "---\nname: good-skill\ndescription: Works fine.\n---\nContent."
    )
    # Bad skill — no frontmatter.
    bad = skills_dir / "bad-skill"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("# No frontmatter here")

    agent_root = tmp_path / "project"
    agent_root.mkdir()

    result = discover_host_skills(agent_root, skills_filter="all")

    assert len(result) == 1
    assert result[0].name == "good-skill"
    captured = capsys.readouterr()
    assert "skipped 1 skill(s)" in captured.err
    assert "bad-skill" in captured.err


def test_discover_host_skills_skips_yaml_syntax_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Host skills whose frontmatter contains invalid YAML (e.g.
    unquoted colons) are skipped gracefully.

    :param tmp_path: Temporary directory for test fixtures.
    :param monkeypatch: Pytest monkeypatch for isolating ``Path.home()``.
    :param capsys: Pytest capture fixture for stderr assertions.
    """
    from omnigent.spec.parser import discover_host_skills

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    skills_dir = fake_home / ".claude" / "skills"
    broken = skills_dir / "broken-yaml"
    broken.mkdir(parents=True)
    # Unquoted colon in description triggers yaml.scanner.ScannerError.
    (broken / "SKILL.md").write_text(
        "---\nname: broken-yaml\ndescription: TRIGGER when: code imports foo\n---\nContent."
    )

    agent_root = tmp_path / "project"
    agent_root.mkdir()

    result = discover_host_skills(agent_root, skills_filter="all")

    assert result == []
    captured = capsys.readouterr()
    assert "skipped 1 skill(s)" in captured.err
    assert "broken-yaml" in captured.err


def test_discover_host_skills_skips_multiple_bad_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    All broken skills are reported in one pass — no whack-a-mole.

    :param tmp_path: Temporary directory for test fixtures.
    :param monkeypatch: Pytest monkeypatch for isolating ``Path.home()``.
    :param capsys: Pytest capture fixture for stderr assertions.
    """
    from omnigent.spec.parser import discover_host_skills

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    skills_dir = fake_home / ".claude" / "skills"
    for name in ("bad-a", "bad-b"):
        d = skills_dir / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("No frontmatter.")

    agent_root = tmp_path / "project"
    agent_root.mkdir()

    result = discover_host_skills(agent_root, skills_filter="all")

    assert result == []
    captured = capsys.readouterr()
    assert "skipped 2 skill(s)" in captured.err
    assert "bad-a" in captured.err
    assert "bad-b" in captured.err


def test_bundled_skills_still_fail_loud_on_bad_frontmatter(
    agent_dir: Path,
) -> None:
    """
    Bundled skills (inside the agent directory, parsed by
    :func:`parse`) must still fail loud — lenient mode is only
    for host-discovered skills.

    :param agent_dir: Temporary agent directory fixture.
    """
    skill_dir = agent_dir / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("No frontmatter here.")
    with pytest.raises(OmnigentError, match=r"missing YAML frontmatter"):
        parse(agent_dir)


def test_parse_mcp_http(
    agent_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Parse an HTTP MCP server config with env var expansion.

    :param agent_dir: Temporary agent directory fixture.
    :param monkeypatch: Pytest monkeypatch for env vars.
    """
    monkeypatch.setenv("API_KEY", "sk-test-key")
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "my-service",
        "transport": "http",
        "url": "http://localhost:9000/mcp",
        "headers": {"Authorization": "Bearer ${API_KEY}"},
    }
    (mcp_dir / "service.yaml").write_text(yaml.dump(mcp_config))
    spec = parse(agent_dir)
    mcp = spec.mcp_servers[0]
    assert mcp.url == "http://localhost:9000/mcp"
    # ${API_KEY} expanded to the value set via monkeypatch.
    assert mcp.headers == {"Authorization": "Bearer sk-test-key"}


def test_parse_mcp_env_unresolved_var_raises(
    agent_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Unresolved ``${VAR}`` in MCP env raises ``OmnigentError``
    at parse time instead of silently passing the literal to the
    server.

    :param agent_dir: Temporary agent directory fixture.
    :param monkeypatch: Pytest monkeypatch for env vars.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "github",
        "transport": "http",
        "url": "http://localhost:9000/mcp",
        "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
    }
    (mcp_dir / "github.yaml").write_text(yaml.dump(mcp_config))
    with pytest.raises(OmnigentError, match=r"Unresolved environment variable"):
        parse(agent_dir)


def test_parse_mcp_headers_unresolved_var_raises(
    agent_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Unresolved ``${VAR}`` in MCP headers raises ValueError at
    parse time.

    :param agent_dir: Temporary agent directory fixture.
    :param monkeypatch: Pytest monkeypatch for env vars.
    """
    monkeypatch.delenv("API_KEY", raising=False)
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "my-service",
        "transport": "http",
        "url": "http://localhost:9000/mcp",
        "headers": {"Authorization": "Bearer ${API_KEY}"},
    }
    (mcp_dir / "service.yaml").write_text(yaml.dump(mcp_config))
    with pytest.raises(OmnigentError, match=r"Unresolved environment variable"):
        parse(agent_dir)


def test_parse_mcp_env_dollar_without_braces_raises(
    agent_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Unresolved ``$VAR`` (without braces) also raises ValueError.

    :param agent_dir: Temporary agent directory fixture.
    :param monkeypatch: Pytest monkeypatch for env vars.
    """
    monkeypatch.delenv("MY_SECRET", raising=False)
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "test",
        "transport": "http",
        "url": "http://localhost:9000/mcp",
        "headers": {"Secret": "$MY_SECRET"},
    }
    (mcp_dir / "test.yaml").write_text(yaml.dump(mcp_config))
    with pytest.raises(OmnigentError, match=r"Unresolved environment variable"):
        parse(agent_dir)


def test_parse_mcp_missing_name(agent_dir: Path) -> None:
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "bad.yaml").write_text(yaml.dump({"transport": "http", "url": "http://x"}))
    with pytest.raises(OmnigentError, match=r"missing required field 'name'"):
        parse(agent_dir)


def test_parse_mcp_missing_transport(agent_dir: Path) -> None:
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "bad.yaml").write_text(yaml.dump({"name": "bad"}))
    with pytest.raises(OmnigentError, match=r"missing required field 'transport'"):
        parse(agent_dir)


def test_parse_inline_mcp_stdio_server(tmp_path: Path) -> None:
    """
    A ``tools:`` block entry with ``type: mcp`` and ``command`` parses
    as a stdio MCPServerConfig.

    Exercises the ``_parse_inline_mcp_servers`` code path (the tools-block
    style, distinct from bundle-file discovery via ``tools/mcp/*.yaml``).
    If the inline path were broken, ``spec.mcp_servers`` would be empty
    even though the config declares the server.
    """
    config = {
        "spec_version": 1,
        "name": "inline-stdio",
        "tools": {
            "my_mcp": {
                "type": "mcp",
                "command": "uvx",
                "args": ["mcp-server-github"],
            }
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Exactly one server parsed from the inline tools block.
    # If _parse_inline_mcp_servers skips it, len() == 0.
    assert len(spec.mcp_servers) == 1
    srv = spec.mcp_servers[0]
    assert srv.name == "my_mcp"
    # command present → transport inferred as "stdio"
    assert srv.transport == "stdio"
    assert srv.command == "uvx"
    assert srv.args == ["mcp-server-github"]
    # stdio servers have no url
    assert srv.url is None


def test_parse_inline_mcp_http_server(tmp_path: Path) -> None:
    """
    A ``tools:`` block entry with ``type: mcp`` and ``url`` parses
    as an http MCPServerConfig with the optional description preserved.

    If the inline path were broken, ``spec.mcp_servers`` would be empty.
    If transport inference were wrong, ``srv.transport`` would not be
    ``"http"``.
    """
    config = {
        "spec_version": 1,
        "name": "inline-http",
        "tools": {
            "my_service": {
                "type": "mcp",
                "url": "http://localhost:9000/mcp",
                "description": "My HTTP service",
            }
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    assert len(spec.mcp_servers) == 1
    srv = spec.mcp_servers[0]
    assert srv.name == "my_service"
    # url present → transport inferred as "http"
    assert srv.transport == "http"
    assert srv.url == "http://localhost:9000/mcp"
    assert srv.description == "My HTTP service"
    assert srv.command is None
    assert srv.args == []


def test_parse_inline_mcp_skips_standard_tools_keys(tmp_path: Path) -> None:
    """
    The standard ``tools:`` block keys (``agents``, ``builtins``,
    ``timeout``, ``retry``, ``sandbox``) are not mistaken for MCP
    server entries.

    If any standard key slipped through ``_TOOLS_CONFIG_KEYS``, the
    server would emit a spurious MCPServerConfig (with wrong transport)
    or raise because the value shape doesn't match.
    """
    config = {
        "spec_version": 1,
        "name": "with-standard-keys",
        "tools": {
            "agents": ["researcher"],
            "builtins": ["web_search"],
            "timeout": 30,
            "retry": {"max_attempts": 3},
            "sandbox": True,
            # Only this entry should appear in mcp_servers
            "real_mcp": {"type": "mcp", "command": "uvx"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Only the real_mcp entry surfaces — the 5 standard keys are filtered.
    # If any standard key leaked through, len() would be > 1.
    assert len(spec.mcp_servers) == 1
    assert spec.mcp_servers[0].name == "real_mcp"


def test_parse_tools_sandbox_docker_image_alias(tmp_path: Path) -> None:
    """Legacy ``tools.sandbox.docker_image`` remains a valid image alias."""
    config = {
        "spec_version": 1,
        "name": "legacy-docker-image",
        "tools": {
            "sandbox": {
                "docker_image": "python:3.12-slim",
                "container_runtime": "podman",
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    assert spec.tools.sandbox.container_image == "python:3.12-slim"
    assert spec.tools.sandbox.docker_image == "python:3.12-slim"
    assert spec.tools.sandbox.container_runtime == "podman"


def test_parse_tools_sandbox_container_image_precedence(tmp_path: Path) -> None:
    """Preferred ``container_image`` wins when both image keys exist."""
    config = {
        "spec_version": 1,
        "name": "container-image-precedence",
        "tools": {
            "sandbox": {
                "container_image": "python:3.12-slim",
                "docker_image": "python:3.11-slim",
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    assert spec.tools.sandbox.container_image == "python:3.12-slim"
    assert spec.tools.sandbox.docker_image == "python:3.12-slim"


def test_parse_inline_mcp_skips_non_mcp_type_entries(tmp_path: Path) -> None:
    """
    Tools-block entries whose ``type`` is not ``"mcp"`` are silently
    ignored by the inline parser.

    Fails if any non-mcp entry is incorrectly treated as an MCP server.
    """
    config = {
        "spec_version": 1,
        "name": "mixed-tools",
        "tools": {
            "python_tool": {"type": "python", "path": "tools/python/foo.py"},
            "mcp_tool": {"type": "mcp", "command": "uvx"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # python_tool must be skipped; only the mcp entry surfaces.
    assert len(spec.mcp_servers) == 1
    assert spec.mcp_servers[0].name == "mcp_tool"


def test_parse_inline_mcp_databricks_only_skipped(tmp_path: Path) -> None:
    """
    An inline ``type: mcp`` entry with no ``command`` or ``url``
    (only ``databricks_server``) is silently skipped because no
    transport can be inferred.

    If the skip were missing, parse() would raise or produce a
    server with an incorrect transport.
    """
    config = {
        "spec_version": 1,
        "name": "db-only",
        "tools": {
            "db_mcp": {
                "type": "mcp",
                "databricks_server": {"name": "some_server"},
            }
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # No command/url → transport unresolvable → entry skipped.
    # If the skip were removed, mcp_servers would be non-empty.
    assert spec.mcp_servers == []


def test_parse_inline_mcp_headers_and_env_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Inline ``type: mcp`` entries expand ``${VAR}`` in ``headers``
    (http transport) and ``env`` (stdio transport).

    If the inline parser dropped these fields (the pre-fix behavior),
    both ``headers`` and ``env`` would be empty dicts.
    """
    monkeypatch.setenv("MCP_TOKEN", "secret-123")
    monkeypatch.setenv("MY_KEY", "val-456")
    config = {
        "spec_version": 1,
        "name": "inline-expand",
        "tools": {
            "svc": {
                "type": "mcp",
                "url": "http://localhost/mcp",
                "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
            },
            "cli": {
                "type": "mcp",
                "command": "my-mcp",
                "env": {"MY_KEY": "${MY_KEY}"},
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    http_srv = next(s for s in spec.mcp_servers if s.name == "svc")
    assert http_srv.headers == {"Authorization": "Bearer secret-123"}

    stdio_srv = next(s for s in spec.mcp_servers if s.name == "cli")
    assert stdio_srv.env == {"MY_KEY": "val-456"}


def test_parse_inline_mcp_rejects_non_dict_headers(tmp_path: Path) -> None:
    """
    Non-dict ``headers`` on an inline MCP entry raises
    ``OmnigentError`` instead of silently falling back to ``{}``.

    Without the validation, a typo like ``headers: "Bearer tok"``
    would be silently ignored and the MCP server would connect
    unauthenticated.
    """
    config = {
        "spec_version": 1,
        "name": "bad-headers",
        "tools": {
            "svc": {
                "type": "mcp",
                "url": "http://localhost/mcp",
                "headers": "Bearer tok",
            }
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"headers.*must be a mapping"):
        parse(tmp_path)


def test_parse_inline_mcp_rejects_non_dict_env(tmp_path: Path) -> None:
    """
    Non-dict ``env`` on an inline stdio MCP entry raises
    ``OmnigentError`` instead of silently falling back to ``{}``.

    Without the validation, ``env: "FOO=bar"`` would be silently
    dropped and the subprocess would launch without the intended
    environment variable.
    """
    config = {
        "spec_version": 1,
        "name": "bad-env",
        "tools": {
            "cli": {
                "type": "mcp",
                "command": "my-mcp",
                "env": "FOO=bar",
            }
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"env.*must be a mapping"):
        parse(tmp_path)


def test_parse_inline_and_bundle_mcp_combined(tmp_path: Path) -> None:
    """
    Inline ``tools:`` block MCP entries and ``tools/mcp/*.yaml`` bundle
    files are both collected and merged into ``spec.mcp_servers``.

    Verifies that the two code paths (``_discover_mcp_servers`` and
    ``_parse_inline_mcp_servers``) are concatenated, not one silently
    shadowing the other. If only one path ran, len() would be 1.
    """
    # Bundle-file MCP server (no auth so no env-var expansion needed)
    mcp_dir = tmp_path / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "bundle_srv.yaml").write_text(
        yaml.dump(
            {
                "name": "bundle_srv",
                "transport": "http",
                "url": "http://bundle.example.com/mcp",
            }
        )
    )
    # config.yaml also declares an inline MCP server
    config = {
        "spec_version": 1,
        "name": "combined",
        "tools": {
            "inline_mcp": {"type": "mcp", "command": "uvx"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Both sources contribute — two distinct entries.
    # If only one path ran, len() would be 1.
    assert len(spec.mcp_servers) == 2
    names = {srv.name for srv in spec.mcp_servers}
    assert names == {"bundle_srv", "inline_mcp"}


def test_parse_local_python_tools(agent_dir: Path) -> None:
    py_dir = agent_dir / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "arxiv_search.py").write_text("def search(): pass")
    (py_dir / "web_scrape.py").write_text("def scrape(): pass")
    spec = parse(agent_dir)
    assert len(spec.local_tools) == 2
    names = {t.name for t in spec.local_tools}
    assert names == {"arxiv_search", "web_scrape"}
    assert all(t.language == "python" for t in spec.local_tools)


def test_parse_local_typescript_tools(agent_dir: Path) -> None:
    ts_dir = agent_dir / "tools" / "typescript"
    ts_dir.mkdir(parents=True)
    (ts_dir / "code_run.ts").write_text("export function run() {}")
    spec = parse(agent_dir)
    assert len(spec.local_tools) == 1
    assert spec.local_tools[0].name == "code_run"
    assert spec.local_tools[0].language == "typescript"


def test_parse_sub_agents(tmp_path: Path) -> None:
    # Parent config referencing two sub-agents
    parent_config = {
        "spec_version": 1,
        "name": "parent",
        "tools": {"agents": ["researcher", "critic"]},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(parent_config))

    # Sub-agent: researcher
    researcher_dir = tmp_path / "agents" / "researcher"
    researcher_dir.mkdir(parents=True)
    (researcher_dir / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "researcher"})
    )

    # Sub-agent: critic
    critic_dir = tmp_path / "agents" / "critic"
    critic_dir.mkdir(parents=True)
    (critic_dir / "config.yaml").write_text(yaml.dump({"spec_version": 1, "name": "critic"}))

    spec = parse(tmp_path)
    assert len(spec.sub_agents) == 2
    sub_names = {sa.name for sa in spec.sub_agents}
    assert sub_names == {"researcher", "critic"}


def test_parse_interaction_defaults(agent_dir: Path) -> None:
    """Omitting interaction block entirely gives defaults."""
    spec = parse(agent_dir)
    assert spec.interaction.conversational is True
    assert spec.interaction.modalities.input == ["text"]
    assert spec.interaction.modalities.output == ["text"]


def test_parse_interaction_partial_modalities(tmp_path: Path) -> None:
    """Omitting one side of modalities defaults that side to [text]."""
    config = {
        "spec_version": 1,
        "interaction": {"modalities": {"input": ["text", "image"]}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.interaction.modalities.input == ["text", "image"]
    assert spec.interaction.modalities.output == ["text"]


def test_parse_os_env_absent_yields_none(agent_dir: Path) -> None:
    """A native YAML without an ``os_env:`` block leaves
    ``spec.os_env`` as ``None`` — no sys_os_* tools registered.

    What breaks if this fails: the runtime would build a default
    :class:`OSEnvironment` for every agent and silently expose
    ``sys_os_read/write/edit/shell`` on agents that never opted
    into them, regressing the "no os_env declared = no FS access"
    contract from the omnigent-compat path.
    """
    spec = parse(agent_dir)
    assert spec.os_env is None


def test_parse_os_env_caller_process(tmp_path: Path) -> None:
    """A native YAML ``os_env:`` mapping parses into a real
    :class:`OSEnvSpec` with the declared ``type`` and ``cwd``.

    What breaks if this fails: native Omnigent YAMLs cannot opt into
    sys_os_* tools — the whole point of step 5l.
    """
    from omnigent.inner.datamodel import OSEnvSpec

    config = {
        "spec_version": 1,
        "name": "with-os-env",
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    # Real OSEnvSpec dataclass — not a dict — so the runtime's
    # isinstance check in ToolManager._register_os_env_tools
    # registers the sys_os_* tools.
    assert isinstance(spec.os_env, OSEnvSpec)
    assert spec.os_env.type == "caller_process"
    assert spec.os_env.cwd == "."
    # Sandbox absent → None (the wrap then defaults appropriately).
    assert spec.os_env.sandbox is None
    assert spec.os_env.fork is False


def test_parse_os_env_with_sandbox(tmp_path: Path) -> None:
    """The nested ``sandbox:`` block parses into a real
    :class:`OSEnvSandboxSpec` with all its fields.

    What breaks if this fails: agents that declared sandbox /
    write-path constraints in YAML would silently lose them at
    runtime, leaving sys_os_* tools running with the agent's
    full process privileges.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    config = {
        "spec_version": 1,
        "name": "with-sandbox",
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {
                "type": "linux_bwrap",
                "read_paths": ["/usr"],
                "write_paths": ["."],
                "write_files": ["/home/me/.claude.json"],
                "allow_network": False,
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert isinstance(spec.os_env, OSEnvSpec)
    assert isinstance(spec.os_env.sandbox, OSEnvSandboxSpec)
    sandbox = spec.os_env.sandbox
    assert sandbox.type == "linux_bwrap"
    assert sandbox.read_paths == ["/usr"]
    assert sandbox.write_paths == ["."]
    # write_files is the per-file grant carve-out for files like
    # ~/.claude.json that can't be expressed as a directory write
    # path — the parser must thread it through.
    assert sandbox.write_files == ["/home/me/.claude.json"]
    assert sandbox.allow_network is False


def test_parse_os_env_non_mapping_raises(tmp_path: Path) -> None:
    """A scalar/list under ``os_env:`` raises OmnigentError —
    fail loud rather than silently dropping the malformed block.
    """
    config = {
        "spec_version": 1,
        "name": "bad-os-env",
        "os_env": "caller_process",
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"os_env must be a YAML mapping"):
        parse(tmp_path)


def test_parse_os_env_sandbox_non_mapping_raises(tmp_path: Path) -> None:
    """A scalar/list under ``os_env.sandbox:`` raises
    OmnigentError — same fail-loud contract as the parent.
    """
    config = {
        "spec_version": 1,
        "name": "bad-sandbox",
        "os_env": {"type": "caller_process", "sandbox": "linux_bwrap"},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"os_env.sandbox must be a YAML mapping"):
        parse(tmp_path)


def test_parse_os_env_sandbox_with_cwd_allow_hidden(tmp_path: Path) -> None:
    """
    ``cwd_allow_hidden`` parses through to
    :class:`OSEnvSandboxSpec.cwd_allow_hidden` verbatim. The bwrap
    backend reads this list and skips masking those names; default
    substitution (``[".venv"]``) happens in the bwrap resolver, not
    here, so the parser must preserve ``None`` vs empty list vs
    non-empty list distinctions.
    """
    config = {
        "spec_version": 1,
        "name": "with-allow-hidden",
        "os_env": {
            "type": "caller_process",
            "sandbox": {
                "type": "linux_bwrap",
                "cwd_allow_hidden": [".venv", ".cache"],
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None
    assert spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.cwd_allow_hidden == [".venv", ".cache"]


def test_parse_os_env_sandbox_cwd_allow_hidden_empty_list_preserved(
    tmp_path: Path,
) -> None:
    """
    An explicit empty list must NOT collapse to ``None``. The
    distinction matters: ``None`` triggers the bwrap resolver's
    documented default (``[".venv"]``); ``[]`` means "mask every
    dotfile, allow nothing." A parser that conflates the two would
    silently re-expose ``.venv`` to a hardened-mode user.
    """
    config = {
        "spec_version": 1,
        "name": "empty-allow-hidden",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap", "cwd_allow_hidden": []},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None
    assert spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.cwd_allow_hidden == []


@pytest.mark.parametrize(
    "bad_value,match_regex",
    [
        (".venv", r"must be a list"),
        ([".venv", 7], r"must be strings"),
        ([".venv", ""], r"must not be empty"),
        ([".venv/secret"], r"single path components"),
        (["../etc"], r"single path components"),
    ],
    ids=["scalar", "non_string_entry", "empty_string", "with_slash", "traversal"],
)
def test_parse_os_env_sandbox_cwd_allow_hidden_validation(
    tmp_path: Path, bad_value: object, match_regex: str
) -> None:
    """
    Invalid ``cwd_allow_hidden`` values raise
    :class:`OmnigentError` at parse time with a message that
    points the author at the rule they violated.

    Validation is the only thing standing between a typo'd YAML
    and a sandbox that exposes ``../etc`` (path traversal) — fail
    loud is the right contract here.
    """
    config = {
        "spec_version": 1,
        "name": "bad-allow-hidden",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap", "cwd_allow_hidden": bad_value},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=match_regex):
        parse(tmp_path)


def test_parse_os_env_sandbox_cwd_hidden_scan_defaults(tmp_path: Path) -> None:
    """
    When the spec omits ``cwd_hidden_scan_max_entries`` and
    ``cwd_hidden_scan_overflow``, the parsed
    :class:`OSEnvSandboxSpec` carries the dataclass defaults (cap
    50000, overflow ``"warn"``).

    Pinning these defaults here means a future change to the
    dataclass surfaces in this test rather than silently shifting
    the contract everyone depends on. The overflow default is
    ``"warn"`` (best-effort partial mask) rather than ``"error"`` so
    heavy-but-trusted trees like ``node_modules`` don't block every
    spawn; untrusted trees opt back into ``"error"``.
    """
    config = {
        "spec_version": 1,
        "name": "default-scan",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.cwd_hidden_scan_max_entries == 50000
    assert spec.os_env.sandbox.cwd_hidden_scan_overflow == "warn"


def test_parse_os_env_sandbox_cwd_hidden_scan_explicit_values(tmp_path: Path) -> None:
    """
    Explicit ``cwd_hidden_scan_max_entries`` + ``..._overflow`` values
    pass through to the spec.
    """
    config = {
        "spec_version": 1,
        "name": "tuned-scan",
        "os_env": {
            "type": "caller_process",
            "sandbox": {
                "type": "linux_bwrap",
                "cwd_hidden_scan_max_entries": 100000,
                "cwd_hidden_scan_overflow": "warn",
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.cwd_hidden_scan_max_entries == 100000
    assert spec.os_env.sandbox.cwd_hidden_scan_overflow == "warn"


@pytest.mark.parametrize(
    "bad_value,match_regex",
    [
        ("not-a-number", r"must be an integer"),
        (True, r"must be an integer"),
        (0, r"must be > 0"),
        (-1, r"must be > 0"),
    ],
    ids=["string", "bool", "zero", "negative"],
)
def test_parse_os_env_sandbox_cwd_hidden_scan_max_entries_validation(
    tmp_path: Path, bad_value: object, match_regex: str
) -> None:
    """
    Non-integer or non-positive caps fail at parse time. The bool
    rejection is intentional — YAML scalars are loose, and ``True``
    masquerading as ``1`` would be a confusing accident.
    """
    config = {
        "spec_version": 1,
        "name": "bad-cap",
        "os_env": {
            "type": "caller_process",
            "sandbox": {
                "type": "linux_bwrap",
                "cwd_hidden_scan_max_entries": bad_value,
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=match_regex):
        parse(tmp_path)


@pytest.mark.parametrize(
    "bad_value",
    ["fail", "ignore", 42, ["warn"]],
    ids=["unknown_string", "synonym_attempt", "int", "list"],
)
def test_parse_os_env_sandbox_cwd_hidden_scan_overflow_validation(
    tmp_path: Path, bad_value: object
) -> None:
    """
    Only ``"error"``, ``"warn"``, ``"unlimited"`` are accepted.
    Anything else fails at parse time so misconfigurations don't
    silently degrade to default behavior.
    """
    config = {
        "spec_version": 1,
        "name": "bad-overflow",
        "os_env": {
            "type": "caller_process",
            "sandbox": {
                "type": "linux_bwrap",
                "cwd_hidden_scan_overflow": bad_value,
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"must be one of"):
        parse(tmp_path)


def test_parse_ignores_unknown_files(agent_dir: Path) -> None:
    """Parser ignores files/directories not in the spec."""
    (agent_dir / "README.md").write_text("Ignored")
    (agent_dir / "extra_dir").mkdir()
    (agent_dir / "extra_dir" / "stuff.txt").write_text("Ignored")
    spec = parse(agent_dir)
    assert spec.name == "test-agent"


def test_parse_multiple_skills_sorted(agent_dir: Path) -> None:
    """Skills are discovered in sorted directory order."""
    for name in ["beta-skill", "alpha-skill"]:
        skill_dir = agent_dir / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Skill {name}.\n---\nContent."
        )
    spec = parse(agent_dir)
    assert [s.name for s in spec.skills] == ["alpha-skill", "beta-skill"]


# ── Env var expansion in MCP configs ───────────────────


def test_mcp_env_vars_expanded_from_environment(
    agent_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``${VAR}`` references in MCP env and headers are expanded
    against the process environment at parse time.
    """
    monkeypatch.setenv("MY_TOKEN", "secret-123")
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "token-server",
        "transport": "http",
        "url": "http://localhost:9000/mcp",
        "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
    }
    (mcp_dir / "token.yaml").write_text(yaml.dump(mcp_config))
    spec = parse(agent_dir)
    assert spec.mcp_servers[0].headers == {"Authorization": "Bearer secret-123"}


def test_mcp_headers_expanded_from_environment(
    agent_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``${VAR}`` references in HTTP headers are expanded at parse
    time.
    """
    monkeypatch.setenv("MY_API_KEY", "key-abc")
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "auth-service",
        "transport": "http",
        "url": "http://localhost:9000/mcp",
        "headers": {"Authorization": "Bearer ${MY_API_KEY}"},
    }
    (mcp_dir / "auth.yaml").write_text(yaml.dump(mcp_config))
    spec = parse(agent_dir)
    assert spec.mcp_servers[0].headers == {
        "Authorization": "Bearer key-abc",
    }


def test_mcp_env_expansion_mixed_set_and_unset_raises(
    agent_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    If any env value contains an unresolved ``${VAR}``, parsing
    raises ValueError even when other vars are set.

    :param agent_dir: Temporary agent directory fixture.
    :param monkeypatch: Pytest monkeypatch for env vars.
    """
    monkeypatch.setenv("SET_VAR", "expanded")
    monkeypatch.delenv("UNSET_VAR", raising=False)
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "mixed",
        "transport": "http",
        "url": "http://localhost:9000/mcp",
        "headers": {
            "A": "${SET_VAR}",
            "B": "${UNSET_VAR}",
            "C": "plain-value",
        },
    }
    (mcp_dir / "mixed.yaml").write_text(yaml.dump(mcp_config))
    with pytest.raises(OmnigentError, match=r"Unresolved environment variable"):
        parse(agent_dir)


# ── MCP required field validation ─────────────────────


def test_mcp_missing_url_raises(agent_dir: Path) -> None:
    """
    Parser rejects an MCP config with ``transport: http`` but no
    ``url`` field.

    :param agent_dir: Temporary agent directory with minimal
        ``config.yaml``.
    """
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "no-url-server",
        "transport": "http",
        # url intentionally omitted
    }
    (mcp_dir / "no_url.yaml").write_text(yaml.dump(mcp_config))
    with pytest.raises(OmnigentError, match=r"missing required field 'url'"):
        parse(agent_dir)


# ── Timeout / retry / execution parsing ────────────────


def test_parse_llm_timeout_and_retry(tmp_path: Path) -> None:
    """LLM block with explicit request_timeout and retry overrides."""
    config = {
        "spec_version": 1,
        "llm": {
            "model": "openai/gpt-5.4",
            "request_timeout": 120,
            "retry": {
                "max_retries": 5,
                "retryable_status_codes": [429, 502],
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.llm is not None

    # Explicit request_timeout should override the 300s default.
    # Failure means the parser ignores the request_timeout key.
    assert spec.llm.request_timeout == 120

    # Retry max_retries should match the YAML value.
    # Failure means retry block is not parsed or defaults are used instead.
    assert spec.llm.retry.max_retries == 5

    # Status codes should reflect the custom list, not the defaults.
    # Failure means the parser falls back to default status codes.
    assert spec.llm.retry.retryable_status_codes == (429, 502)


def test_parse_llm_timeout_defaults(tmp_path: Path) -> None:
    """LLM block with only model inherits default timeout and retry."""
    config = {
        "spec_version": 1,
        "llm": {"model": "openai/gpt-4o"},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.llm is not None

    # Default LLM request_timeout is 300s per LLMConfig dataclass.
    # Failure means the parser sets a different default.
    assert spec.llm.request_timeout == 300

    # Default retry max_retries is 7 per RetryPolicy dataclass.
    # Failure means the parser produces a non-default retry config.
    assert spec.llm.retry.max_retries == 7


def test_parse_tools_global_timeout_and_retry(tmp_path: Path) -> None:
    """Tools block with explicit timeout and retry overrides."""
    config = {
        "spec_version": 1,
        "tools": {
            "timeout": 30,
            "retry": {"max_retries": 4},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Explicit tools timeout should override the 60s default.
    # Failure means the parser ignores the tools timeout key.
    assert spec.tools.timeout == 30

    # Retry max_retries should match the YAML value.
    # Failure means the tools retry block is not parsed.
    assert spec.tools.retry.max_retries == 4


def test_parse_builtins_string_entries(tmp_path: Path) -> None:
    """Plain string entries in tools.builtins produce BuiltinToolConfig
    with empty config dicts."""
    config = {
        "spec_version": 1,
        "tools": {
            "builtins": ["web_search", "web_search_alt"],
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Two entries parsed, both with empty config.
    assert len(spec.tools.builtins) == 2
    assert spec.tools.builtins[0].name == "web_search"
    assert spec.tools.builtins[0].config == {}
    assert spec.tools.builtins[1].name == "web_search_alt"
    assert spec.tools.builtins[1].config == {}


def test_parse_builtins_dict_entries(tmp_path: Path) -> None:
    """Dict entries in tools.builtins carry tool-specific config."""
    config = {
        "spec_version": 1,
        "tools": {
            "builtins": [
                {
                    "name": "web_search_alt",
                    "api_key": "AIza-test",
                    "engine_id": "eng-123",
                },
            ],
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    assert len(spec.tools.builtins) == 1
    entry = spec.tools.builtins[0]
    assert entry.name == "web_search_alt"
    # Config contains all keys except 'name'.
    assert entry.config == {
        "api_key": "AIza-test",
        "engine_id": "eng-123",
    }


def test_parse_builtins_mixed_entries(tmp_path: Path) -> None:
    """tools.builtins supports a mix of strings and dicts."""
    config = {
        "spec_version": 1,
        "tools": {
            "builtins": [
                "web_search",
                {
                    "name": "web_search_cfg",
                    "api_key": "pplx-test",
                },
            ],
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    assert len(spec.tools.builtins) == 2
    # First entry: string → no config.
    assert spec.tools.builtins[0].name == "web_search"
    assert spec.tools.builtins[0].config == {}
    # Second entry: dict → has config.
    assert spec.tools.builtins[1].name == "web_search_cfg"
    assert spec.tools.builtins[1].config == {"api_key": "pplx-test"}


def test_parse_builtins_dict_missing_name(tmp_path: Path) -> None:
    """Dict entry without 'name' raises OmnigentError."""
    config = {
        "spec_version": 1,
        "tools": {
            "builtins": [
                {"api_key": "orphan-key"},
            ],
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"name"):
        parse(tmp_path)


def test_parse_executor_config(tmp_path: Path) -> None:
    """Executor block with explicit timeout and max_iterations."""
    config = {
        "spec_version": 1,
        "executor": {
            "timeout": 7200,
            "max_iterations": 500,
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Explicit executor timeout should be honored.
    # Failure means executor block parsing is broken.
    assert spec.executor.timeout == 7200

    # Explicit max_iterations should override the 1000 default.
    # Failure means max_iterations is ignored by the parser.
    assert spec.executor.max_iterations == 500

    # Default type should be "omnigent" when not specified.
    # Failure means the parser doesn't apply the default type.
    assert spec.executor.type == "omnigent"


def test_parse_executor_defaults(tmp_path: Path) -> None:
    """No executor block yields ExecutorSpec defaults."""
    config = {"spec_version": 1}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Default executor timeout is 3600s per ExecutorSpec.
    # Failure means the parser uses a different default.
    assert spec.executor.timeout == 3600

    # Default max_iterations is 1000 per ExecutorSpec.
    # Failure means the parser uses a different default.
    assert spec.executor.max_iterations == 1000

    # Default type is "omnigent" per ExecutorSpec.
    # Failure means the parser uses a different default.
    assert spec.executor.type == "omnigent"


def test_parse_executor_config_field(tmp_path: Path) -> None:
    """Executor block with a ``config`` sub-block parses string values.

    The ``config`` field is executor-type-specific. For the omnigent
    executor it carries ``harness`` / ``profile``. The parser coerces
    values to strings so non-string YAML scalars (numbers, bools)
    round-trip as their string form.
    """
    config = {
        "spec_version": 1,
        "executor": {
            "type": "omnigent",
            "config": {
                "harness": "claude-sdk",
                "profile": "test-profile",
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Failure means the parser silently drops the config block,
    # breaking omnigent harness selection at executor construction.
    assert spec.executor.type == "omnigent"
    assert spec.executor.config == {
        "harness": "claude-sdk",
        "profile": "test-profile",
    }


def test_parse_executor_config_missing_defaults_to_empty(
    tmp_path: Path,
) -> None:
    """Absent ``executor.config`` block yields an empty dict, not None."""
    config = {"spec_version": 1, "executor": {"type": "omnigent"}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # Failure means callers that do ``spec.executor.config.get(...)``
    # would hit AttributeError on None.
    assert spec.executor.config == {}


def test_parse_mcp_server_with_timeout_and_retry(
    agent_dir: Path,
) -> None:
    """MCP server YAML with per-server timeout and retry overrides."""
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "slow-service",
        "transport": "http",
        "url": "http://localhost:9000/mcp",
        "timeout": 120,
        "retry": {
            "max_retries": 7,
            "backoff_base_s": 3.0,
        },
    }
    (mcp_dir / "slow.yaml").write_text(yaml.dump(mcp_config))
    spec = parse(agent_dir)
    assert len(spec.mcp_servers) == 1
    mcp = spec.mcp_servers[0]

    # Per-server timeout should be parsed from the YAML.
    # Failure means MCP timeout parsing is broken (returns None).
    assert mcp.timeout == 120

    # Per-server retry should be populated, not None.
    # Failure means the retry block is ignored for MCP servers.
    assert mcp.retry is not None

    # Retry max_retries should match the YAML value.
    # Failure means MCP retry fields are not forwarded correctly.
    assert mcp.retry.max_retries == 7


def test_parse_mcp_stdio_minimal(agent_dir: Path) -> None:
    """
    Parse a stdio MCP server with only the required ``command``.

    What breaks if this fails: authors declaring a subprocess MCP
    (the common glean / github / databricks shape) would see the
    parser reject the whole spec at load time.

    :param agent_dir: Temporary agent directory fixture.
    """
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "local-tool",
        "transport": "stdio",
        "command": "npx",
    }
    (mcp_dir / "local.yaml").write_text(yaml.dump(mcp_config))
    spec = parse(agent_dir)
    mcp = spec.mcp_servers[0]
    # Transport + command survive the parse.
    assert mcp.transport == "stdio"
    assert mcp.command == "npx"
    # Defaults: empty args / env. The legacy ``sandbox: bool``
    # field was removed in step 7; the parse should still work
    # without it.
    assert mcp.args == []
    assert mcp.env == {}
    # HTTP fields unset on stdio.
    assert mcp.url is None
    assert mcp.headers == {}


def test_parse_mcp_stdio_with_args_and_env(
    agent_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Parse a stdio MCP with every field populated, including
    ``${VAR}`` expansion in ``env``.

    What breaks if this fails: a YAML like the databricks /
    github MCPs (``env: {GITHUB_TOKEN: ${GITHUB_TOKEN}}``) would
    either pass the literal ``${GITHUB_TOKEN}`` to the subprocess
    (breaking auth) or fail at parse time.

    :param agent_dir: Temporary agent directory fixture.
    :param monkeypatch: Pytest monkeypatch for env vars.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xyz")
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
    }
    (mcp_dir / "github.yaml").write_text(yaml.dump(mcp_config))
    spec = parse(agent_dir)
    mcp = spec.mcp_servers[0]
    assert mcp.transport == "stdio"
    assert mcp.command == "npx"
    # Args preserved verbatim, not expanded (they're a literal argv).
    assert mcp.args == ["-y", "@modelcontextprotocol/server-github"]
    # ${GITHUB_TOKEN} expanded via monkeypatch — the subprocess sees
    # the real token, not the literal.
    assert mcp.env == {"GITHUB_TOKEN": "ghp_xyz"}


def test_parse_mcp_stdio_rejects_legacy_sandbox_field(agent_dir: Path) -> None:
    """
    A YAML that still declares ``sandbox: <bool>`` on a stdio MCP
    is rejected with a clear migration message.

    Step 7 of the harness contract migration removed the
    ``MCPServerConfig.sandbox`` field. The previous default
    (srt-wrap) blocked outbound network and silently broke
    every useful MCP. Failing loud at parse time tells users
    porting old YAMLs to drop the key, instead of silently
    accepting it as a no-op.

    What breaks if this fails: anyone copying a pre-step-7
    MCP YAML gets a confusing "tool not found" or hang at
    runtime; this test ensures the parser produces a
    direct, actionable error instead.
    """
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    mcp_config = {
        "name": "legacy",
        "transport": "stdio",
        "command": "npx",
        "sandbox": False,
    }
    (mcp_dir / "legacy.yaml").write_text(yaml.dump(mcp_config))
    with pytest.raises(OmnigentError, match=r"sandbox.*was removed"):
        parse(agent_dir)


def test_parse_mcp_stdio_missing_command_raises(agent_dir: Path) -> None:
    """
    Stdio MCP without ``command`` fails loud at parse time.

    What breaks if this fails: authors who typo ``command:`` would
    get a runtime AttributeError at MCP connection time instead of
    a clean parse error naming the missing field.

    :param agent_dir: Temporary agent directory fixture.
    """
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "broken.yaml").write_text(yaml.dump({"name": "broken", "transport": "stdio"}))
    with pytest.raises(OmnigentError, match=r"missing required field 'command'"):
        parse(agent_dir)


def test_parse_mcp_stdio_rejects_http_fields(agent_dir: Path) -> None:
    """
    Stdio MCP with a stray ``url:`` (copy-pasted from an HTTP
    example) fails loud at parse time instead of silently ignoring
    the wrong-transport field.

    What breaks if this fails: authors migrating between
    transports see their changes silently ignored — e.g. an HTTP
    config edited to stdio but still carrying ``url`` looks fine
    but doesn't actually use the URL.

    :param agent_dir: Temporary agent directory fixture.
    """
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "mixed.yaml").write_text(
        yaml.dump(
            {
                "name": "mixed",
                "transport": "stdio",
                "command": "npx",
                "url": "http://stale.example/sse",
            }
        )
    )
    with pytest.raises(OmnigentError, match=r"wrong-transport field"):
        parse(agent_dir)


def test_parse_mcp_http_rejects_stdio_fields(agent_dir: Path) -> None:
    """
    HTTP MCP with a stray ``command:`` fails loud at parse time.

    Mirror of the stdio-rejects-HTTP test. Symmetric coverage so
    either direction of mistaken transport mixing is caught.

    :param agent_dir: Temporary agent directory fixture.
    """
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "mixed.yaml").write_text(
        yaml.dump(
            {
                "name": "mixed",
                "transport": "http",
                "url": "http://mcp.example.com/sse",
                "command": "npx",
            }
        )
    )
    with pytest.raises(OmnigentError, match=r"wrong-transport field"):
        parse(agent_dir)


def test_parse_mcp_unknown_transport_raises(agent_dir: Path) -> None:
    """
    ``transport: grpc`` or any other value fails loud with a
    clear "must be 'http' or 'stdio'" message.

    What breaks if this fails: a typo like ``stdin`` (instead of
    ``stdio``) would produce a cryptic downstream error
    ("AttributeError: 'NoneType' object has no attribute
    'rstrip'" or similar) instead of naming the field.

    :param agent_dir: Temporary agent directory fixture.
    """
    mcp_dir = agent_dir / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "weird.yaml").write_text(
        yaml.dump(
            {
                "name": "weird",
                "transport": "grpc",
                "command": "something",
            }
        )
    )
    with pytest.raises(OmnigentError, match=r"must be 'http' or 'stdio'"):
        parse(agent_dir)


# ─── Top-level ``timers:`` flag (step 10 of harness contract) ─


def test_parse_timers_defaults_to_false_when_omitted(agent_dir: Path) -> None:
    """
    Without a top-level ``timers:`` key the parsed ``AgentSpec.timers``
    is ``False``.

    Default-off matches the inner stack (``AgentDef.timers`` is also
    ``False`` by default) — agents authored before step 10 must keep
    their pre-step-10 tool surface unchanged. A regression that
    flipped the default to ``True`` would silently expose the timer
    builtins to every agent.

    :param agent_dir: Temporary agent directory fixture.
    """
    spec = parse(agent_dir)
    assert spec.timers is False


def test_parse_timers_true_sets_flag(tmp_path: Path) -> None:
    """
    ``timers: true`` in config.yaml round-trips to
    ``AgentSpec.timers == True``.

    The flag is the gate for ``ToolManager._register_timer_tools``
    (see step 10) — a regression where the parser dropped the
    field would mean the YAML opt-in had no effect at runtime.

    :param tmp_path: pytest-provided temporary directory.
    """
    config = {"spec_version": 1, "name": "timer-agent", "timers": True}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.timers is True


# ─── Top-level ``spawn:`` flag (spawn-write opt-in) ───────────


def test_parse_spawn_defaults_to_false_when_omitted(agent_dir: Path) -> None:
    """
    Without a top-level ``spawn:`` key the parsed ``AgentSpec.spawn``
    is ``False``.

    Default-off is the design: session *reads* are always available,
    but the child-session spawn writes (``sys_session_create`` /
    ``sys_session_send`` / ``sys_session_close``) are opt-in. A
    regression that flipped the default to ``True`` would silently
    expose the spawn-write surface to every agent.

    :param agent_dir: Temporary agent directory fixture.
    """
    spec = parse(agent_dir)
    assert spec.spawn is False


def test_parse_spawn_true_sets_flag(tmp_path: Path) -> None:
    """
    ``spawn: true`` in config.yaml round-trips to
    ``AgentSpec.spawn == True``.

    The flag is the sole grant for ``sys_session_create`` in
    ``ToolManager._register_sub_agent_tools`` (``tools.agents`` only
    permits the declared sub-agent list via send/close) — a regression
    where the parser dropped the field would mean the YAML opt-in had
    no effect at runtime and the agent couldn't author-and-launch
    child sessions.

    :param tmp_path: pytest-provided temporary directory.
    """
    config = {"spec_version": 1, "name": "spawn-agent", "spawn": True}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.spawn is True


# ---------------------------------------------------------------------------
# os_env.sandbox.env_passthrough
# ---------------------------------------------------------------------------


def test_parse_os_env_sandbox_env_passthrough_default_none(tmp_path: Path) -> None:
    """
    Omitting ``env_passthrough`` parses to ``None``, which the helper
    spawn path treats as "only the always-passed defaults".

    Pinning the default here ensures that future spec changes don't
    silently flip the helper to "inherit everything from the parent",
    which would re-open the credential-leak vector
    :func:`omnigent.inner.os_env.build_helper_env` is meant to close.
    """
    config = {
        "spec_version": 1,
        "name": "no-passthrough",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.env_passthrough is None


def test_parse_os_env_sandbox_env_passthrough_explicit_list(tmp_path: Path) -> None:
    """
    A list of valid POSIX env-var names round-trips verbatim.

    This is the supported way for spec authors to grant the helper
    access to specific credentials the agent legitimately uses
    (e.g. an ``AWS_PROFILE`` to pick the right account, a
    ``GITHUB_TOKEN`` for git operations).
    """
    config = {
        "spec_version": 1,
        "name": "with-passthrough",
        "os_env": {
            "type": "caller_process",
            "sandbox": {
                "type": "linux_bwrap",
                "env_passthrough": ["AWS_PROFILE", "GITHUB_TOKEN", "DATABRICKS_HOST"],
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.env_passthrough == [
        "AWS_PROFILE",
        "GITHUB_TOKEN",
        "DATABRICKS_HOST",
    ]


def test_parse_os_env_sandbox_env_passthrough_empty_list_preserved(
    tmp_path: Path,
) -> None:
    """
    An explicit empty list parses to ``[]``, distinct from ``None``.

    The helper spawn path treats both as "only defaults" today, but
    keeping them distinct preserves the option to change one of them
    (e.g. add a future "warn if user wrote an empty list explicitly"
    diagnostic).
    """
    config = {
        "spec_version": 1,
        "name": "empty-passthrough",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap", "env_passthrough": []},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None and spec.os_env.sandbox is not None
    assert spec.os_env.sandbox.env_passthrough == []


@pytest.mark.parametrize(
    "bad_value,match_regex",
    [
        ("AWS_PROFILE", r"must be a list"),
        (["AWS_PROFILE", 7], r"must be strings"),
        (["AWS_PROFILE", ""], r"must be POSIX environment"),
        (["1AWS_PROFILE"], r"must be POSIX environment"),
        (["AWS PROFILE"], r"must be POSIX environment"),
        (["AWS-PROFILE"], r"must be POSIX environment"),
        (["AWS=PROFILE"], r"must be POSIX environment"),
        (["AWS/PROFILE"], r"must be POSIX environment"),
    ],
    ids=[
        "scalar_string",
        "non_string_entry",
        "empty_string",
        "starts_with_digit",
        "contains_space",
        "contains_dash",
        "contains_equals",
        "contains_slash",
    ],
)
def test_parse_os_env_sandbox_env_passthrough_validation(
    tmp_path: Path, bad_value: object, match_regex: str
) -> None:
    """
    Invalid ``env_passthrough`` values raise :class:`OmnigentError`
    at parse time with a message that names the field and the rule
    the entry violated.

    The POSIX env-var name regex is the only thing standing between a
    misconfigured spec and a name like ``AWS=secret`` smuggling a
    *value* through the *name* slot of ``os.execve``. Fail loud here.
    """
    config = {
        "spec_version": 1,
        "name": "bad-passthrough",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap", "env_passthrough": bad_value},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=match_regex):
        parse(tmp_path)


# ---------------------------------------------------------------------------
# os_env.start_in_scratch
# ---------------------------------------------------------------------------


def test_parse_os_env_start_in_scratch_defaults_false(tmp_path: Path) -> None:
    """
    Omitting ``start_in_scratch`` parses to ``False`` so existing
    specs keep the long-standing behaviour of starting the helper in
    cwd. Pinning the default here ensures a future YAML-shape tweak
    (e.g. flipping the default) is caught at review time rather than
    silently rerouting every agent's working directory into scratch.
    """
    config = {
        "spec_version": 1,
        "name": "no-start-in-scratch",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None
    assert spec.os_env.start_in_scratch is False


def test_parse_os_env_start_in_scratch_true(tmp_path: Path) -> None:
    """
    Setting ``start_in_scratch: true`` together with an active
    sandbox parses successfully and threads through to the spec.
    """
    config = {
        "spec_version": 1,
        "name": "scratch-cwd",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap"},
            "start_in_scratch": True,
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.os_env is not None
    assert spec.os_env.start_in_scratch is True


def test_parse_os_env_start_in_scratch_with_fork_rejected(tmp_path: Path) -> None:
    """
    ``start_in_scratch`` and ``fork`` both manage the agent's writable
    workspace and would fight each other, so the parser rejects the
    combination at spec-load time.

    What breaks if this fails: a misconfigured spec with both knobs
    set ships a helper whose effective cwd silently depends on which
    setup step ran last — exactly the kind of "where did my files
    go?" footgun the explicit error is meant to prevent.
    """
    config = {
        "spec_version": 1,
        "name": "scratch-and-fork",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "linux_bwrap"},
            "fork": True,
            "start_in_scratch": True,
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"mutually exclusive"):
        parse(tmp_path)


def test_parse_os_env_start_in_scratch_with_sandbox_none_rejected(
    tmp_path: Path,
) -> None:
    """
    ``start_in_scratch`` requires an active sandbox because the
    scratch tmpdir is created by the sandbox layer. Asking for
    ``sandbox.type: none`` together with ``start_in_scratch: true``
    has no destination directory and is rejected at parse time so
    users see the misconfiguration immediately rather than at first
    tool call.
    """
    config = {
        "spec_version": 1,
        "name": "scratch-without-sandbox",
        "os_env": {
            "type": "caller_process",
            "sandbox": {"type": "none"},
            "start_in_scratch": True,
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"requires an active sandbox"):
        parse(tmp_path)


def test_executor_profile_field_lifted_from_yaml(tmp_path: Path) -> None:
    """
    Top-level ``executor.profile`` lifts into the concrete
    ``ExecutorSpec.profile`` field.

    For ``executor.type == "omnigent"`` the parser additionally
    mirrors the value into ``executor.config["profile"]`` so the
    legacy reader (which still consults ``config["profile"]``)
    keeps working until the omnigent-compat sunset lands.

    :param tmp_path: pytest-provided temporary directory.
    """
    config = {
        "spec_version": 1,
        "name": "agent",
        "executor": {
            "type": "omnigent",
            "profile": "dev",
            "config": {"harness": "claude-sdk"},
        },
        "llm": {"model": "databricks-claude-sonnet-4-6"},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    # Concrete field populated for every executor type.
    assert spec.executor.profile == "dev"
    # Back-compat mirror into config["profile"] for omnigent.
    # Without this the legacy omnigent executor (which still
    # reads config["profile"]) would silently fall back to env
    # vars / DEFAULT section.
    assert spec.executor.config.get("profile") == "dev"


def test_executor_profile_field_lifted_for_non_omnigent(tmp_path: Path) -> None:
    """``executor.profile`` lifts into ``ExecutorSpec.profile`` for all executor types."""
    config = {
        "spec_version": 1,
        "name": "agent",
        "executor": {"type": "claude_sdk", "profile": "prod"},
        "llm": {"model": "databricks-claude-sonnet-4-6"},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    assert spec.executor.profile == "prod"
    assert "profile" not in spec.executor.config


def test_omnigent_and_default_executor_minimal_configs_still_parse(tmp_path: Path) -> None:
    """Both legacy ``omnigent`` and default minimal YAMLs continue to parse cleanly."""
    omni_config = {
        "spec_version": 1,
        "name": "omni-agent",
        "executor": {
            "type": "omnigent",
            "config": {"harness": "claude-sdk"},
        },
        "llm": {"model": "databricks-claude-sonnet-4-6"},
    }
    omni_dir = tmp_path / "omni"
    omni_dir.mkdir()
    (omni_dir / "config.yaml").write_text(yaml.dump(omni_config))
    omni_spec = parse(omni_dir)
    assert omni_spec.executor.type == "omnigent"
    assert omni_spec.executor.config.get("harness") == "claude-sdk"

    llm_config = {
        "spec_version": 1,
        "name": "llm-agent",
        "llm": {
            "model": "openai/gpt-4o",
            "connection": {"api_key": "sk-test"},
        },
    }
    llm_dir = tmp_path / "llm"
    llm_dir.mkdir()
    (llm_dir / "config.yaml").write_text(yaml.dump(llm_config))
    llm_spec = parse(llm_dir)
    assert llm_spec.executor.type == "omnigent"
    assert llm_spec.executor.profile is None


# ---------------------------------------------------------------------------
# executor.auth parsing
# ---------------------------------------------------------------------------


def test_parse_executor_auth_databricks(tmp_path: Path) -> None:
    """
    ``executor.auth: {type: databricks, profile: oss}`` parses into
    :class:`DatabricksAuth`.

    Failure means Databricks profile auth from the spec is silently
    dropped and the harness falls back to env-var auth, which makes
    the spec non-self-contained.
    """
    config = {
        "spec_version": 1,
        "executor": {
            "harness": "openai-agents",
            "model": "databricks-gpt-5-4-mini",
            "auth": {"type": "databricks", "profile": "oss"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # auth field must be populated with a DatabricksAuth instance.
    assert isinstance(spec.executor.auth, DatabricksAuth)
    assert spec.executor.auth.profile == "oss"


def test_parse_executor_auth_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``executor.auth: {type: api_key, api_key: $MY_KEY}`` expands the
    env-var reference and parses into :class:`ApiKeyAuth`.

    Failure means the api_key is not expanded at parse time and the
    executor receives a literal ``$MY_KEY`` string instead of the real
    key value.
    """
    monkeypatch.setenv("MY_KEY", "sk-test-123")
    config = {
        "spec_version": 1,
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o",
            "auth": {"type": "api_key", "api_key": "$MY_KEY"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # auth must be ApiKeyAuth with the resolved key value.
    assert isinstance(spec.executor.auth, ApiKeyAuth)
    assert spec.executor.auth.api_key == "sk-test-123"


def test_parse_executor_auth_provider(tmp_path: Path) -> None:
    """
    ``executor.auth: {type: provider, name: litellm}`` parses into
    :class:`ProviderAuth` carrying the provider name.

    Failure means a spec referencing a named generic provider is dropped
    and the harness never routes through that provider's endpoint.
    """
    config = {
        "spec_version": 1,
        "executor": {
            "harness": "codex",
            "auth": {"type": "provider", "name": "litellm"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # auth must be a ProviderAuth carrying the provider name verbatim.
    # If this returned ApiKeyAuth/None instead, the provider branch in
    # _parse_executor_auth was not taken and routing would fall back.
    assert isinstance(spec.executor.auth, ProviderAuth)
    assert spec.executor.auth.name == "litellm"
    assert spec.executor.auth.type == "provider"


def test_parse_executor_auth_provider_missing_name_raises(tmp_path: Path) -> None:
    """``type: provider`` without a ``name`` fails loud, not silently empty."""
    config = {
        "spec_version": 1,
        "executor": {"auth": {"type": "provider"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"executor.auth.name is required"):
        parse(tmp_path)


def test_parse_executor_auth_absent(tmp_path: Path) -> None:
    """No ``auth:`` key yields ``spec.executor.auth is None``."""
    config = {
        "spec_version": 1,
        "executor": {"harness": "openai-agents", "model": "gpt-4o"},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    # auth must be None — harness falls back to env-var / profile defaults.
    assert spec.executor.auth is None


def test_parse_executor_auth_unknown_type_raises(tmp_path: Path) -> None:
    """An unknown ``auth.type`` value raises :class:`OmnigentError`."""
    config = {
        "spec_version": 1,
        "executor": {"auth": {"type": "magic_token", "token": "abc"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"must be 'api_key', 'databricks', or 'provider'"):
        parse(tmp_path)


def test_parse_executor_auth_api_key_missing_key_raises(tmp_path: Path) -> None:
    """
    ``type: api_key`` without an ``api_key`` field raises
    :class:`OmnigentError` rather than producing an empty key.
    """
    config = {
        "spec_version": 1,
        "executor": {"auth": {"type": "api_key"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"api_key is required"):
        parse(tmp_path)


def test_parse_executor_auth_databricks_missing_profile_raises(tmp_path: Path) -> None:
    """
    ``type: databricks`` without a ``profile`` field raises
    :class:`OmnigentError` rather than silently using an empty profile.
    """
    config = {
        "spec_version": 1,
        "executor": {"auth": {"type": "databricks"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"profile is required"):
        parse(tmp_path)


def test_parse_executor_auth_api_key_with_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``executor.auth: {type: api_key, api_key: …, base_url: …}`` parses
    both fields and expands env-var references in ``base_url``.

    Failure means a custom endpoint declared alongside an API key is
    silently dropped, routing requests to the default OpenAI endpoint.
    """
    monkeypatch.setenv("MY_KEY", "sk-test-456")
    monkeypatch.setenv("MY_BASE", "https://gw.example.com/v1")
    config = {
        "spec_version": 1,
        "executor": {
            "harness": "openai-agents",
            "auth": {"type": "api_key", "api_key": "$MY_KEY", "base_url": "$MY_BASE"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    assert isinstance(spec.executor.auth, ApiKeyAuth)
    assert spec.executor.auth.api_key == "sk-test-456"
    assert spec.executor.auth.base_url == "https://gw.example.com/v1"


def test_parse_executor_auth_api_key_base_url_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``base_url`` defaults to ``None`` when not declared."""
    monkeypatch.setenv("MY_KEY", "sk-no-url")
    config = {
        "spec_version": 1,
        "executor": {
            "harness": "openai-agents",
            "auth": {"type": "api_key", "api_key": "$MY_KEY"},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)

    assert isinstance(spec.executor.auth, ApiKeyAuth)
    assert spec.executor.auth.base_url is None


# ── credential_proxy parser tests ─────────────────────────────────


def _credential_proxy_config(entries: list[dict[str, object]]) -> dict[str, object]:
    """
    Build a minimal agent config carrying a ``credential_proxy`` block.

    :param entries: The ``credential_proxy`` list to embed under
        ``os_env.sandbox``.
    :returns: A config dict ready to ``yaml.dump`` for :func:`parse`.
    """
    return {
        "spec_version": 1,
        "name": "cred-proxy",
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {
                "type": "linux_bwrap",
                "egress_rules": [
                    "* github.com/**",
                    "* api.github.com/**",
                    "* corp.example.com/**",
                    "* git.example.com/**",
                    "* bearer.example.com/**",
                    "* basic.example.com/**",
                ],
                "credential_proxy": entries,
            },
        },
    }


def test_parse_credential_proxy_all_four_types(tmp_path: Path) -> None:
    """All four ``credential_proxy`` types normalize to host bindings.

    What breaks if this fails: the YAML the user writes wouldn't reach
    the runtime as the right per-host scheme/injection, so the egress
    proxy wouldn't swap credentials (or would swap the wrong scheme).
    """
    config = _credential_proxy_config(
        [
            {"type": "gh_basic", "source": {"env": "GH_PAT"}},
            {
                "type": "git_https",
                "target": "git.example.com/org/repo.git",
                "source": {"env": "GH_PAT"},
            },
            {
                "type": "https_bearer",
                "target": "bearer.example.com/rest",
                "source": {"env": "CORP"},
                "env": "CORP_TOKEN",
            },
            {
                "type": "https_basic",
                "targets": ["basic.example.com"],
                "source": {"file": "/tmp/secret"},
                "username": "svc",
            },
        ]
    )
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    proxy = spec.os_env.sandbox.credential_proxy
    assert proxy is not None
    by = {(e.host, e.scheme): e for e in proxy.entries}

    # gh_basic -> github.com (basic, swap-on-access) and api.github.com
    # (token + GH_TOKEN/GITHUB_TOKEN injection because gh gates locally).
    gh_git = by[("github.com", "basic")]
    assert gh_git.inject_env == []  # swap-on-access: nothing injected for git
    gh_api = by[("api.github.com", "token")]
    assert gh_api.inject_env == ["GH_TOKEN", "GITHUB_TOKEN"]

    # git_https -> swap-on-access Basic for its bound host (nothing injected).
    git_https = by[("git.example.com", "basic")]
    assert git_https.inject_env == []

    # https_bearer with an explicit ``env`` opts into placeholder injection.
    bearer = by[("bearer.example.com", "bearer")]
    assert bearer.inject_env == ["CORP_TOKEN"]
    assert bearer.source.kind == "env" and bearer.source.env == "CORP"

    # https_basic keeps the explicit username and a file source; with no
    # ``env`` it is pure swap-on-access (inject_env empty).
    basic = by[("basic.example.com", "basic")]
    assert basic.username == "svc"
    assert basic.inject_env == []
    assert basic.source.kind == "file" and basic.source.path == "/tmp/secret"


def test_parse_credential_proxy_rejects_duplicate_host(tmp_path: Path) -> None:
    """Two entries binding the same host fail loudly at parse time.

    The egress proxy keys its swap-on-access table by host, so a
    duplicate-host config would silently drop one credential (last
    wins). Rejecting it up front prevents a nondeterministic,
    hard-to-debug "wrong scheme on the wire" outcome. Here ``gh_basic``
    already binds ``github.com`` and the explicit ``git_https`` binds it
    again.
    """
    config = _credential_proxy_config(
        [
            {"type": "gh_basic", "source": {"env": "GH_PAT"}},
            {"type": "git_https", "target": "github.com", "source": {"env": "GH_PAT"}},
        ]
    )
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"binds host 'github.com' more than once"):
        parse(tmp_path)


def test_parse_credential_proxy_git_https_default_username(tmp_path: Path) -> None:
    """``git_https`` defaults the Basic username to ``x-access-token``.

    A wrong default would make GitHub reject the Basic auth even though
    the token is valid.
    """
    config = _credential_proxy_config(
        [{"type": "git_https", "target": "github.com", "source": {"env": "GH_PAT"}}]
    )
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    entry = spec.os_env.sandbox.credential_proxy.entries[0]
    assert entry.username == "x-access-token"


def test_parse_credential_proxy_https_env_optional(tmp_path: Path) -> None:
    """``https_*`` without ``env`` parses as a swap-on-access binding.

    The ``env`` field is the opt-in injection shim, not a requirement.
    Omitting it must yield a valid entry with an empty ``inject_env`` so
    the proxy attaches the credential on access. If ``env`` were still
    treated as required, parsing would raise instead.
    """
    config = _credential_proxy_config(
        [{"type": "https_bearer", "target": "corp.example.com", "source": {"env": "CORP"}}]
    )
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    entry = spec.os_env.sandbox.credential_proxy.entries[0]
    assert entry.host == "corp.example.com"
    assert entry.scheme == "bearer"
    assert entry.inject_env == []


@pytest.mark.parametrize(
    "entries,match",
    [
        # Unknown ``type`` — caught by the pydantic ``Literal``.
        ([{"type": "bogus", "source": {"env": "X"}}], r"type: Input should be"),
        # Missing ``source`` — pydantic ``Field required``.
        ([{"type": "https_bearer", "target": "h.example.com"}], r"source: Field required"),
        # ``source`` as a bare string (the old surface) is now rejected —
        # it must be a nested ``{env|file|command: ...}`` mapping.
        (
            [{"type": "https_bearer", "target": "h.example.com", "source": "env:X", "env": "T"}],
            r"source:.*valid dictionary",
        ),
        # Two source keys set — exactly one is allowed.
        (
            [
                {
                    "type": "https_bearer",
                    "target": "h.example.com",
                    "source": {"env": "X", "file": "/tmp/s"},
                }
            ],
            r"exactly one of 'env', 'file', or 'command'",
        ),
        # Malformed ``env`` injection-shim name.
        (
            [
                {
                    "type": "https_bearer",
                    "target": "h.example.com",
                    "source": {"env": "X"},
                    "env": "not a valid name",
                }
            ],
            r"env must be a POSIX",
        ),
        # Both ``target`` and ``targets`` set.
        (
            [
                {
                    "type": "https_bearer",
                    "target": "h.example.com",
                    "targets": ["h2.example.com"],
                    "source": {"env": "X"},
                    "env": "T",
                }
            ],
            r"exactly one of 'target' or 'targets'",
        ),
        # Host fails DNS-safety validation (still enforced post-pydantic).
        (
            [{"type": "git_https", "target": "bad_host!", "source": {"env": "X"}}],
            r"must be an exact DNS hostname",
        ),
        # Unknown key — ``extra="forbid"`` rejects typos.
        (
            [
                {
                    "type": "https_bearer",
                    "target": "h.example.com",
                    "source": {"env": "X"},
                    "bogus": 1,
                }
            ],
            r"bogus: Extra inputs are not permitted",
        ),
    ],
)
def test_parse_credential_proxy_fail_loud(
    tmp_path: Path, entries: list[dict[str, object]], match: str
) -> None:
    """Malformed ``credential_proxy`` entries fail loudly at parse time.

    Each case proves a specific misconfiguration is rejected up front
    rather than silently producing a half-wired (insecure) policy.
    """
    config = _credential_proxy_config(entries)
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=match):
        parse(tmp_path)


def test_parse_credential_proxy_requires_egress_rules(tmp_path: Path) -> None:
    """``credential_proxy`` without ``egress_rules`` is rejected.

    The MITM proxy (driven by egress_rules) is what performs the swap and
    blocks placeholder leaks; without it the feature would be a no-op that
    injects placeholders the agent can't use — fail loud instead.
    """
    config = {
        "spec_version": 1,
        "name": "cred-proxy-no-egress",
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {
                "type": "linux_bwrap",
                "credential_proxy": [
                    {"type": "git_https", "target": "github.com", "source": {"env": "X"}}
                ],
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"requires os_env.sandbox.egress_rules"):
        parse(tmp_path)


def test_parse_credential_proxy_requires_hard_backend(tmp_path: Path) -> None:
    """``credential_proxy`` requires a network-isolating backend.

    On ``linux_landlock`` (no hard network deny) the egress proxy isn't
    the only path out, so binding credentials there is unsafe — rejected.

    We deliberately OMIT ``egress_rules`` here so the egress-rules backend
    guard doesn't fire first: that isolates the credential_proxy-specific
    backend check (parser.py:1117). The ``match`` asserts the
    credential_proxy message, not the egress one — so deleting the
    credential_proxy backend guard (falling through to the
    "requires egress_rules" error with its different text) would fail
    this test.
    """
    config = {
        "spec_version": 1,
        "name": "cred-proxy-soft-backend",
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {
                "type": "linux_landlock",
                "credential_proxy": [
                    {"type": "git_https", "target": "github.com", "source": {"env": "X"}}
                ],
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"credential_proxy requires sandbox.type"):
        parse(tmp_path)


def test_parse_credential_proxy_gh_basic_rejected_on_macos(tmp_path: Path) -> None:
    """``gh_basic`` is rejected on macOS (``darwin_seatbelt``).

    ``gh_basic`` wires the GitHub CLI, a Go binary, and Go on macOS verifies
    TLS via the system keychain and ignores ``SSL_CERT_FILE`` — the var the
    egress MITM proxy uses to publish its CA — so every ``gh`` call would fail
    at runtime with an opaque ``certificate is not trusted`` error. We fail
    loud at parse time instead. The ``match`` asserts the macOS-specific
    message (not the backend/egress guards, which pass here since
    ``darwin_seatbelt`` + ``egress_rules`` are both present), so removing the
    macOS guard would let the spec parse and fail this test.
    """
    config = {
        "spec_version": 1,
        "name": "cred-proxy-gh-macos",
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {
                "type": "darwin_seatbelt",
                "egress_rules": ["* github.com/**", "* api.github.com/**"],
                "credential_proxy": [{"type": "gh_basic", "source": {"env": "GH_PAT"}}],
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"gh_basic' does not work on macOS"):
        parse(tmp_path)


def test_parse_credential_proxy_https_primitive_allowed_on_macos(tmp_path: Path) -> None:
    """The generic primitives are NOT rejected on macOS.

    The macOS guard must fire ONLY for the Go-based ``gh_basic`` preset (the
    ``token`` scheme). ``https_bearer`` (and ``https_basic`` / ``git_https``)
    are consumed by curl/python/git, which trust ``SSL_CERT_FILE`` on macOS, so
    they must still parse on ``darwin_seatbelt``. This guards against the guard
    being too broad and breaking the primitives that DO work.
    """
    config = {
        "spec_version": 1,
        "name": "cred-proxy-bearer-macos",
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {
                "type": "darwin_seatbelt",
                "egress_rules": ["* corp.example.com/**"],
                "credential_proxy": [
                    {
                        "type": "https_bearer",
                        "target": "corp.example.com/rest",
                        "source": {"env": "CORP"},
                        "env": "CORP_TOKEN",
                    }
                ],
            },
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    spec = parse(tmp_path)
    proxy = spec.os_env.sandbox.credential_proxy
    assert proxy is not None
    assert proxy.entries[0].scheme == "bearer"
