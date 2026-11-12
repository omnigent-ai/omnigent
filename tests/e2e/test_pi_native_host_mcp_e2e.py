"""Real Pi connects to MCP servers that only Claude Code, Codex and Cursor configure.

Run with ``pytest tests/e2e/test_pi_native_host_mcp_e2e.py -v``. Needs a Pi CLI
with built-in MCP (0.99 or newer) on ``PATH`` or in ``OMNIGENT_PI_PATH``. The
host home, Pi agent dir and server are temporary; no model is called.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import click
import pytest

from omnigent.harnesses.pi_native.credentials import PiProviderConfig, pi_native_provider_launch
from omnigent.harnesses.pi_native.main import pi_version, resolve_pi_executable
from omnigent.host import mcp_inventory
from tests._helpers.live_server import find_free_port, terminate_process

_FIXTURES = Path(__file__).parents[1] / "tools" / "fixtures"
_ECHO_SERVER = _FIXTURES / "echo_stdio_mcp_server.py"
_ECHO_HTTP_SERVER = _FIXTURES / "echo_http_mcp_server.py"
_FIRST_PI_WITH_MCP = (0, 99)


def _pi_with_mcp() -> str | None:
    try:
        executable = resolve_pi_executable()
    except click.ClickException:
        return None
    version = pi_version(executable)
    return executable if version is not None and version[:2] >= _FIRST_PI_WITH_MCP else None


_PI = _pi_with_mcp()

pytestmark = [
    pytest.mark.skipif(_PI is None, reason="requires a Pi CLI with built-in MCP (0.99+)"),
    pytest.mark.timeout(120),
]


# Writes one environment variable to a file, then runs the echo server.
_RECORD_ENV_THEN_SERVE = (
    "import os, runpy, sys; "
    "open(sys.argv[1], 'w').write(os.environ.get(sys.argv[2], '<unset>')); "
    "runpy.run_path(sys.argv[3], run_name='__main__')"
)


def _echo_server(**extra: object) -> dict[str, object]:
    return {"command": sys.executable, "args": [str(_ECHO_SERVER)], **extra}


def _echo_server_recording(variable: str, record: Path, value: str) -> dict[str, object]:
    return {
        "command": sys.executable,
        "args": ["-c", _RECORD_ENV_THEN_SERVE, str(record), variable, str(_ECHO_SERVER)],
        "env": {variable: value},
    }


@pytest.fixture
def echo_http_port() -> Iterator[int]:
    """A local streamable-HTTP MCP server serving ``/mcp`` on the yielded port."""
    port = find_free_port()
    server = subprocess.Popen(
        [sys.executable, str(_ECHO_HTTP_SERVER), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline or server.poll() is not None:
                    raise
                time.sleep(0.2)
        yield port
    finally:
        terminate_process(server)


def test_managed_pi_session_connects_to_host_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pi_host_mcp_servers: None, echo_http_port: int
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    monkeypatch.setenv("ECHO_HTTP_PORT", str(echo_http_port))
    monkeypatch.setattr(mcp_inventory, "_managed_mcp_paths", tuple)
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "claude-echo": _echo_server_recording(
                        "ECHO_TOKEN", tmp_path / "claude-env", "${ECHO_TOKEN}"
                    ),
                    "claude-http": {
                        "type": "http",
                        "url": "http://127.0.0.1:${ECHO_HTTP_PORT}/mcp",
                        "headers": {"X-Echo-Token": "${ECHO_TOKEN}"},
                    },
                    "shared": {"command": "/nonexistent/host-version"},
                }
            }
        ),
        encoding="utf-8",
    )
    # Codex values are literal, so Pi must not interpolate this one.
    codex = _echo_server_recording("LITERAL", tmp_path / "codex-env", "${ECHO_TOKEN} $5")
    (home / "codex").mkdir()
    (home / "codex" / "config.toml").write_text(
        "[mcp_servers.codex-echo]\n"
        f"command = {json.dumps(codex['command'])}\n"
        f"args = {json.dumps(codex['args'])}\n"
        f"env = {{ LITERAL = {json.dumps(codex['env']['LITERAL'])} }}\n",
        encoding="utf-8",
    )
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cursor-echo": _echo_server_recording("PRICE", tmp_path / "cursor-env", "$5")
                }
            }
        ),
        encoding="utf-8",
    )
    pi_agent = home / ".pi" / "agent"
    pi_agent.mkdir(parents=True)
    user_config = json.dumps({"mcpServers": {"shared": _echo_server()}})
    (pi_agent / "mcp.json").write_text(user_config, encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_agent))
    provider = PiProviderConfig(
        provider_id="omnigent",
        base_url="http://127.0.0.1:9/v1",
        api="openai-completions",
        model="demo-model",
        api_key="synthetic-key",
        auth_header=False,
    )

    launch = pi_native_provider_launch(tmp_path / "managed-agent", provider)
    assert _PI is not None
    result = subprocess.run(
        [_PI, "mcp", "list", "--json"],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "ECHO_TOKEN": "synthetic-token",
            **launch.env,
        },
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["errors"] == []
    states = {server["name"]: (server["state"], server["tools"]) for server in report["servers"]}
    assert states == {
        "claude-echo": ("connected", ["echo"]),
        "claude-http": ("connected", ["echo", "slow_echo"]),
        "codex-echo": ("connected", ["echo"]),
        "cursor-echo": ("connected", ["echo"]),
        "shared": ("connected", ["echo"]),
    }
    assert (tmp_path / "claude-env").read_text(encoding="utf-8") == "synthetic-token"
    assert (tmp_path / "codex-env").read_text(encoding="utf-8") == "${ECHO_TOKEN} $5"
    assert (tmp_path / "cursor-env").read_text(encoding="utf-8") == "$5"
    assert (pi_agent / "mcp.json").read_text(encoding="utf-8") == user_config
    assert sorted(path.name for path in pi_agent.iterdir()) == ["mcp.json"]
