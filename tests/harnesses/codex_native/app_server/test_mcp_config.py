"""Mcp config tests for Codex app server."""

from __future__ import annotations

import errno
import stat
import traceback
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import tomlkit

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from omnigent.harnesses.codex_native import app_server, launch_args
from tests.harnesses.codex_native.app_server._support import (
    _PLAIN_TOOL_APPROVALS,
    _disable_codex_startup_rpc,
    _test_app_server,
)


async def test_start_upserts_mcp_server_config_across_relaunches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Codex native startup upserts MCP config across relaunches.

    Repeated native terminal startup uses the same private ``CODEX_HOME``.
    The generated config must remain valid TOML and the user's real
    symlinked config must stay untouched.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    source_config = real_codex_home / "config.toml"
    original = """\
[projects."/repo"]
trust_level = "trusted"

[mcp_servers.omnigent] # stale generated table
command = "/old/python"
args = ["old"]

[mcp_servers.omnigent.env] # stale generated env
OLD = "1"

[mcp_servers.omnigent.tools.sys_session_rename] # stale generated approval
approval_mode = "prompt"

[mcp_servers.other]
command = "other"
args = []
"""
    source_config.write_text(original, encoding="utf-8")

    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    await server.close()
    await server.start()
    await server.close()

    assert source_config.read_text(encoding="utf-8") == original
    config_path = codex_home / "config.toml"
    assert not config_path.is_symlink()
    rendered = config_path.read_text(encoding="utf-8")
    assert rendered.count("[mcp_servers.omnigent]") == 1
    assert "[mcp_servers.omnigent.env]" not in rendered
    parsed = tomllib.loads(rendered)
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["omnigent"] == {
        "command": "/new/python",
        "args": [
            "-I",
            "-m",
            "omnigent.harnesses.claude_native.bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        "tools": _PLAIN_TOOL_APPROVALS,
    }


async def test_cold_start_refreshes_user_mcp_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new app-server reads current MCPs without resetting session settings."""
    source = tmp_path / "source"
    source.mkdir()
    source_config = source / "config.toml"
    source_config.write_text(
        'model = "shared"\n[mcp_servers.changed]\ncommand = "old"\n'
        'env = { OLD = "value" }\n[mcp_servers.removed]\ncommand = "removed"\n'
    )
    private = tmp_path / "private"
    monkeypatch.setenv("CODEX_HOME", str(source))
    _disable_codex_startup_rpc(monkeypatch)
    server = _test_app_server(tmp_path, private, tmp_path / "bridge", tmp_path)
    await server.start()
    await server.close()
    assert server.proc is None
    config_path = private / "config.toml"
    document = tomlkit.parse(config_path.read_text())
    document["model"] = "session-model"
    document["model_reasoning_effort"] = "high"
    config_path.write_text(tomlkit.dumps(document))
    updated = (
        'model = "new-shared-model"\n[mcp_servers.changed]\ncommand = "new"\n'
        'env = { NEW = "value" }\n[mcp_servers.added]\nurl = "https://example.test/mcp"\n'
        "enabled = false\n"
    )
    source_config.write_text(updated)

    await server.start()
    await server.close()

    config = tomllib.loads(config_path.read_text())
    assert config["model"] == "session-model"
    assert config["model_reasoning_effort"] == "high"
    assert set(config["mcp_servers"]) == {"changed", "added", "omnigent"}
    assert config["mcp_servers"]["changed"] == {"command": "new", "env": {"NEW": "value"}}
    assert config["mcp_servers"]["added"] == {
        "url": "https://example.test/mcp",
        "enabled": False,
    }
    assert config["mcp_servers"]["omnigent"]["command"] == "/new/python"
    assert source_config.read_text() == updated


@pytest.mark.parametrize("empty_source", [None, "[mcp_servers]\n"])
async def test_cold_start_removes_all_user_mcps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty_source: str | None
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_config = source / "config.toml"
    source_config.write_text('[mcp_servers.removed]\ncommand = "old"\n')
    private = tmp_path / "private"
    monkeypatch.setenv("CODEX_HOME", str(source))
    _disable_codex_startup_rpc(monkeypatch)
    server = _test_app_server(tmp_path, private, tmp_path / "bridge", tmp_path)
    await server.start()
    await server.close()
    if empty_source is None:
        source_config.unlink()
    else:
        source_config.write_text(empty_source)

    await server.start()
    await server.close()

    config = tomllib.loads((private / "config.toml").read_text())
    assert set(config["mcp_servers"]) == {"omnigent"}


@pytest.mark.parametrize("version", [(0, 133, 0), (0, 154, 0)])
async def test_cold_start_refreshes_mcps_across_profile_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    private = tmp_path / "private"
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setattr(app_server, "_codex_cli_version", AsyncMock(return_value=version))
    _disable_codex_startup_rpc(monkeypatch)
    server = _test_app_server(tmp_path, private, tmp_path / "bridge", tmp_path)
    for generation, profile in enumerate(("work", "work", "other", None)):
        base = {"mcp_servers": {"shared": {"command": "base", "args": [str(generation)]}}}
        overlays = {
            "work": {"mcp_servers": {"shared": {"command": f"work-{generation}"}}},
            "other": {"mcp_servers": {"other": {"command": "other"}}},
        }
        (source / "config.toml").write_text(tomlkit.dumps({**base, "profiles": overlays}))
        for name, overlay in overlays.items():
            (source / f"{name}.config.toml").write_text(tomlkit.dumps(overlay))
        server.config_profile = profile

        await server.start()
        await server.close()

        config = tomllib.loads((private / "config.toml").read_text())
        servers = config["mcp_servers"]
        assert servers["shared"] == {
            "command": f"work-{generation}" if profile == "work" else "base",
            "args": [str(generation)],
        }
        assert set(servers) == (
            {"shared", "omnigent", "other"} if profile == "other" else {"shared", "omnigent"}
        )


@pytest.mark.parametrize(
    ("invalid", "profile", "diagnostic"),
    [
        pytest.param("invalid = [", None, "UnexpectedEofError at line 1, column", id="syntax"),
        pytest.param('mcp_servers = "bad"', None, "Invalid mcp_servers", id="inventory-shape"),
        pytest.param("[mcp_servers]\nbad = 1", None, "Invalid mcp_servers", id="server-shape"),
        pytest.param(
            PermissionError(errno.EACCES, "Permission denied"),
            None,
            "Permission denied",
            id="unreadable",
        ),
        pytest.param(b"\xff", None, "UnicodeDecodeError", id="invalid-utf8"),
        pytest.param("invalid = [", "work", "UnexpectedEofError", id="profile-syntax"),
    ],
)
async def test_cold_start_invalid_mcp_source_preserves_private_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str | bytes | OSError,
    profile: str | None,
    diagnostic: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    content = invalid if isinstance(invalid, str) else ""
    (source / "config.toml").write_text(content if profile is None else "")
    if profile:
        (source / f"{profile}.config.toml").write_text(content)
    failed_path = source / (f"{profile}.config.toml" if profile else "config.toml")
    if isinstance(invalid, bytes):
        failed_path.write_bytes(invalid)
    elif isinstance(invalid, OSError):
        original_read = Path.read_text

        def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == failed_path:
                raise invalid
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
    private = tmp_path / "private"
    private.mkdir()
    original = 'model = "private"\n[mcp_servers.existing]\ncommand = "keep"\n'
    (private / "config.toml").write_text(original)
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setattr(app_server, "_codex_cli_version", AsyncMock(return_value=(0, 154, 0)))
    spawn = AsyncMock()
    monkeypatch.setattr(app_server.asyncio, "create_subprocess_exec", spawn)
    server = _test_app_server(tmp_path, private, tmp_path / "bridge", tmp_path)
    server.config_profile = profile

    with pytest.raises(ValueError, match=r"Codex.*config") as caught:
        await server.start()

    assert str(failed_path) in str(caught.value)
    assert diagnostic in str(caught.value)
    assert (private / "config.toml").read_text() == original
    spawn.assert_not_called()


def test_mcp_refresh_reads_utf8_independently_of_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    config_path = private / "config.toml"
    config_path.write_text('developer_instructions = "保持设置"\n', encoding="utf-8")
    (source / "config.toml").write_text(
        '[mcp_servers.search]\ncommand = "搜索"\n', encoding="utf-8"
    )
    (source / "work.config.toml").write_text(
        '[mcp_servers.search]\nargs = ["資料"]\n', encoding="utf-8"
    )
    original_read = Path.read_text

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        assert kwargs.get("encoding") == "utf-8"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    servers = launch_args.read_codex_mcp_servers(source, "work", codex_version=(0, 154, 0))
    app_server._inject_mcp_server_config(private, tmp_path / "bridge", mcp_servers=servers)

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcp_servers"]["search"] == {"command": "搜索", "args": ["資料"]}
    assert config["developer_instructions"] == "保持设置"


def test_mcp_parse_diagnostic_does_not_expose_config_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('[mcp_servers.test.env]\nTOKEN = "private-value', encoding="utf-8")

    with pytest.raises(ValueError, match=r"line 2, column") as caught:
        launch_args.read_codex_mcp_servers(tmp_path, None, codex_version=(0, 154, 0))

    diagnostic = "".join(traceback.format_exception(caught.value))
    assert str(config_path) in diagnostic
    assert "UnexpectedEofError" in diagnostic
    assert "private-value" not in diagnostic


async def test_cold_start_rejects_shared_home_before_modifying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir(mode=0o755)
    config_path = source / "config.toml"
    original = 'model = "keep"\n'
    config_path.write_text(original, encoding="utf-8")
    original_mode = stat.S_IMODE(source.stat().st_mode)
    monkeypatch.setenv("CODEX_HOME", str(source))
    spawn = AsyncMock()
    monkeypatch.setattr(app_server.asyncio, "create_subprocess_exec", spawn)
    server = _test_app_server(tmp_path, source, tmp_path / "bridge", tmp_path)

    with pytest.raises(ValueError, match="Please report this as a bug"):
        await server.start()

    assert config_path.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(source.stat().st_mode) == original_mode
    spawn.assert_not_called()


@pytest.mark.parametrize(
    ("profile", "version"),
    [
        pytest.param(None, (0, 154, 0), id="no-profile"),
        pytest.param("work", (0, 154, 0), id="file-profile"),
        pytest.param("work", (0, 133, 0), id="legacy-profile"),
    ],
)
async def test_cold_start_minimal_config_preserves_explicit_profile_mcps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
    version: tuple[int, int, int],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    base = '[mcp_servers.ambient]\ncommand = "do-not-start"\n'
    legacy_profile = '[profiles.work.mcp_servers.explicit]\ncommand = "old"\n'
    source_config = source / "config.toml"
    source_config.write_text(base + legacy_profile)
    profile_config = source / "work.config.toml"
    profile_config.write_text('[mcp_servers.explicit]\ncommand = "old"\n')
    monkeypatch.setenv("CODEX_HOME", str(source))
    monkeypatch.setenv("HARNESS_CODEX_MINIMAL_CONFIG", "true")
    monkeypatch.setattr(app_server, "_codex_cli_version", AsyncMock(return_value=version))
    _disable_codex_startup_rpc(monkeypatch)
    private = tmp_path / "private"
    server = _test_app_server(tmp_path, private, tmp_path / "bridge", tmp_path)
    server.config_profile = profile

    await server.start()
    await server.close()

    expected = {"explicit", "omnigent"} if profile else {"omnigent"}
    config_path = private / "config.toml"
    assert set(tomllib.loads(config_path.read_text())["mcp_servers"]) == expected

    if profile and version < (0, 134, 0):
        source_config.write_text(base + legacy_profile.replace('"old"', '"new"'))
    else:
        # The existing minimal home no longer needs the ambient source on restart.
        source_config.write_text("invalid = [")
        profile_config.write_text('[mcp_servers.explicit]\ncommand = "new"\n')

    await server.start()
    await server.close()

    servers = tomllib.loads(config_path.read_text())["mcp_servers"]
    assert set(servers) == expected
    if profile:
        assert servers["explicit"] == {"command": "new"}


def test_mcp_refresh_atomically_replaces_private_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.toml"
    original = 'model = "keep"\n[mcp_servers.old]\ncommand = "old"\n'
    source.write_text(original)
    private = tmp_path / "private"
    private.mkdir()
    config_path = private / "config.toml"
    config_path.symlink_to(source)

    app_server._inject_mcp_server_config(
        private, tmp_path / "bridge", mcp_servers={"new": {"command": "new"}}
    )

    assert source.read_text() == original
    assert not config_path.is_symlink()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    config = tomllib.loads(config_path.read_text())
    assert config["model"] == "keep"
    assert set(config["mcp_servers"]) == {"new", "omnigent"}


def test_failed_mcp_refresh_preserves_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    original = 'model = "keep"\n[mcp_servers.old]\ncommand = "old"\n'
    config_path.write_text(original)
    monkeypatch.setattr(launch_args.os, "replace", Mock(side_effect=OSError("write failed")))

    with pytest.raises(OSError, match="write failed"):
        app_server._inject_mcp_server_config(tmp_path, tmp_path / "bridge", mcp_servers={})

    assert config_path.read_text() == original
    assert not list(tmp_path.glob(".config.toml.*"))


async def test_start_writes_fresh_mcp_config_without_leading_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Codex native startup writes fresh MCP config without leading blanks.

    Codex should be able to read a newly-created private ``config.toml``
    without cosmetic leading whitespace from the generated section
    separator logic.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    await server.close()

    rendered = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert rendered.startswith("[mcp_servers.omnigent]\n")
    assert stat.S_IMODE(codex_home.stat().st_mode) == 0o700
    assert stat.S_IMODE((codex_home / "config.toml").stat().st_mode) == 0o600
    parsed = tomllib.loads(rendered)
    assert parsed["mcp_servers"]["omnigent"] == {
        "command": "/new/python",
        "args": [
            "-I",
            "-m",
            "omnigent.harnesses.claude_native.bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        "tools": _PLAIN_TOOL_APPROVALS,
    }
