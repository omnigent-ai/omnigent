"""Real stdio/HTTP probes, bounded work, private transport diagnostics, and cleanup."""

from __future__ import annotations

import asyncio
import json
import sys

import psutil
import pytest

from omnigent.host import mcp_tools
from omnigent.host.mcp_inventory import ConfiguredMcpServer
from omnigent.host.mcp_tools import HostMcpTools, _effective_config

SERVER_SCRIPT = """import json, os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
Path(os.environ["PID_FILE"]).write_text(json.dumps([os.getpid(), child.pid]))
print("synthetic-stderr-secret", file=sys.stderr, flush=True)
filler = os.environ.get("FILLER", "x")
if os.environ.get("HANG"):
    time.sleep(120)
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif message["method"] == "tools/list":
        second = message.get("params", {}).get("cursor") == "page2"
        result = {"tools": [{
            "name": "tool\\x00"+str(i)+filler*300, "description": "Read\\n"+filler*400,
            "inputSchema": {"type": "object", "description": "synthetic-schema-secret"}
        } for i in range(300 if not second else 220)]}
        if not second:
            result["nextCursor"] = "page2"
    else:
        result = {}
    print(json.dumps({"jsonrpc":"2.0", "id":message["id"], "result":result}), flush=True)
"""


def _entry(config, name="docs", harness="claude", plugin=None):
    summary = {
        "name": name,
        "harness": harness,
        "transport": "http" if "url" in config else "stdio",
    }
    if plugin:
        summary["plugin"] = plugin
    return ConfiguredMcpServer(summary, config)


def _stdio(tmp_path, **env):
    script = tmp_path / "server.py"
    script.write_text(SERVER_SCRIPT)
    return _entry(
        {
            "command": sys.executable,
            "args": [str(script)],
            "env": {
                "PID_FILE": str(tmp_path / "pids.json"),
                "TOKEN": "synthetic-env-secret",
                **env,
            },
        }
    )


def _assert_reaped(tmp_path):
    for pid in json.loads((tmp_path / "pids.json").read_text()):
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.mark.parametrize("filler", ["x", "😀"])
async def test_stdio_caps_pagination_private_output_and_cleanup(
    tmp_path, monkeypatch, caplog, capfd, filler
):
    entry = _stdio(tmp_path, FILLER=filler)
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
    result = await HostMcpTools().probe("claude", "docs")
    assert result["connection"] == "connected"
    assert result["truncated"] is True
    assert len(result["tools"]) == 500
    assert len(result["tools"][0]["name"]) == 256
    assert result["tools"][0]["description"].startswith("Read ")
    assert len(result["tools"][0]["description"]) == 300
    output = json.dumps(result) + caplog.text + str(capfd.readouterr())
    for secret in (
        "synthetic-stderr-secret",
        "synthetic-env-secret",
        "synthetic-schema-secret",
        str(tmp_path),
        sys.executable,
    ):
        assert secret not in output
    _assert_reaped(tmp_path)


async def test_timeout_reaps_stdio_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [_stdio(tmp_path, HANG="1")])
    monkeypatch.setattr(mcp_tools, "PROBE_TIMEOUT_SECONDS", 2)
    result = await HostMcpTools().probe("claude", "docs")
    assert result["connection"] == "timeout"
    _assert_reaped(tmp_path)


async def test_cancellation_reaps_stdio_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [_stdio(tmp_path, HANG="1")])
    task = asyncio.create_task(HostMcpTools().probe("claude", "docs"))
    async with asyncio.timeout(5):
        while not (tmp_path / "pids.json").exists():
            await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_reaped(tmp_path)


