"""Managed Pi agent dirs receive the host's MCP servers without touching ``~/.pi/agent``."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import pytest

from omnigent.host import mcp_inventory
from omnigent.host.mcp_inventory import ConfiguredMcpServer
from omnigent.inner import pi_mcp
from omnigent.inner.pi_mcp import merge_pi_mcp_config, pi_mcp_servers_from_host
from omnigent.inner.pi_settings import prepare_managed_pi_agent_dir


def _server(
    name: str, harness: str, config: dict[str, object], plugin_root: Path | None = None
) -> ConfiguredMcpServer:
    return ConfiguredMcpServer(
        summary={"name": name, "harness": harness}, config=config, plugin_root=plugin_root
    )


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pi_host_mcp_servers: None) -> Path:
    """A temporary home whose Claude, Codex and Cursor configs feed the real inventory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(mcp_inventory, "_managed_mcp_paths", tuple)
    return home


@pytest.fixture
def pi_agent(home: Path) -> Path:
    agent = home / ".pi" / "agent"
    agent.mkdir(parents=True)
    return agent


def _prepare(home: Path, pi_agent: Path) -> Path:
    managed = home / "managed-agent"
    prepare_managed_pi_agent_dir(managed, global_agent_dir=pi_agent)
    return managed


def _managed_servers(managed: Path) -> dict[str, object]:
    return json.loads((managed / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]


# --- Translation -------------------------------------------------------------


def test_claude_stdio_keeps_secrets_as_pi_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Command and args resolve now; env values stay ``${NAME}`` for Pi to read at spawn."""
    monkeypatch.setenv("TOOL_HOME", "/opt/tool")
    monkeypatch.delenv("UNSET_LEVEL", raising=False)
    servers = pi_mcp_servers_from_host(
        [
            _server(
                "github",
                "claude",
                {
                    "command": "${TOOL_HOME}/bin/gh-mcp",
                    "args": ["--root", "${TOOL_HOME}"],
                    "env": {
                        "GITHUB_TOKEN": "${GITHUB_TOKEN}",
                        "LEVEL": "${UNSET_LEVEL:-info}",
                        "PRICE": "$5 !",
                        "SHELL_LIKE": "!rm -rf /",
                    },
                },
            )
        ]
    )

    assert servers == {
        "github": {
            "command": "/opt/tool/bin/gh-mcp",
            "args": ["--root", "/opt/tool"],
            "env": {
                "GITHUB_TOKEN": "${GITHUB_TOKEN}",
                "LEVEL": "info",
                "PRICE": "$$5 !",
                "SHELL_LIKE": "$!rm -rf /",
            },
        }
    }


def test_claude_default_yields_to_a_set_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEVEL", "debug")
    servers = pi_mcp_servers_from_host(
        [_server("tool", "claude", {"command": "tool", "env": {"LEVEL": "${LEVEL:-info}"}})]
    )
    assert servers["tool"]["env"] == {"LEVEL": "${LEVEL}"}


def test_claude_http_resolves_url_and_templates_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HOST", "mcp.example.com")
    servers = pi_mcp_servers_from_host(
        [
            _server(
                "remote",
                "claude",
                {
                    "type": "http",
                    "url": "https://${MCP_HOST}/mcp",
                    "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}"},
                    "oauth": {"clientId": "abc", "callbackPort": 8123, "unknown": "dropped"},
                },
            )
        ]
    )
    assert servers == {
        "remote": {
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}"},
            "oauth": {"clientId": "abc", "callbackPort": 8123},
        }
    }


def test_plugin_root_is_written_inline(tmp_path: Path) -> None:
    plugin_root = tmp_path / "plugins" / "figma"
    servers = pi_mcp_servers_from_host(
        [
            _server(
                "figma",
                "claude",
                {
                    "command": "${CLAUDE_PLUGIN_ROOT}/server",
                    "env": {"DATA": "${CLAUDE_PLUGIN_ROOT}/data"},
                },
                plugin_root=plugin_root,
            )
        ]
    )
    assert servers["figma"] == {
        "command": f"{plugin_root}/server",
        "env": {"DATA": f"{plugin_root}/data"},
    }


def test_cursor_env_and_home_substitutions(home: Path) -> None:
    servers = pi_mcp_servers_from_host(
        [
            _server(
                "linear",
                "cursor",
                {
                    "command": "${userHome}/bin/linear",
                    "env": {"TOKEN": "${env:LINEAR_TOKEN}", "CACHE": "${userHome}/.cache"},
                },
            )
        ]
    )
    assert servers["linear"] == {
        "command": f"{home}/bin/linear",
        "env": {"TOKEN": "${LINEAR_TOKEN}", "CACHE": f"{home}/.cache"},
    }


def test_codex_values_are_literal_and_options_carry_over(home: Path) -> None:
    servers = pi_mcp_servers_from_host(
        [
            _server(
                "docs",
                "codex",
                {
                    "command": "docs-mcp",
                    "args": ["--price", "$5"],
                    "env": {"MODE": "${NOT_A_TEMPLATE}"},
                    "cwd": "work",
                    "tool_timeout_sec": 30,
                    "enabled_tools": ["search"],
                    "disabled_tools": ["delete"],
                },
            ),
            _server(
                "api",
                "codex",
                {
                    "url": "https://api.example.com/mcp",
                    "http_headers": {"X-Team": "$core"},
                    "env_http_headers": {"X-Key": "API_KEY"},
                    "bearer_token_env_var": "API_TOKEN",
                },
            ),
        ]
    )
    assert servers == {
        "docs": {
            "command": "docs-mcp",
            "args": ["--price", "$5"],
            "env": {"MODE": "$${NOT_A_TEMPLATE}"},
            "cwd": str(home / "work"),
            "timeout": 30,
            "toolExposure": {"*": "hidden", "search": "codemode", "delete": "hidden"},
        },
        "api": {
            "url": "https://api.example.com/mcp",
            "headers": {
                "X-Team": "$$core",
                "X-Key": "${API_KEY}",
                "Authorization": "Bearer ${API_TOKEN}",
            },
        },
    }


@pytest.mark.parametrize(
    ("name", "harness", "config"),
    [
        ("events", "claude", {"type": "sse", "url": "https://sse.example.com"}),
        ("workspace", "cursor", {"command": "tool", "env": {"ROOT": "${workspaceFolder}"}}),
        ("bad name", "claude", {"command": "tool"}),
        ("missing", "claude", {"command": "${UNSET_PI_MCP_VARIABLE}"}),
        ("no-transport", "claude", {"env": {}}),
        ("bad-header", "codex", {"url": "https://x.example.com", "bearer_token_env_var": "a b"}),
    ],
)
def test_servers_pi_cannot_load_are_skipped(
    name: str, harness: str, config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UNSET_PI_MCP_VARIABLE", raising=False)
    assert pi_mcp_servers_from_host([_server(name, harness, config)]) == {}


def test_first_server_for_a_pi_name_wins() -> None:
    """Pi shares one namespace for names differing only in ``-``/``_``; a skipped one frees it."""
    servers = pi_mcp_servers_from_host(
        [
            _server("search_docs", "claude", {"type": "sse", "url": "https://sse.example.com"}),
            _server("search-docs", "codex", {"command": "codex-search"}),
            _server("search_docs", "cursor", {"command": "cursor-search"}),
        ]
    )
    assert servers == {"search-docs": {"command": "codex-search"}}


# --- Merging -----------------------------------------------------------------


def test_user_definition_wins_and_override_adjusts_host_server() -> None:
    user = {
        "settings": {"toolPrefix": "short"},
        "mcpServers": {
            "github": {"command": "my-github"},
            "linear": {"enabled": False},
            "docs": {"exposure": "direct", "toolExposure": {"delete_*": "hidden"}},
            "mine": {"url": "https://mine.example.com"},
        },
    }
    host = {
        "github": {"command": "host-github"},
        "linear": {"command": "host-linear"},
        "docs": {"command": "host-docs", "toolExposure": {"*": "codemode"}},
    }

    merged = merge_pi_mcp_config(user, host)

    assert merged == {
        "settings": {"toolPrefix": "short"},
        "mcpServers": {
            "linear": {"command": "host-linear", "enabled": False},
            "docs": {
                "command": "host-docs",
                "exposure": "direct",
                "toolExposure": {"delete_*": "hidden"},
            },
            "github": {"command": "my-github"},
            "mine": {"url": "https://mine.example.com"},
        },
    }


def test_merge_is_unneeded_when_the_user_defines_every_host_server() -> None:
    user = {"mcpServers": {"search_docs": {"command": "mine"}}}
    assert merge_pi_mcp_config(user, {"search-docs": {"command": "host"}}) is None
    assert merge_pi_mcp_config({}, {}) is None
    with pytest.raises(ValueError, match="mcpServers"):
        merge_pi_mcp_config({"mcpServers": []}, {"docs": {"command": "host"}})


# --- Managed agent dir -------------------------------------------------------


def test_missing_pi_config_gets_host_servers_from_every_harness(
    home: Path, pi_agent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    _write_json(home / ".claude.json", {"mcpServers": {"github": {"command": "gh-mcp"}}})
    (home / "codex").mkdir()
    (home / "codex" / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "docs-mcp"\n\n'
        '[mcp_servers.github]\ncommand = "codex-github"\n',
        encoding="utf-8",
    )
    _write_json(
        home / ".cursor" / "mcp.json",
        {"mcpServers": {"linear": {"url": "https://mcp.linear.app/mcp"}}},
    )

    managed = _prepare(home, pi_agent)

    target = managed / "mcp.json"
    assert not target.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _managed_servers(managed) == {
        "github": {"command": "gh-mcp"},
        "docs": {"command": "docs-mcp"},
        "linear": {"url": "https://mcp.linear.app/mcp"},
    }
    assert not (pi_agent / "mcp.json").exists()


def test_user_pi_config_is_merged_into_a_copy_and_left_untouched(
    home: Path, pi_agent: Path
) -> None:
    _write_json(
        home / ".claude.json",
        {"mcpServers": {"github": {"command": "host-github"}, "docs": {"command": "docs"}}},
    )
    user_file = pi_agent / "mcp.json"
    user_file.write_text(
        '{"mcpServers": {"github": {"command": "my-github"}, "docs": {"enabled": false}}}',
        encoding="utf-8",
    )
    before = user_file.read_bytes()

    managed = _prepare(home, pi_agent)

    assert _managed_servers(managed) == {
        "docs": {"command": "docs", "enabled": False},
        "github": {"command": "my-github"},
    }
    assert user_file.read_bytes() == before
    assert sorted(path.name for path in pi_agent.iterdir()) == ["mcp.json"]


def test_user_file_stays_linked_when_it_covers_the_host(home: Path, pi_agent: Path) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"github": {"command": "host"}}})
    _write_json(pi_agent / "mcp.json", {"mcpServers": {"github": {"command": "mine"}}})

    managed = _prepare(home, pi_agent)

    assert (managed / "mcp.json").is_symlink()
    assert (managed / "mcp.json").resolve() == (pi_agent / "mcp.json").resolve()


def test_reused_dir_regenerates_and_relinks(home: Path, pi_agent: Path) -> None:
    """A copy from an earlier launch is refreshed, then replaced by the link once unneeded."""
    claude = home / ".claude.json"
    _write_json(claude, {"mcpServers": {"github": {"command": "v1"}}})
    managed = _prepare(home, pi_agent)
    assert _managed_servers(managed) == {"github": {"command": "v1"}}

    _write_json(claude, {"mcpServers": {"github": {"command": "v2"}}})
    _prepare(home, pi_agent)
    assert _managed_servers(managed) == {"github": {"command": "v2"}}

    _write_json(pi_agent / "mcp.json", {"mcpServers": {"github": {"command": "mine"}}})
    _prepare(home, pi_agent)
    assert (managed / "mcp.json").resolve() == (pi_agent / "mcp.json").resolve()

    (pi_agent / "mcp.json").unlink()
    claude.unlink()
    _prepare(home, pi_agent)
    assert not (managed / "mcp.json").exists()
    assert not (managed / "mcp.json").is_symlink()


@pytest.mark.parametrize("host_has_servers", [False, True])
def test_session_owned_config_in_the_managed_dir_is_kept(
    home: Path, pi_agent: Path, host_has_servers: bool
) -> None:
    """A file Pi created there, or a generated copy edited in a session, survives a relaunch."""
    if host_has_servers:
        _write_json(home / ".claude.json", {"mcpServers": {"github": {"command": "host"}}})
    managed = _prepare(home, pi_agent)
    target = managed / "mcp.json"
    if host_has_servers:
        assert _managed_servers(managed) == {"github": {"command": "host"}}
    else:
        assert not target.exists()
    session_config = '{"mcpServers": {"added-in-pi": {"command": "mine"}}}'
    target.write_text(session_config, encoding="utf-8")

    _prepare(home, pi_agent)

    assert target.read_text(encoding="utf-8") == session_config
    assert not (pi_agent / "mcp.json").exists()


def test_undecodable_digest_keeps_the_managed_config(home: Path, pi_agent: Path) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"github": {"command": "host"}}})
    managed = _prepare(home, pi_agent)
    generated = (managed / "mcp.json").read_bytes()
    (managed / pi_mcp._GENERATED_DIGEST_NAME).write_bytes(b"\xff\xfe")

    _prepare(home, pi_agent)

    assert (managed / "mcp.json").read_bytes() == generated


def test_unreadable_user_config_is_linked_not_replaced(
    home: Path, pi_agent: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pi reports the user's own broken file instead of a copy that hides it."""
    _write_json(home / ".claude.json", {"mcpServers": {"github": {"command": "host"}}})
    user_file = pi_agent / "mcp.json"
    user_file.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="omnigent.inner.pi_mcp"):
        managed = _prepare(home, pi_agent)

    assert (managed / "mcp.json").resolve() == user_file.resolve()
    assert user_file.read_text(encoding="utf-8") == "{not json"
    assert "unreadable" in caplog.text


