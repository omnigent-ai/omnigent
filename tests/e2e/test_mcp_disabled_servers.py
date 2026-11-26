"""Real native CLIs must omit disabled or non-allowlisted servers and retain the Omnigent relay.

CI pins Claude 2.1.295 and Codex 0.142.0 in setup-harness-cli/manifest.json.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import (
    compat_server_cwd,
    compat_server_python,
    resolve_server_version,
    server_executable,
    server_pythonpath,
)
from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_local_server
from tests._helpers.mock_llm import advertised_tool_names
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.session import send_user_text
from tests.e2e.conftest import configure_mock_llm, get_mock_requests

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _mcp_failure_metadata(requests: list[dict], home: Path, session_id: str, harness: str) -> dict:
    """Project bounded requests and applicable Codex bridge evidence without contents."""
    servers = ("off_fixture", "on_fixture", "omnigent")
    controls = ("sys_session_get_info", "sys_session_create", "sys_terminal_list")
    known_names = (*servers, *controls, *(f"mcp__{name}" for name in servers))
    remaining = 64

    def shape(value: object) -> str:
        return (
            type(value).__name__
            if type(value) in (dict, list, str, bool, int, float, type(None))
            else "other"
        )

    def name(value: object) -> dict:
        result = {"shape": shape(value)}
        if isinstance(value, str):
            result["known"] = value if value in known_names else None
            result["markers"] = [server for server in servers if server in value[:256]]
            result["uninspected_chars"] = max(0, len(value) - 256)
        return result

    def project(items: object, depth: int = 0) -> dict:
        nonlocal remaining
        result = {"shape": shape(items)}
        if not isinstance(items, list):
            return result
        entries = []
        for item in items[:remaining] if depth < 3 else ():
            if not remaining:
                break
            remaining -= 1
            entry = {"shape": shape(item)}
            if isinstance(item, dict):
                kind = item.get("type")
                entry["type"] = (
                    kind
                    if kind in ("function", "namespace", "mcp", "tool_search", "custom")
                    else "other"
                )
                for field in ("name", "namespace", "server_label"):
                    entry[field] = name(item.get(field))
                function = item.get("function")
                entry["function"] = name(
                    function.get("name") if isinstance(function, dict) else function
                )
                entry["deferred"] = {
                    field: value if isinstance(value := item.get(field), bool) else shape(value)
                    for field in ("defer_loading", "deferred")
                }
                entry["tools"] = project(item.get("tools"), depth + 1)
            entries.append(entry)
        return {
            **result,
            "count": len(items),
            "entries": entries,
            "omitted": len(items) - len(entries),
        }

    request_metadata = [
        {
            "ordinal": index,
            "tools": project(request.get("tools") if isinstance(request, dict) else request),
        }
        for index, request in reversed(
            tuple(enumerate(requests[-8:], start=max(0, len(requests) - 8)))
        )
    ]
    bridge = (
        home / ".omnigent" / "codex-native" / hashlib.sha256(session_id.encode()).hexdigest()[:32]
    )

    def record(filename: str) -> tuple[str, object]:
        if harness != "codex":
            return "not_applicable", None
        path = bridge / filename
        try:
            if any(
                parent.is_symlink()
                for parent in (home, home / ".omnigent", bridge.parent, bridge, path)
            ):
                return "symlink", None
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return "nonregular", None
                data = stream.read(524289)
            if len(data) > 524288:
                return "oversized", None
            return "parsed", json.loads(data)
        except FileNotFoundError:
            return "missing", None
        except (OSError, ValueError, RecursionError):
            return "unreadable_or_invalid", None

    relay_state, relay = record("tool_relay.json")
    tools = relay.get("tools") if isinstance(relay, dict) else None
    inspected = tools[:128] if isinstance(tools, list) else []
    startup_state, startup = record("mcp_startup.json")
    startup_servers = startup.get("servers") if isinstance(startup, dict) else None
    if startup_state == "parsed" and not isinstance(startup_servers, dict):
        startup_state = "unreadable_or_invalid"
    return {
        "requests": request_metadata,
        "requests_omitted": max(0, len(requests) - 8),
        "descriptor_slots_used": 64 - remaining,
        "relay": {
            "state": relay_state,
            "tools_shape": shape(tools),
            "count": len(tools) if isinstance(tools, list) else None,
            "omitted": max(0, len(tools) - 128) if isinstance(tools, list) else 0,
            "controls_in_inspected_prefix": {
                control: any(
                    isinstance(tool, dict) and tool.get("name") == control for tool in inspected
                )
                for control in controls
            },
        },
        "startup_observer_cache": {
            "state": startup_state,
            "individual_success_not_implied_by_settlement": True,
            "servers": {
                server: status
                if status in ("starting", "ready", "failed", "cancelled")
                else "unknown"
                for server in servers
                if isinstance(startup_servers, dict)
                and isinstance(row := startup_servers.get(server), dict)
                and (status := row.get("status")) is not None
            },
        },
    }


@contextmanager
def _journey_server(
    tmp_path_factory: pytest.TempPathFactory, name: str, *, mcp_flags: bool
) -> Iterator[str]:
    env = {"PYTHONPATH": server_pythonpath(_REPO_ROOT)}
    if mcp_flags and compat_server_python() is None:
        env["OMNIGENT_FEATURES"] = (
            "mcp_disabled_servers,mcp_disabled_servers_claude,mcp_disabled_servers_codex"
        )
    with isolated_local_server(
        tmp_path_factory.mktemp(name),
        env=env,
        executable=server_executable(),
        cwd=compat_server_cwd(),
        health_timeout=180.0,
    ) as base_url:
        yield base_url


@pytest.fixture(scope="module")
def mcp_enabled_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Keep MCP release flags local to this journey, preserving old-server compatibility."""
    with _journey_server(tmp_path_factory, "mcp_enabled_server", mcp_flags=True) as base_url:
        yield base_url


