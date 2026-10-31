"""The host MCP inventory reports names and metadata, never secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.host import mcp_inventory
from omnigent.host.mcp_inventory import (
    HostMcpInventory,
    discover_mcp_servers,
    read_auth_status_overlay,
)
from omnigent.spec.skill_sources import _plugin_asset_id

_SECRET = "sk-live-secret-token"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(mcp_inventory, "_warned_auth_status", None)
    # A real managed config or status-file override on the dev machine must not leak in.
    monkeypatch.setattr(mcp_inventory, "_managed_mcp_paths", tuple)
    monkeypatch.delenv(mcp_inventory.MCP_AUTH_STATUS_ENV, raising=False)
    return home


def _write_managed_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: dict[str, object]
) -> Path:
    """Point the managed chain at a writable copy of Claude Code's enterprise config."""
    path = tmp_path / "managed" / "managed-mcp.json"
    _write_json(path, {"mcpServers": servers})
    monkeypatch.setattr(mcp_inventory, "_managed_mcp_paths", lambda: (path,))
    return path


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
            "source_id": _plugin_asset_id("figma@market", "mcp", "figma"),
        },
        {
            "name": "figjam",
            "harness": "claude",
            "transport": "http",
            "scope": "user",
            "plugin": "figma",
            "source_id": _plugin_asset_id("figma@market", "mcp", "figjam"),
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


def _write_auth_status(home: Path, servers: list[object]) -> None:
    _write_json(home / ".omnigent" / "mcp-auth-status.json", {"servers": servers})


def test_auth_status_is_stamped_on_the_servers_it_names(home: Path) -> None:
    _write_json(
        home / ".cursor" / "mcp.json",
        {"mcpServers": {"slack": _stdio(), "linear": {"url": "https://mcp.linear.app/mcp"}}},
    )
    _write_auth_status(
        home,
        [
            {"harness": "cursor", "name": "slack", "status": "connected"},
            {
                "harness": "cursor",
                "name": "linear",
                "status": "sign_in_required",
                "sign_in_url": "https://auth.example.com/start?state=abc",
            },
            # A server the inventory did not find, and one on another harness, are ignored.
            {"harness": "cursor", "name": "ghost", "status": "connected"},
            {"harness": "claude", "name": "slack", "status": "sign_in_required"},
        ],
    )

    servers = HostMcpInventory().discover()

    assert [(s["name"], s.get("auth_status"), s.get("sign_in_url")) for s in servers] == [
        ("slack", "connected", None),
        ("linear", "sign_in_required", "https://auth.example.com/start?state=abc"),
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"harness": "cursor", "name": "slack", "status": "pending"},
        {"harness": "cursor", "name": "slack"},
        {"harness": "cursor", "name": 7, "status": "connected"},
        "slack",
    ],
)
def test_auth_status_entries_that_do_not_validate_are_dropped(home: Path, entry: object) -> None:
    _write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"slack": _stdio()}})
    _write_auth_status(home, [entry])
    assert "auth_status" not in HostMcpInventory().discover()[0]


@pytest.mark.parametrize(
    "status,url,expected",
    [
        (
            "sign_in_required",
            "https://auth.example.com/go",
            {"sign_in_url": "https://auth.example.com/go"},
        ),
        (
            "sign_in_required",
            "http://auth.example.com/go",
            {"sign_in_url": "http://auth.example.com/go"},
        ),
        # Only a page a browser can open is passed on.
        ("sign_in_required", "javascript:alert(1)", {}),
        ("sign_in_required", "file:///etc/passwd", {}),
        ("sign_in_required", "auth.example.com", {}),
        ("sign_in_required", "//auth.example.com/go", {}),
        ("sign_in_required", "http://[::1", {}),  # urlsplit raises on this
        ("sign_in_required", "", {}),
        ("sign_in_required", 5, {}),
        # A connected account has nothing to sign in to.
        ("connected", "https://auth.example.com/go", {}),
    ],
)
def test_only_an_openable_url_is_reported_and_only_while_sign_in_is_due(
    home: Path, status: str, url: object, expected: dict[str, str]
) -> None:
    _write_auth_status(
        home, [{"harness": "cursor", "name": "slack", "status": status, "sign_in_url": url}]
    )
    assert read_auth_status_overlay(home) == {
        ("cursor", "slack"): {"auth_status": status, **expected}
    }


