"""Lazy MCP discovery; raw configuration and transport diagnostics stay on the host."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from cachetools import TTLCache
from mcp.client.stdio import get_default_environment

from omnigent.host.mcp_inventory import ConfiguredMcpServer, configured_mcp_servers
from omnigent.host.mcp_login import (
    STATUS_TIMEOUT_SECONDS,
    SignInState,
    SubprocessGuard,
    harness_sign_in_state,
)
from omnigent.inner._proc import remember_process_group, spawn_kwargs
from omnigent.inner._subprocess_lifecycle import terminate_subprocess
from omnigent.runner.identity import strip_runner_auth_secrets
from omnigent.spec.types import MCPServerConfig

PROBE_TIMEOUT_SECONDS = 10.0
PROBE_CACHE_SECONDS = 300.0
# Short, so a sign-in made outside Omnigent shows up on the next expansion.
SIGN_IN_CACHE_SECONDS = 30
# Older servers stop waiting for a probe reply after 15 seconds.
RESPONSE_BUDGET_SECONDS = 13.0
# Room for terminating a status command that outlives its timeout.
_STATUS_CLEANUP_SECONDS = 1.0
MAX_TOOLS = 500
MAX_TOOL_NAME = 256
MAX_TOOL_DESCRIPTION = 300
_HTTP_ENV_VARS = (
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


class McpProbeResult(TypedDict):
    tools: list[dict[str, str | None]]
    connection: str
    truncated: bool
    # Whether the harness itself can sign in, or already has; only with ``needs_auth``.
    harness_sign_in: NotRequired[SignInState]


def _result(connection: str) -> McpProbeResult:
    return {"tools": [], "connection": connection, "truncated": False}


def _strings(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("invalid string mapping")
    return dict(value)


def _expand(value: str, harness: str, env: dict[str, str]) -> str:
    if harness == "codex":
        return value

    def replace(match: re.Match[str]) -> str:
        key = match[1]
        if harness == "cursor":
            if key == "userHome":
                return str(Path.home())
            if not key.startswith("env:"):
                raise ValueError("unsupported substitution")
            key = key[4:]
        name, separator, default = key.partition(":-")
        if name in env:
            return env[name]
        if separator:
            return default
        raise ValueError("unresolved substitution")

    return re.sub(r"\$\{([^}]+)\}", replace, value)


def _effective_config(server: ConfiguredMcpServer) -> tuple[MCPServerConfig, Path, str]:
    raw = server.config
    harness = server.summary["harness"]
    env = dict(os.environ)
    if server.plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(server.plugin_root)

    def expand(value: str) -> str:
        return _expand(value, harness, env)

    kind = raw.get("type", "auto")
    if kind not in {"auto", "stdio", "http", "streamable-http", "sse"}:
        raise ValueError("unsupported transport")
    cwd = Path.home()
    if harness == "codex" and "cwd" in raw:
        if not isinstance(raw["cwd"], str):
            raise ValueError("invalid working directory")
        cwd = cwd / Path(raw["cwd"]).expanduser()
    name = server.summary["name"]
    if server.summary["transport"] == "http":
        url = raw.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("missing URL")
        headers = {
            k: expand(v)
            for k, v in _strings(raw.get("headers", raw.get("http_headers", {}))).items()
        }
        if harness == "codex":
            for header, variable in _strings(raw.get("env_http_headers", {})).items():
                if variable not in env:
                    raise PermissionError("missing credential")
                headers[header] = env[variable]
            token_var = raw.get("bearer_token_env_var")
            if token_var is not None:
                if not isinstance(token_var, str) or not env.get(token_var):
                    raise PermissionError("missing credential")
                headers["Authorization"] = f"Bearer {env[token_var]}"
        config = MCPServerConfig(name=name, transport="http", url=expand(url), headers=headers)
    else:
        command = raw.get("command")
        args = raw.get("args", [])
        if (
            not isinstance(command, str)
            or not command
            or not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
        ):
            raise ValueError("invalid stdio configuration")
        inherited = (
            get_default_environment() if harness == "codex" else strip_runner_auth_secrets(env)
        )
        inherited = {k: v for k, v in inherited.items() if not k.startswith("OMNIGENT_")}
        if harness == "codex":
            variables = raw.get("env_vars", [])
            if not isinstance(variables, list) or not all(
                isinstance(var, str) for var in variables
            ):
                raise ValueError("invalid environment names")
            inherited.update({var: env[var] for var in variables if var in env})
        inherited.update({k: expand(v) for k, v in _strings(raw.get("env", {})).items()})
        config = MCPServerConfig(
            name=name,
            transport="stdio",
            command=expand(command),
            args=[expand(arg) for arg in args],
            env=inherited,
        )
    transport = (
        "sse"
        if kind == "sse"
        else "streamable-http"
        if kind in {"http", "streamable-http"}
        else "auto"
    )
    return config, cwd, transport


async def find_configured_server(
    harness: str, name: str, plugin: str | None = None, source_id: str | None = None
) -> ConfiguredMcpServer:
    """The one configured server a request names; ``LookupError`` if none or several."""
    servers = await asyncio.to_thread(configured_mcp_servers)
    matches = [
        s
        for s in servers
        if s.summary["harness"] == harness
        and (
            s.summary.get("source_id") == source_id
            if source_id is not None
            else (s.summary["name"], s.summary.get("plugin")) == (name, plugin)
        )
    ]
    if len(matches) != 1:
        raise LookupError("MCP server unavailable or ambiguous")
    return matches[0]


class HostMcpTools:
    def __init__(self, subprocess_guard: SubprocessGuard = contextlib.nullcontext) -> None:
        self._subprocess_guard = subprocess_guard
        self._slots = asyncio.Semaphore(2)
        self._cache: TTLCache[str, McpProbeResult] = TTLCache(maxsize=128, ttl=PROBE_CACHE_SECONDS)
        self._sign_in_slots = asyncio.Semaphore(2)
        self._sign_in_cache: TTLCache[tuple[str | None, ...], SignInState | None] = TTLCache(
            maxsize=128, ttl=SIGN_IN_CACHE_SECONDS
        )

    def clear_cache(self) -> None:
        """Forget probe results, e.g. after the user signs in to a server."""
        self._cache.clear()
        self._sign_in_cache.clear()

    async def probe(
        self, harness: str, name: str, plugin: str | None = None, source_id: str | None = None
    ) -> McpProbeResult:
        deadline = asyncio.get_running_loop().time() + RESPONSE_BUDGET_SECONDS
        server = await find_configured_server(harness, name, plugin, source_id)
        try:
            config, cwd, transport = _effective_config(server)
        except PermissionError:
            # A missing environment credential; signing in through the harness won't help.
            return _result("needs_auth")
        except (ValueError, TypeError):
            return _result("unsupported")
        result = await self._probe(config, cwd, transport)
        if result["connection"] == "needs_auth":
            key = (harness, name, plugin, source_id)
            sign_in = await self._sign_in_state(server, key, deadline)
            if sign_in is not None:
                return {**result, "harness_sign_in": sign_in}
        return result

    async def _sign_in_state(
        self, server: ConfiguredMcpServer, key: tuple[str | None, ...], deadline: float
    ) -> SignInState | None:
        """The harness's sign-in for *server*, or ``None`` if unknown by *deadline*."""
        missing = object()
        cached = self._sign_in_cache.get(key, missing)
        if cached is not missing:
            return cast("SignInState | None", cached)
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout_at(deadline - _STATUS_CLEANUP_SECONDS):
                await self._sign_in_slots.acquire()
        except TimeoutError:
            return None
        try:
            cached = self._sign_in_cache.get(key, missing)
            if cached is not missing:
                return cast("SignInState | None", cached)
            budget = deadline - _STATUS_CLEANUP_SECONDS - loop.time()
            if budget <= 0:
                return None
            with self._subprocess_guard():
                state = await harness_sign_in_state(
                    server, timeout=min(STATUS_TIMEOUT_SECONDS, budget)
                )
            # A shortened check may have guessed; let the next expansion ask again.
            if budget >= STATUS_TIMEOUT_SECONDS:
                self._sign_in_cache[key] = state
            return state
        finally:
            self._sign_in_slots.release()

    async def _probe(self, config: MCPServerConfig, cwd: Path, transport: str) -> McpProbeResult:
        payload = json.dumps(
            {
                "config": asdict(config),
                "cwd": str(cwd),
                "transport": transport,
                "network_env": {
                    key: os.environ[key]
                    for key in _HTTP_ENV_VARS
                    if config.transport == "http" and key in os.environ
                },
            },
            sort_keys=True,
        )
        key = hashlib.sha256(payload.encode()).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        attempted = False
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                async with self._slots:
                    cached = self._cache.get(key)
                    if cached is not None:
                        return cached
                    attempted = True
                    result = await _probe_worker(payload)
        except TimeoutError:
            if not attempted:
                raise BlockingIOError("MCP probe capacity exhausted") from None
            result = _result("timeout")
        self._cache[key] = result
        return result