@pytest.mark.parametrize("failure", ["inventory", "write", "digest"])
def test_failed_refresh_does_not_block_launch_or_keep_a_stale_copy(
    home: Path, pi_agent: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A refresh that fails falls back to the user's file, so a server they disabled stays off."""
    _write_json(home / ".claude.json", {"mcpServers": {"github": {"command": "host"}}})
    managed = _prepare(home, pi_agent)
    assert _managed_servers(managed) == {"github": {"command": "host"}}
    user_file = pi_agent / "mcp.json"
    user_config = '{"mcpServers": {"github": {"enabled": false}}}'
    user_file.write_text(user_config, encoding="utf-8")

    write_private_file = pi_mcp._write_private_file

    def fail(path: Path | None = None, content: bytes = b"") -> None:
        if failure == "digest" and path != managed / pi_mcp._GENERATED_DIGEST_NAME:
            assert path is not None
            write_private_file(path, content)
            return
        raise OSError(f"{failure} failed")

    monkeypatch.setattr(
        pi_mcp, "_host_mcp_servers" if failure == "inventory" else "_write_private_file", fail
    )
    _prepare(home, pi_agent)

    assert (managed / "settings.json").exists()
    assert (managed / "mcp.json").resolve() == user_file.resolve()
    assert user_file.read_text(encoding="utf-8") == user_config


def test_generated_copy_never_writes_through_a_link(home: Path, pi_agent: Path) -> None:
    _write_json(pi_agent / "mcp.json", {"mcpServers": {}})
    managed = _prepare(home, pi_agent)
    assert (managed / "mcp.json").is_symlink()

    _write_json(home / ".claude.json", {"mcpServers": {"github": {"command": "host"}}})
    _prepare(home, pi_agent)

    assert not (managed / "mcp.json").is_symlink()
    assert json.loads((pi_agent / "mcp.json").read_text(encoding="utf-8")) == {"mcpServers": {}}