def test_an_oversized_status_file_is_ignored(home: Path) -> None:
    """The report is a short list; a huge file is not parsed on every poll."""
    path = home / ".omnigent" / "mcp-auth-status.json"
    path.parent.mkdir(parents=True)
    padding = "x" * mcp_inventory.MCP_AUTH_STATUS_MAX_BYTES
    path.write_text(
        json.dumps(
            {
                "pad": padding,
                "servers": [{"harness": "cursor", "name": "slack", "status": "connected"}],
            }
        )
    )
    assert path.stat().st_size > mcp_inventory.MCP_AUTH_STATUS_MAX_BYTES
    assert read_auth_status_overlay(home) == {}


def test_a_status_path_that_is_not_a_regular_file_is_ignored(home: Path) -> None:
    """A FIFO reports size 0 and would block this thread forever; the panel polls it."""
    path = home / ".omnigent" / "mcp-auth-status.json"
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    assert read_auth_status_overlay(home) == {}


def test_an_unusable_status_file_is_logged_once_per_change(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The panel polls this file every few seconds; one bad write is one warning."""
    path = home / ".omnigent" / "mcp-auth-status.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with caplog.at_level("WARNING", logger="omnigent.host.mcp_inventory"):
        for _ in range(3):
            assert read_auth_status_overlay(home) == {}
        assert len(caplog.records) == 1

        # A later bad write is a new problem and is reported again.
        path.write_text("{still not json either}")
        assert read_auth_status_overlay(home) == {}
        assert len(caplog.records) == 2

        # Once it parses, the next bad write is reported rather than suppressed.
        _write_auth_status(home, [])
        assert read_auth_status_overlay(home) == {}
        path.write_text("{broken again")
        assert read_auth_status_overlay(home) == {}
        assert len(caplog.records) == 3


def test_malformed_or_missing_auth_status_file_changes_nothing(home: Path) -> None:
    _write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"slack": _stdio()}})
    assert "auth_status" not in HostMcpInventory().discover()[0]
    path = home / ".omnigent" / "mcp-auth-status.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert "auth_status" not in HostMcpInventory().discover()[0]
    _write_json(path, {"servers": {"slack": "connected"}})
    assert "auth_status" not in HostMcpInventory().discover()[0]
    # Deeply nested JSON raises RecursionError, which is not a ValueError: letting it
    # escape would lose every host server, not just the account state.
    path.write_text("[" * 60_000)
    servers = HostMcpInventory().discover()
    assert [s["name"] for s in servers] == ["slack"]
    assert "auth_status" not in servers[0]


def test_auth_status_is_fresh_while_the_config_scan_is_cached(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_inventory.time, "monotonic", lambda: 100.0)
    _write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"slack": _stdio()}})
    inventory = HostMcpInventory()
    _write_auth_status(
        home,
        [
            {
                "harness": "cursor",
                "name": "slack",
                "status": "sign_in_required",
                "sign_in_url": "https://a.b/",
            }
        ],
    )
    assert inventory.discover()[0]["auth_status"] == "sign_in_required"

    # The sign-in manager flips the file; the next request sees it without a rescan.
    _write_auth_status(home, [{"harness": "cursor", "name": "slack", "status": "connected"}])
    first = inventory.discover()[0]
    assert (first["auth_status"], first.get("sign_in_url")) == ("connected", None)
    # And the cached scan itself is not mutated by the overlay.
    _write_auth_status(home, [])
    assert "auth_status" not in inventory.discover()[0]


def test_managed_config_servers_are_discovered_and_outrank_user_config(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An organization's pinned servers are often the ones needing sign-in, so the
    inventory has to see them; they also win their name against the user's own config."""
    _write_managed_mcp(
        tmp_path,
        monkeypatch,
        {
            "linear": {"type": "http", "url": f"https://mcp.linear.app/mcp?token={_SECRET}"},
            "github": _stdio(),
            "off": _stdio(enabled=False),
            # Omnigent's own relay is not user config wherever it appears.
            "omnigent": _stdio(),
        },
    )
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "github": {"type": "http", "url": "https://user.example.com/mcp"},
                "own": _stdio(),
            }
        },
    )

    servers = discover_mcp_servers()

    assert servers == [
        {
            "name": "linear",
            "harness": "claude",
            "transport": "http",
            "scope": "user",
            "managed": "true",
            "url_host": "mcp.linear.app",
        },
        {
            "name": "github",
            "harness": "claude",
            "transport": "stdio",
            "scope": "user",
            "managed": "true",
        },
        {"name": "own", "harness": "claude", "transport": "stdio", "scope": "user"},
    ]
    assert _SECRET not in json.dumps(servers)


