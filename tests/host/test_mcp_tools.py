"""Real stdio/HTTP probes, bounded work, private transport diagnostics, and cleanup."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager, suppress
from dataclasses import asdict

import psutil
import pytest

from omnigent.host import mcp_tools
from omnigent.host.mcp_inventory import ConfiguredMcpServer
from omnigent.host.mcp_tools import HostMcpTools, _effective_config
from tests.budgets import budget

SERVER_SCRIPT = """import json, os, subprocess, sys, time
from pathlib import Path
time.sleep(float(os.environ.get("STARTUP_DELAY", "0")))
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pid_file = Path(os.environ["PID_FILE"])
pid_file.with_suffix(".tmp").write_text(json.dumps([os.getpid(), child.pid]))
pid_file.with_suffix(".tmp").replace(pid_file)
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


async def _assert_reaped(tmp_path):
    async with asyncio.timeout(budget(5)):
        for pid in json.loads((tmp_path / "pids.json").read_text()):
            with suppress(psutil.NoSuchProcess):
                process = psutil.Process(pid)
                while process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    await asyncio.sleep(0.01)


async def _wait_for_stdio_tree(tmp_path, task):
    async with asyncio.timeout(budget(15)):
        while not (tmp_path / "pids.json").exists():
            if task.done():
                pytest.fail(f"Probe exited before starting the stdio fixture: {await task}")
            await asyncio.sleep(0.02)


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
    await _assert_reaped(tmp_path)


@pytest.mark.parametrize("startup_delay", [0, 3])
async def test_timeout_reaps_stdio_tree(tmp_path, monkeypatch, startup_delay):
    entry = _stdio(tmp_path, HANG="1", STARTUP_DELAY=str(startup_delay))
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
    config, cwd, transport = _effective_config(entry)
    payload = json.dumps({"config": asdict(config), "cwd": str(cwd), "transport": transport})
    worker = asyncio.create_task(mcp_tools._probe_worker(payload))

    async def running_worker(_payload):
        return await worker

    try:
        # Start the real process tree before exercising the probe's short deadline.
        await _wait_for_stdio_tree(tmp_path, worker)
        monkeypatch.setattr(mcp_tools, "_probe_worker", running_worker)
        monkeypatch.setattr(mcp_tools, "PROBE_TIMEOUT_SECONDS", 0.05)
        result = await HostMcpTools().probe("claude", "docs")
        assert result["connection"] == "timeout"
        await _assert_reaped(tmp_path)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


