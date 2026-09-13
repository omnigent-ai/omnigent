from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.onboarding.acp_auth import AcpAgentEntry, acp_agents_settings
from omnigent.onboarding.openclaw_config import (
    discover_openclaw_agents,
    merge_imported_acp_entries,
    openclaw_agents_to_acp_entries,
    read_openclaw_config,
    resolve_openclaw_acp_agent,
)


def test_discovers_acpx_config(tmp_path: Path) -> None:
    acpx = tmp_path / "config.json"
    acpx.write_text(
        """
        {
          "agents": {
            "Gemini CLI": {"command": "gemini", "args": ["--experimental-acp"]},
            "Broken": {"args": ["missing-command"]}
          }
        }
        """,
        encoding="utf-8",
    )

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=tmp_path / "missing.json")

    assert discovery.errors == ()
    assert [(agent.name, agent.command_line, agent.source) for agent in discovery.agents] == [
        ("Gemini CLI", "gemini --experimental-acp", "acpx")
    ]


def test_discovers_openclaw_wrapped_json5_config(tmp_path: Path) -> None:
    openclaw = tmp_path / "openclaw.json"
    openclaw.write_text(
        """
        {
          // OpenClaw wraps the acpx registry under plugins.entries.acpx.config.
          plugins: {
            entries: {
              acpx: {
                config: {
                  agents: {
                    'Claude Code': {
                      command: 'npx',
                      args: ['-y', '@zed-industries/claude-code-acp'],
                    },
                  },
                },
              },
            },
          },
        }
        """,
        encoding="utf-8",
    )

    discovery = discover_openclaw_agents(
        acpx_path=tmp_path / "missing.json", openclaw_path=openclaw
    )

    assert discovery.errors == ()
    assert [(agent.name, agent.command_line, agent.source) for agent in discovery.agents] == [
        ("Claude Code", "npx -y @zed-industries/claude-code-acp", "openclaw")
    ]


def test_reads_selected_acpx_config_by_shape(tmp_path: Path) -> None:
    config = tmp_path / "agents.json"
    config.write_text(
        '{"agents": {"Gemini CLI": {"command": "gemini", "args": ["--acp"]}}}',
        encoding="utf-8",
    )

    discovery = read_openclaw_config(config)

    assert discovery.errors == ()
    assert [(agent.name, agent.command_line, agent.source) for agent in discovery.agents] == [
        ("Gemini CLI", "gemini --acp", "acpx")
    ]


def test_acpx_json5_parses_identically_via_discovery_and_selected_path(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        """
        {
          // acpx registries use the same tolerant parser as wrapped OpenClaw.
          agents: {
            "Gemini CLI": {command: "gemini", args: ["--acp"],},
          },
        }
        """,
        encoding="utf-8",
    )

    discovered = discover_openclaw_agents(
        acpx_path=config, openclaw_path=tmp_path / "missing.json"
    )
    selected = read_openclaw_config(config)

    assert discovered.errors == selected.errors == ()
    assert discovered.agents == selected.agents


def test_reads_selected_openclaw_config_by_shape(tmp_path: Path) -> None:
    config = tmp_path / "registry.json"
    config.write_text(
        "{plugins: {entries: {acpx: {config: {agents: "
        '{Goose: {command: "goose", args: ["acp"]}}}}}}}',
        encoding="utf-8",
    )

    discovery = read_openclaw_config(config)

    assert discovery.errors == ()
    assert [(agent.name, agent.command_line, agent.source) for agent in discovery.agents] == [
        ("Goose", "goose acp", "openclaw")
    ]


def test_selected_unrelated_file_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "package.json"
    config.write_text('{"name": "not-an-agent-registry"}', encoding="utf-8")

    discovery = read_openclaw_config(config)

    assert discovery.agents == ()
    assert len(discovery.errors) == 1
    assert discovery.errors[0].message == "file is not an OpenClaw/acpx agent registry"


def test_selected_missing_file_is_soft_error(tmp_path: Path) -> None:
    config = tmp_path / "missing.json"

    discovery = read_openclaw_config(config)

    assert discovery.agents == ()
    assert len(discovery.errors) == 1
    assert discovery.errors[0].path == config