def test_only_the_first_readable_managed_config_counts(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code reads the managed chain first-readable-wins, so a lower-precedence
    file is ignored even when the winning one pins no servers."""
    missing = tmp_path / "missing" / "managed-mcp.json"
    unreadable = tmp_path / "unreadable" / "managed-mcp.json"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("not json")
    winner = tmp_path / "winner" / "managed-mcp.json"
    _write_json(winner, {"mcpServers": {"linear": _stdio()}})
    lower = tmp_path / "lower" / "managed-mcp.json"
    _write_json(lower, {"mcpServers": {"github": _stdio()}})
    paths = (missing, unreadable, winner, lower)
    monkeypatch.setattr(mcp_inventory, "_managed_mcp_paths", lambda: paths)

    assert [server["name"] for server in discover_mcp_servers()] == ["linear"]

    # A winning file with no servers still shadows the lower-precedence one.
    _write_json(winner, {"permissions": {}})
    assert discover_mcp_servers() == []


def test_without_a_managed_config_nothing_changes(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common install has no such file; discovery must not depend on it."""
    _write_json(home / ".claude.json", {"mcpServers": {"own": _stdio()}})
    monkeypatch.setattr(
        mcp_inventory, "_managed_mcp_paths", lambda: (tmp_path / "absent" / "managed-mcp.json",)
    )
    assert discover_mcp_servers() == [
        {"name": "own", "harness": "claude", "transport": "stdio", "scope": "user"}
    ]


def test_a_managed_server_reaches_the_panel_with_its_account_state(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end for the case the feature exists for: a server the user cannot edit,
    reported as needing sign-in, arriving at the panel with its state and URL."""
    from omnigent.host.frames import (
        HostMcpServersResultFrame,
        decode_host_frame,
        encode_host_frame,
    )
    from omnigent.server.routes.mcp_servers import McpServerSummary

    _write_managed_mcp(tmp_path, monkeypatch, {"linear": {"url": "https://mcp.linear.app/mcp"}})
    _write_auth_status(
        home,
        [
            {
                "harness": "claude",
                "name": "linear",
                "status": "sign_in_required",
                "sign_in_url": "https://auth.example.com/start",
            }
        ],
    )

    discovered = HostMcpInventory().discover()
    answered = decode_host_frame(
        encode_host_frame(HostMcpServersResultFrame("r", "ok", mcp_servers=discovered))
    )
    assert isinstance(answered, HostMcpServersResultFrame)
    published = McpServerSummary.model_validate(answered.mcp_servers[0]).model_dump()

    assert published["name"] == "linear"
    assert published["managed"] is True
    assert published["auth_status"] == "sign_in_required"
    assert published["sign_in_url"] == "https://auth.example.com/start"


def test_the_status_file_can_live_outside_a_read_only_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where ``$HOME`` is read-only, producer and reader agree through the env var."""
    _write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"slack": _stdio()}})
    elsewhere = tmp_path / "run" / "mcp-auth-status.json"
    _write_json(
        elsewhere, {"servers": [{"harness": "cursor", "name": "slack", "status": "connected"}]}
    )
    # The default location is deliberately left empty.
    assert HostMcpInventory().discover()[0].get("auth_status") is None

    monkeypatch.setenv(mcp_inventory.MCP_AUTH_STATUS_ENV, str(elsewhere))
    assert HostMcpInventory().discover()[0]["auth_status"] == "connected"