async def test_http_401_needs_auth_without_private_logs(monkeypatch, caplog, capfd):
    async def reject(reader, writer):
        await reader.read(8192)
        writer.write(
            b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(reject, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        entry = _entry(
            {
                "type": "http",
                "url": f"http://127.0.0.1:{port}/mcp?token=synthetic-url-secret",
                "headers": {"Authorization": "Bearer synthetic-header-secret"},
            }
        )
        monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
        result = await HostMcpTools().probe("claude", "docs")
    assert result["connection"] == "needs_auth"
    output = json.dumps(result) + caplog.text + str(capfd.readouterr())
    assert "synthetic-url-secret" not in output
    assert "synthetic-header-secret" not in output


async def test_cache_config_digest_unknown_and_concurrency(monkeypatch):
    entries = [_entry({"command": "fixture"}, name=str(i)) for i in range(5)]
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: entries)
    calls = active = peak = 0

    async def probe(payload):
        nonlocal calls, active, peak
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return mcp_tools._result("connected")

    monkeypatch.setattr(mcp_tools, "_probe_worker", probe)
    discovery = HostMcpTools()
    await asyncio.gather(*(discovery.probe("claude", str(i)) for i in range(5)))
    assert peak == 2
    await discovery.probe("claude", "0")
    assert calls == 5
    entries[0].config["args"] = ["changed"]
    await discovery.probe("claude", "0")
    assert calls == 6
    with pytest.raises(LookupError):
        await discovery.probe("claude", "0", "wrong-plugin")
    with pytest.raises(LookupError):
        await discovery.probe("cursor", "0")
    monkeypatch.setattr(mcp_tools, "PROBE_CACHE_SECONDS", 0.01)
    expiring = HostMcpTools()
    await expiring.probe("claude", "0")
    await asyncio.sleep(0.02)
    await expiring.probe("claude", "0")
    assert calls == 8


def test_effective_config_expansion_and_inheritance(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPLICIT_TOKEN", "value")
    monkeypatch.setenv("OMNIGENT_RUNNER_TOKEN", "private")
    entry = _entry(
        {
            "command": "${CLAUDE_PLUGIN_ROOT}/run",
            "args": ["${UNSET:-fallback}"],
            "env": {"TOKEN": "${EXPLICIT_TOKEN}"},
        }
    )
    entry.plugin_root = tmp_path
    config, _cwd, _transport = _effective_config(entry)
    assert config.command == f"{tmp_path}/run"
    assert config.args == ["fallback"]
    assert config.env["TOKEN"] == "value"
    assert "OMNIGENT_RUNNER_TOKEN" not in config.env
    config, _, _ = _effective_config(
        _entry({"command": "fixture", "env_vars": ["EXPLICIT_TOKEN"]}, harness="codex")
    )
    assert config.env["EXPLICIT_TOKEN"] == "value"
    config, _, _ = _effective_config(
        _entry(
            {
                "url": "https://example.test/mcp",
                "bearer_token_env_var": "EXPLICIT_TOKEN",
                "env_http_headers": {"X-Token": "EXPLICIT_TOKEN"},
            },
            harness="codex",
        )
    )
    assert config.headers == {"Authorization": "Bearer value", "X-Token": "value"}
    config, _, _ = _effective_config(
        _entry({"command": "fixture", "args": ["${env:EXPLICIT_TOKEN}"]}, harness="cursor")
    )
    assert config.args == ["value"]


async def test_unsupported_and_missing_credentials(monkeypatch):
    entry = _entry({"command": "${workspaceFolder}/run"}, harness="cursor")
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
    assert (await HostMcpTools().probe("cursor", "docs"))["connection"] == "unsupported"
    entry = _entry(
        {"url": "https://example.test/mcp", "bearer_token_env_var": "ABSENT_TEST_TOKEN"},
        harness="codex",
    )
    assert (await HostMcpTools().probe("codex", "docs"))["connection"] == "needs_auth"


async def test_missing_executable_is_unreachable(monkeypatch, tmp_path):
    entry = _entry({"command": str(tmp_path / "missing-executable")})
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
    assert (await HostMcpTools().probe("claude", "docs"))["connection"] == "unreachable"


def test_transport_timeouts_keep_the_timeout_status():
    import httpx

    timeout = httpx.ReadTimeout("synthetic-private-URL")
    assert mcp_tools._failure_status(timeout) == "timeout"
    assert mcp_tools._failure_status(ExceptionGroup("transport", [timeout])) == "timeout"  # noqa: F821