def test_discovers_both_config_shapes(tmp_path: Path) -> None:
    acpx = tmp_path / "acpx.json"
    openclaw = tmp_path / "openclaw.json"
    acpx.write_text(
        '{"agents": {"Qwen Code": {"command": "qwen", "args": ["--acp"]}}}',
        encoding="utf-8",
    )
    openclaw.write_text(
        "{plugins: {entries: {acpx: {config: {agents: "
        '{Goose: {command: "goose", args: ["acp"]}}}}}}}',
        encoding="utf-8",
    )

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=openclaw)

    assert discovery.errors == ()
    assert [(agent.name, agent.command_line) for agent in discovery.agents] == [
        ("Qwen Code", "qwen --acp"),
        ("Goose", "goose acp"),
    ]


def test_translator_deduplicates_same_agent_across_sources(tmp_path: Path) -> None:
    acpx = tmp_path / "acpx.json"
    openclaw = tmp_path / "openclaw.json"
    acpx.write_text(
        '{"agents": {"Gemini CLI": {"command": "gemini", "args": ["--experimental-acp"]}}}',
        encoding="utf-8",
    )
    openclaw.write_text(
        "{plugins: {entries: {acpx: {config: {agents: "
        '{"Gemini CLI": {command: "gemini", args: ["--experimental-acp"]}}}}}}}',
        encoding="utf-8",
    )

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=openclaw)
    entries = openclaw_agents_to_acp_entries(discovery.agents)

    assert entries == [
        AcpAgentEntry(slug="gemini-cli", name="Gemini CLI", command="gemini --experimental-acp")
    ]


def test_openclaw_json5_supports_escaped_apostrophes(tmp_path: Path) -> None:
    openclaw = tmp_path / "openclaw.json"
    openclaw.write_text(
        r"""
        {plugins: {entries: {acpx: {config: {agents: {
          Helper: {command: 'helper', args: ['it\'s-valid']},
        }}}}}}
        """,
        encoding="utf-8",
    )

    discovery = discover_openclaw_agents(
        acpx_path=tmp_path / "missing.json", openclaw_path=openclaw
    )

    assert discovery.errors == ()
    assert discovery.agents[0].args == ("it's-valid",)


def test_agent_args_are_shell_quoted(tmp_path: Path) -> None:
    acpx = tmp_path / "config.json"
    acpx.write_text(
        '{"agents": {"Helper": {"command": "helper", "args": ["a b", "--flag=v;rm"]}}}',
        encoding="utf-8",
    )

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=tmp_path / "missing.json")

    assert discovery.errors == ()
    assert discovery.agents[0].command_line == "helper 'a b' '--flag=v;rm'"


def test_command_path_with_spaces_is_shell_quoted(tmp_path: Path) -> None:
    acpx = tmp_path / "config.json"
    acpx.write_text(
        '{"agents": {"Helper": {"command": "/Applications/My App/bin/helper", '
        '"args": ["--acp"]}}}',
        encoding="utf-8",
    )

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=tmp_path / "missing.json")

    assert discovery.errors == ()
    assert discovery.agents[0].command_line == "'/Applications/My App/bin/helper' --acp"


def test_non_string_args_skip_only_malformed_agent(tmp_path: Path) -> None:
    openclaw = tmp_path / "openclaw.json"
    openclaw.write_text(
        "{plugins: {entries: {acpx: {config: {agents: "
        '{Broken: {command: "broken", args: [1.20]}, '
        'Helper: {command: "helper", args: ["--acp"]}}}}}}}',
        encoding="utf-8",
    )

    discovery = discover_openclaw_agents(
        acpx_path=tmp_path / "missing.json", openclaw_path=openclaw
    )

    assert [(agent.name, agent.command_line) for agent in discovery.agents] == [
        ("Helper", "helper --acp")
    ]
    assert len(discovery.errors) == 1
    assert "args must be a string or list of strings" in discovery.errors[0].message


def test_malformed_config_is_soft_error(tmp_path: Path) -> None:
    acpx = tmp_path / "config.json"
    acpx.write_text('{"agents": ', encoding="utf-8")

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=tmp_path / "missing.json")

    assert discovery.agents == ()
    assert len(discovery.errors) == 1
    assert discovery.errors[0].path == acpx
    assert "invalid JSON" in discovery.errors[0].message