@pytest.fixture(scope="module")
def mcp_flagless_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A server without the MCP release flags, which strict mode does not need."""
    with _journey_server(tmp_path_factory, "mcp_flagless_server", mcp_flags=False) as base_url:
        yield base_url


@pytest.fixture
def mcp_journey_server(request: pytest.FixtureRequest, control: str) -> str:
    """Select the server once for both compatibility checks and the journey."""
    return request.getfixturevalue(
        "mcp_enabled_server" if control == "switch" else "mcp_flagless_server"
    )


@pytest.fixture
def server_version(mcp_journey_server: str) -> str:
    """Check compatibility against the same isolated server the journey uses."""
    return resolve_server_version(mcp_journey_server)


@pytest.mark.min_server_version("0.22.0.dev0")
@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "control",
    [
        pytest.param("switch", marks=pytest.mark.min_runner_version("0.22.0.dev0")),
        pytest.param("strict", marks=pytest.mark.min_runner_version("0.24.0.dev0")),
    ],
)
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_disabled_server_absent_from_real_native_session(
    mcp_journey_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    harness: str,
    control: str,
) -> None:
    if shutil.which(harness) is None:
        pytest.skip(f"needs the {harness} CLI")
    server = mcp_journey_server
    home = tmp_path / "host" / "home"
    home.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture = Path(__file__).resolve().parents[1] / "tools/fixtures/echo_stdio_mcp_server.py"
    # A visible control proves absence is selective, rather than failed MCP discovery.
    names = ("off_fixture", "on_fixture")
    config = {name: {"command": sys.executable, "args": [str(fixture)]} for name in names}
    if harness == "claude":
        (home / ".claude.json").write_text(json.dumps({"mcpServers": config}))
        if control == "strict":
            # Claude unions allowlists across settings sources; strict mode must still win.
            (home / ".claude").mkdir()
            (home / ".claude/settings.json").write_text(
                json.dumps({"allowedMcpServers": [{"serverName": "off_fixture"}]})
            )
    else:
        codex = home / ".codex"
        codex.mkdir()
        text = "\n".join(
            f"[mcp_servers.{name}]\ncommand={json.dumps(sys.executable)}\nargs={json.dumps([str(fixture)])}\n"
            for name in names
        )
        (codex / "config.toml").write_text(text)
        # Exercise Codex's project layer as well as its private user-home copy.
        (workspace / ".codex").mkdir()
        (workspace / ".codex/config.toml").write_text(text)
    model = f"mcp-{control}-{harness}-{tmp_path.name}"
    family = "anthropic" if harness == "claude" else "openai"
    provider = {
        "base_url": mock_llm_server_url if harness == "claude" else f"{mock_llm_server_url}/v1",
        "api_key": "mock-key",
        "models": {"default": model},
    }
    if harness == "codex":
        provider["wire_api"] = "responses"
    responses = [{"text": "MCP test complete."}]
    if harness == "claude":
        # Claude can send its first request while MCP servers are still connecting.
        responses.insert(
            0,
            {
                "tool_calls": [
                    {
                        "call_id": "wait_for_mcp",
                        "name": "WaitForMcpServers",
                        "arguments": "{}",
                    }
                ]
            },
        )
    configure_mock_llm(mock_llm_server_url, responses, key=model)
    host_config: dict[str, object] = {
        "providers": {"test": {"kind": "key", "default": [family], family: provider}}
    }
    if control == "strict":
        # The user-config allowlist needs no host disable state or server feature flag;
        # this server runs without the flags.
        host_config["mcp"] = {"inherit": False, "allow": ["on_fixture"]}
    with (
        httpx.Client(base_url=server, timeout=120) as client,
        isolated_host(
            tmp_path / "host",
            server,
            config=host_config,
            env={
                "CLAUDE_CONFIG_DIR": None,
                "CODEX_HOME": None,
                "CLAUDE_ENV_FILE": None,
                "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "OMNIGENT_WRAPPER_BYPASS",
            },
            ready=lambda host: host.wait_online(client, timeout=90),
        ) as host,
    ):
        if control == "switch":

            def mcp_inventory_ready() -> bool:
                inventory = client.get(f"/v1/hosts/{host.host_id}/mcp-servers", timeout=20)
                if inventory.status_code == 409:
                    return False
                assert inventory.status_code == 200, (
                    f"MCP inventory failed: {inventory.status_code}: {inventory.text}"
                )
                return set(names) <= {
                    server["name"]
                    for server in inventory.json()["mcp_servers"]
                    if server["harness"] == harness
                }

            # The DB can report online before the live tunnel is registered.
            poll_until(mcp_inventory_ready, timeout_s=30, interval_s=0.1)
            response = client.put(
                f"/v1/hosts/{host.host_id}/mcp-servers/enabled",
                json={
                    "harness": harness,
                    "server": "off_fixture",
                    "enabled": False,
                },
            )
            response.raise_for_status()
        session = create_native_session(
            client,
            server,
            harness=harness,
            metadata={
                "host_id": host.host_id,
                "workspace": str(workspace),
            },
            **({"model": model} if harness == "codex" else {}),
        )
        session_id = session["session_id"]
        try:
            send_user_text(client, session_id=session_id, text="Say MCP test complete.")
            tools: set[str] = set()
            requests: list[dict] = []

            def mcp_controls_advertised() -> bool:
                nonlocal tools, requests
                requests = get_mock_requests(mock_llm_server_url, key=model)
                tools = advertised_tool_names(requests)
                return all(
                    any(marker in name for name in tools) for marker in ("on_fixture", "omnigent")
                )

            try:
                try:
                    poll_until(mcp_controls_advertised, timeout_s=120)
                except AssertionError as exc:
                    try:
                        metadata = _mcp_failure_metadata(requests, host.home, session_id, harness)
                        with contextlib.suppress(OSError):
                            (tmp_path / "mcp-failure-metadata.json").write_text(
                                json.dumps(metadata)
                            )
                        exc.add_note(f"MCP controls never advertised: {metadata}")
                    except Exception:
                        exc.add_note("MCP failure metadata unavailable")
                    raise
            except BaseException as exc:
                pending = [exc]
                seen: set[int] = set()
                while pending:
                    error = pending.pop()
                    if id(error) in seen:
                        continue
                    seen.add(id(error))
                    traceback.clear_frames(error.__traceback__)
                    pending.extend(
                        item for item in (error.__cause__, error.__context__) if item is not None
                    )
                raise
            finally:
                # Pytest renders locals after both polling and later assertion failures.
                requests = []
            assert any("on_fixture" in name for name in tools), tools
            assert not any("off_fixture" in name for name in tools), tools
            assert any("omnigent" in name for name in tools), sorted(tools)
        finally:
            client.delete(f"/v1/sessions/{session_id}")