async def test_cancellation_reaps_stdio_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [_stdio(tmp_path, HANG="1")])
    monkeypatch.setattr(mcp_tools, "PROBE_TIMEOUT_SECONDS", budget(30))
    task = asyncio.create_task(HostMcpTools().probe("claude", "docs"))
    try:
        await _wait_for_stdio_tree(tmp_path, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _assert_reaped(tmp_path)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("via_proxy", [False, True])
async def test_http_401_needs_auth_without_private_logs(monkeypatch, caplog, capfd, via_proxy):
    for key in mcp_tools._HTTP_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

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
        target = f"127.0.0.1:{port}"
        if via_proxy:
            monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")
            target = "synthetic-mcp.invalid"
        entry = _entry(
            {
                "type": "http",
                "url": f"http://{target}/mcp?token=synthetic-url-secret",
                "headers": {"Authorization": "Bearer synthetic-header-secret"},
            }
        )
        monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
        checked = []

        async def sign_in_state(server, timeout):
            checked.append(server)
            return "available"

        monkeypatch.setattr(mcp_tools, "harness_sign_in_state", sign_in_state)
        result = await HostMcpTools().probe("claude", "docs")
    assert (result["connection"], result["harness_sign_in"]) == ("needs_auth", "available")
    assert checked == [entry]
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

    saturated = HostMcpTools()
    with monkeypatch.context() as queued:
        queued.setattr(mcp_tools, "PROBE_TIMEOUT_SECONDS", 0.01)
        async with saturated._slots, saturated._slots:
            with pytest.raises(BlockingIOError, match="capacity exhausted"):
                await saturated.probe("claude", "0")
    assert calls == 8
    assert not saturated._cache
    assert (await saturated.probe("claude", "0"))["connection"] == "connected"
    assert calls == 9


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
    async def no_sign_in_check(server, timeout):
        raise AssertionError("a harness sign-in can't supply an environment credential")

    monkeypatch.setattr(mcp_tools, "harness_sign_in_state", no_sign_in_check)
    entry = _entry({"command": "${workspaceFolder}/run"}, harness="cursor")
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
    assert (await HostMcpTools().probe("cursor", "docs"))["connection"] == "unsupported"
    entry = _entry(
        {"url": "https://example.test/mcp", "bearer_token_env_var": "ABSENT_TEST_TOKEN"},
        harness="codex",
    )
    assert await HostMcpTools().probe("codex", "docs") == mcp_tools._result("needs_auth")


async def test_missing_executable_is_unreachable(monkeypatch, tmp_path):
    entry = _entry({"command": str(tmp_path / "missing-executable")})
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
    assert (await HostMcpTools().probe("claude", "docs"))["connection"] == "unreachable"


def test_transport_timeouts_keep_the_timeout_status():
    import httpx

    timeout = httpx.ReadTimeout("synthetic-private-URL")
    assert mcp_tools._failure_status(timeout) == "timeout"
    assert mcp_tools._failure_status(ExceptionGroup("transport", [timeout])) == "timeout"


async def test_plugin_identity_selects_marketplace_and_never_probes_disabled(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from omnigent.host.plugins import discover_plugins

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    installs = {}
    for market in ("first", "second", "disabled"):
        plugin = claude / "plugins" / "cache" / market
        plugin.mkdir(parents=True)
        installs[f"toolkit@{market}"] = [{"installPath": str(plugin)}]
        (plugin / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "docs": {"command": f"fixture-{market}"},
                    }
                }
            )
        )
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": installs}))
    (claude / "settings.json").write_text(
        json.dumps({"enabledPlugins": {key: not key.endswith("@disabled") for key in installs}})
    )
    commands = []

    async def probe(payload):
        commands.append(json.loads(payload)["config"]["command"])
        return mcp_tools._result("connected")

    monkeypatch.setattr(mcp_tools, "_probe_worker", probe)
    discovery = HostMcpTools()
    plugins = {p["marketplace"]: p for p in discover_plugins()}
    active_ids = {s.summary.get("source_id") for s in mcp_tools.configured_mcp_servers()}
    for market in ("second", "first"):
        source_id = plugins[market]["mcp_entries"][0]["id"]
        assert source_id in active_ids
        await discovery.probe("claude", "docs", "toolkit", source_id)
        assert commands[-1] == f"fixture-{market}"
    disabled_id = plugins["disabled"]["mcp_entries"][0]["id"]
    assert disabled_id not in active_ids
    with pytest.raises(LookupError):
        await discovery.probe("claude", "docs", "toolkit", disabled_id)
    with pytest.raises(LookupError):
        await discovery.probe("claude", "docs", "toolkit")
    assert commands == ["fixture-second", "fixture-first"]