def test_config_path_directory_is_soft_error(tmp_path: Path) -> None:
    acpx = tmp_path / "config.json"
    acpx.mkdir()

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=tmp_path / "missing.json")

    assert discovery.agents == ()
    assert len(discovery.errors) == 1
    assert discovery.errors[0].path == acpx


def test_unreadable_config_is_soft_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acpx = tmp_path / "config.json"
    acpx.write_text("{}", encoding="utf-8")

    def deny_read(*args: object, **kwargs: object) -> str:
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "read_text", deny_read)

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=tmp_path / "missing.json")

    assert discovery.agents == ()
    assert len(discovery.errors) == 1
    assert discovery.errors[0].message == "permission denied"


def test_deeply_nested_json5_is_soft_error(tmp_path: Path) -> None:
    openclaw = tmp_path / "openclaw.json"
    openclaw.write_text("[" * 100 + "]" * 100, encoding="utf-8")

    discovery = discover_openclaw_agents(
        acpx_path=tmp_path / "missing.json", openclaw_path=openclaw
    )

    assert discovery.agents == ()
    assert len(discovery.errors) == 1
    assert discovery.errors[0].message == "invalid JSON5: nesting is too deep"


def test_empty_configs_return_no_agents(tmp_path: Path) -> None:
    acpx = tmp_path / "config.json"
    openclaw = tmp_path / "openclaw.json"
    acpx.write_text('{"agents": {}}', encoding="utf-8")
    openclaw.write_text("{plugins: {entries: {acpx: {config: {}}}}}", encoding="utf-8")

    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=openclaw)

    assert discovery.agents == ()
    assert discovery.errors == ()


def test_translator_emits_acp_agents_settings(tmp_path: Path) -> None:
    acpx = tmp_path / "config.json"
    acpx.write_text(
        '{"agents": {"Gemini CLI": {"command": "gemini", "args": ["--experimental-acp"]}}}',
        encoding="utf-8",
    )

    entries = openclaw_agents_to_acp_entries(
        discover_openclaw_agents(acpx_path=acpx, openclaw_path=tmp_path / "missing.json").agents
    )

    assert entries == [
        AcpAgentEntry(slug="gemini-cli", name="Gemini CLI", command="gemini --experimental-acp")
    ]
    assert acp_agents_settings(entries) == {
        "acp": {"agents": [{"name": "Gemini CLI", "command": "gemini --experimental-acp"}]}
    }


def test_resolves_single_agent_by_name_or_slug(tmp_path: Path) -> None:
    acpx = tmp_path / "config.json"
    acpx.write_text(
        '{"agents": {"Gemini CLI": {"command": "gemini", "args": ["--experimental-acp"]}}}',
        encoding="utf-8",
    )
    discovery = discover_openclaw_agents(acpx_path=acpx, openclaw_path=tmp_path / "missing.json")

    by_name = resolve_openclaw_acp_agent("gemini cli", discovery=discovery)
    by_slug = resolve_openclaw_acp_agent("gemini-cli", discovery=discovery)

    expected = AcpAgentEntry(
        slug="gemini-cli", name="Gemini CLI", command="gemini --experimental-acp"
    )
    assert by_name == expected
    assert by_slug == expected


def test_import_merge_is_idempotent() -> None:
    imported = [
        AcpAgentEntry(slug="gemini-cli", name="Gemini CLI", command="gemini --experimental-acp")
    ]

    merged, added = merge_imported_acp_entries(imported, existing=[])
    rerun_merged, rerun_added = merge_imported_acp_entries(imported, existing=merged)

    assert added == imported
    assert rerun_added == []
    assert rerun_merged == merged


def test_import_merge_suffixes_preexisting_slug_collision() -> None:
    existing = [AcpAgentEntry(slug="gemini-cli", name="Gemini CLI", command="custom-gemini --acp")]
    imported = [
        AcpAgentEntry(slug="gemini-cli", name="Gemini CLI", command="gemini --experimental-acp")
    ]

    merged, added = merge_imported_acp_entries(imported, existing=existing)
    rerun_merged, rerun_added = merge_imported_acp_entries(imported, existing=merged)

    expected = AcpAgentEntry(
        slug="gemini-cli-2", name="Gemini CLI", command="gemini --experimental-acp"
    )
    assert added == [expected]
    assert merged == [*existing, expected]
    assert rerun_added == []
    assert rerun_merged == merged