async def _probe_worker(payload: str) -> McpProbeResult:
    # Transport libraries and stdio servers may log secrets. Isolate their output.
    env = get_default_environment()
    env.update(json.loads(payload).get("network_env", {}))
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-P",
        "-m",
        "omnigent.host.mcp_tools",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        # Escaped astral Unicode uses up to 12 bytes per capped character.
        limit=4 * 1024 * 1024,
        **spawn_kwargs(),
    )
    remember_process_group(process)
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(payload.encode() + b"\n")
        await process.stdin.drain()
        result = cast(McpProbeResult, json.loads(await process.stdout.readline()))
        remember_process_group(process)
        process.stdin.write(b"\n")
        await process.stdin.drain()
        await process.stdout.readline()
        return result
    finally:
        await terminate_subprocess(
            process, label="MCP probe", terminate_timeout=0.5, kill_timeout=0.5
        )


def _text(value: str, limit: int) -> str:
    return "".join(c if c.isprintable() else " " for c in value).strip()[:limit]


def _failure_status(exc: BaseException) -> str:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return "needs_auth"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            status = _failure_status(child)
            if status != "unreachable":
                return status
    return "unreachable"


async def _worker() -> None:
    from omnigent.tools.mcp import McpServerConnection

    payload = json.loads(sys.stdin.readline())
    connection = McpServerConnection(
        MCPServerConfig(**payload["config"]),
        cwd=Path(payload["cwd"]),
        http_transport=payload["transport"],
        discovery_limit=MAX_TOOLS + 1,
    )
    try:
        tools = await connection.connect()
        result: McpProbeResult = {
            "connection": "connected",
            "tools": [
                {
                    "name": _text(tool.name, MAX_TOOL_NAME),
                    "description": _text(tool.description, MAX_TOOL_DESCRIPTION)
                    if tool.description is not None
                    else None,
                }
                for tool in tools[:MAX_TOOLS]
            ],
            "truncated": len(tools) > MAX_TOOLS,
        }
    except Exception as exc:  # noqa: BLE001 — only the connection enum leaves this process
        result = _result(_failure_status(exc))
    print(json.dumps(result), flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    await connection.close()
    print("closed", flush=True)
    # Stay alive until the parent has reaped every descendant, including detached stdio groups.
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(_worker())