async def test_http_worker_inherits_only_network_settings(monkeypatch):
    from types import SimpleNamespace

    settings = {
        key: f"synthetic-{key}"
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    for key in ("OMNIGENT_RUNNER_TOKEN", "DATABRICKS_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "synthetic-private-token")
    monkeypatch.setattr(
        mcp_tools, "configured_mcp_servers", lambda: [_entry({"url": "https://example.test/mcp"})]
    )
    captured = {}

    async def capture_spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        raise RuntimeError("captured spawn")

    local_asyncio = SimpleNamespace(**vars(asyncio))
    local_asyncio.create_subprocess_exec = capture_spawn
    monkeypatch.setattr(mcp_tools, "asyncio", local_asyncio)
    with pytest.raises(RuntimeError, match="captured spawn"):
        await HostMcpTools().probe("claude", "docs")
    assert {key: captured[key] for key in settings} == settings
    assert "synthetic-private-token" not in captured.values()


async def test_http_network_settings_invalidate_probe_cache(monkeypatch):
    monkeypatch.setattr(
        mcp_tools, "configured_mcp_servers", lambda: [_entry({"url": "https://example.test/mcp"})]
    )
    calls = []

    async def probe(payload):
        calls.append(json.loads(payload)["network_env"])
        return mcp_tools._result("connected")

    monkeypatch.setattr(mcp_tools, "_probe_worker", probe)
    discovery = HostMcpTools()
    monkeypatch.setenv("HTTPS_PROXY", "http://first.example:8080")
    await discovery.probe("claude", "docs")
    await discovery.probe("claude", "docs")
    monkeypatch.setenv("HTTPS_PROXY", "http://second.example:8080")
    await discovery.probe("claude", "docs")
    assert [env["HTTPS_PROXY"] for env in calls] == [
        "http://first.example:8080",
        "http://second.example:8080",
    ]


async def test_sign_in_check_fits_the_response_budget_and_pauses_reaping(monkeypatch):
    entry = _entry({"type": "http", "url": "https://mcp.example.test/mcp"})
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])
    monkeypatch.setattr(mcp_tools, "RESPONSE_BUDGET_SECONDS", 2.0)
    monkeypatch.setattr(mcp_tools, "_STATUS_CLEANUP_SECONDS", 0.2)

    async def slow_401(payload):
        await asyncio.sleep(1.0)
        return mcp_tools._result("needs_auth")

    guarded = 0

    @contextmanager
    def guard():
        nonlocal guarded
        guarded += 1
        try:
            yield
        finally:
            guarded -= 1

    timeouts = []

    async def hanging_status(server, timeout):
        assert guarded == 1, "the orphan reaper must not collect the status command"
        timeouts.append(timeout)
        await asyncio.sleep(timeout)
        return "available"

    monkeypatch.setattr(mcp_tools, "_probe_worker", slow_401)
    monkeypatch.setattr(mcp_tools, "harness_sign_in_state", hanging_status)
    discovery = HostMcpTools(subprocess_guard=guard)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await discovery.probe("claude", "docs")
    assert result["harness_sign_in"] == "available"
    # The probe used 1.0 of the 2.0 budget and 0.2 is held back for cleanup.
    assert len(timeouts) == 1 and timeouts[0] <= 0.8
    assert loop.time() - started < 2.0 + 1.0
    assert guarded == 0
    # A shortened check is a guess, so it isn't cached.
    assert not discovery._sign_in_cache

    # With no time left for the check, the reply keeps the plain needs-auth result.
    discovery.clear_cache()
    async with discovery._sign_in_slots, discovery._sign_in_slots:
        started = loop.time()
        result = await discovery.probe("claude", "docs")
    assert loop.time() - started < 2.0 + 1.0
    assert "harness_sign_in" not in result
    assert len(timeouts) == 1


async def test_sign_in_state_is_cached_until_a_sign_in(monkeypatch):
    entry = _entry({"type": "http", "url": "https://mcp.example.test/mcp"})
    monkeypatch.setattr(mcp_tools, "configured_mcp_servers", lambda: [entry])

    async def needs_auth(payload):
        return mcp_tools._result("needs_auth")

    states = iter(["available", "available", "signed_in", "signed_in"])
    checks = 0
    checking = asyncio.Event()
    release = asyncio.Event()
    release.set()

    async def status(server, timeout):
        nonlocal checks
        checks += 1
        checking.set()
        await release.wait()
        return next(states)

    monkeypatch.setattr(mcp_tools, "_probe_worker", needs_auth)
    monkeypatch.setattr(mcp_tools, "harness_sign_in_state", status)
    discovery = HostMcpTools()

    async def sign_in() -> str:
        return (await discovery.probe("claude", "docs"))["harness_sign_in"]

    assert [await sign_in(), await sign_in()] == ["available", "available"]
    assert checks == 1
    assert all("harness_sign_in" not in cached for cached in discovery._cache.values())

    # A check that was running when the user signed in is reported but not kept.
    discovery.clear_cache()
    checking.clear()
    release.clear()
    before = asyncio.create_task(sign_in())
    await checking.wait()
    discovery.clear_cache()
    release.set()
    assert await before == "available"
    assert not discovery._sign_in_cache
    assert await sign_in() == "signed_in"
    assert checks == 3

    # So is a tools probe the server refused just before the sign-in.
    discovery.clear_cache()
    probing = asyncio.Event()
    refuse = asyncio.Event()

    async def refused_before_sign_in(payload):
        probing.set()
        await refuse.wait()
        return mcp_tools._result("needs_auth")

    monkeypatch.setattr(mcp_tools, "_probe_worker", refused_before_sign_in)
    before = asyncio.create_task(sign_in())
    await probing.wait()
    discovery.clear_cache()
    refuse.set()
    await before
    assert not discovery._cache
