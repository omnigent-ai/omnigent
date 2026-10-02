"""The host MCP inventory reports names and metadata, never secrets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.host import mcp_inventory
from omnigent.host.mcp_inventory import HostMcpInventory, discover_mcp_servers

_SECRET = "sk-live-secret-token"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _stdio(**extra: object) -> dict[str, object]:
    return {
        "type": "stdio",
        "command": "/usr/bin/tool",
        "args": ["--token", _SECRET],
        "env": {"API_KEY": _SECRET},
        **extra,
    }


def _write_claude_plugin(config_dir: Path, *, enabled: bool = True) -> None:
    plugin = config_dir / "plugins" / "cache" / "market" / "figma" / "1.0"
    _write_json(plugin / ".mcp.json", {"mcpServers": {"figma": _stdio()}})
    _write_json(
        plugin / ".claude-plugin" / "plugin.json",
        {"name": "figma", "mcpServers": {"figma": _stdio(), "figjam": {"url": "https://f.io/m"}}},
    )
    _write_json(
        config_dir / "plugins" / "installed_plugins.json",
        {
            "version": 2,
            "plugins": {"figma@market": [{"scope": "user", "installPath": str(plugin)}]},
        },
    )
    _write_json(config_dir / "settings.json", {"enabledPlugins": {"figma@market": enabled}})


def test_reads_each_harness_and_drops_secrets(home: Path) -> None:
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "github": _stdio(),
                "linear": {
                    "type": "http",
                    "url": f"https://mcp.linear.app/mcp?token={_SECRET}",
                    "headers": {"Authorization": f"Bearer {_SECRET}"},
                },
                "omnigent": _stdio(),
            },
            "projects": {"/repo": {"mcpServers": {"project-only": _stdio()}}},
        },
    )
    _write_claude_plugin(home / ".claude")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        "[mcp_servers.glean]\n"
        f'command = "glean"\nargs = ["{_SECRET}"]\nenv = {{ TOKEN = "{_SECRET}" }}\n'
        '[mcp_servers.off]\ncommand = "off"\nenabled = false\n'
        '[mcp_servers.docs]\nurl = "https://docs.example.com/mcp"\n'
        '[mcp_servers.omnigent]\ncommand = "python"\n'
    )
    _write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"slack": _stdio()}})

    servers = discover_mcp_servers()

    assert servers == [
        {"name": "github", "harness": "claude", "transport": "stdio", "scope": "user"},
        {
            "name": "linear",
            "harness": "claude",
            "transport": "http",
            "scope": "user",
            "url_host": "mcp.linear.app",
        },
        {
            "name": "figma",
            "harness": "claude",
            "transport": "stdio",
            "scope": "user",
            "plugin": "figma",
        },
        {
            "name": "figjam",
            "harness": "claude",
            "transport": "http",
            "scope": "user",
            "plugin": "figma",
            "url_host": "f.io",
        },
        {"name": "glean", "harness": "codex", "transport": "stdio", "scope": "user"},
        {
            "name": "docs",
            "harness": "codex",
            "transport": "http",
            "scope": "user",
            "url_host": "docs.example.com",
        },
        {"name": "slack", "harness": "cursor", "transport": "stdio", "scope": "user"},
    ]
    assert _SECRET not in json.dumps(servers)
    assert "/usr/bin/tool" not in json.dumps(servers)


def test_honors_claude_and_codex_config_homes(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_dir = tmp_path / "claude-config"
    codex_home = tmp_path / "codex-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    _write_json(home / ".claude.json", {"mcpServers": {"wrong-home": _stdio()}})
    _write_json(claude_dir / ".claude.json", {"mcpServers": {"right-home": _stdio()}})
    _write_claude_plugin(claude_dir, enabled=False)
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('[mcp_servers.codex-home]\ncommand = "x"\n')

    assert [(s["harness"], s["name"]) for s in discover_mcp_servers()] == [
        ("claude", "right-home"),
        ("codex", "codex-home"),
    ]


def test_missing_and_malformed_configs_do_not_fail(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert discover_mcp_servers() == []
    assert not caplog.records

    (home / ".claude.json").write_text("{not json")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text("[mcp_servers\n")
    _write_json(
        home / ".cursor" / "mcp.json",
        {"mcpServers": {"ok": {"command": "x"}, "junk": "not a table", "bare": {}}},
    )

    assert discover_mcp_servers() == [
        {"name": "ok", "harness": "cursor", "transport": "stdio", "scope": "user"}
    ]
    assert {"claude", "codex"} <= {
        word for record in caplog.records for word in record.getMessage().split()
    }


def test_one_harness_crashing_keeps_the_others(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"slack": _stdio()}})

    def crash(*_args: object) -> list[dict[str, str]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_inventory, "_claude_servers", crash)
    assert [s["name"] for s in discover_mcp_servers()] == ["slack"]


def test_inventory_caches_until_the_ttl(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(mcp_inventory.time, "monotonic", lambda: now[0])
    config = home / ".cursor" / "mcp.json"
    _write_json(config, {"mcpServers": {"first": _stdio()}})
    inventory = HostMcpInventory()
    assert [s["name"] for s in inventory.discover()] == ["first"]

    _write_json(config, {"mcpServers": {"second": _stdio()}})
    inventory.discover()[0]["name"] = "mutated"
    assert [s["name"] for s in inventory.discover()] == ["first"]

    now[0] += mcp_inventory.MCP_INVENTORY_CACHE_TTL_SECONDS + 1
    assert [s["name"] for s in inventory.discover()] == ["second"]
