"""Read-only native input probes; terminal existence alone is not readiness."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx


def _directory(env: Mapping[str, str], key: str) -> Path | None:
    value = env.get(f"HARNESS_{key.upper()}_NATIVE_BRIDGE_DIR")
    return Path(value) if value else None


def _pane(bridge_dir: Path) -> tuple[str, str] | None:
    try:
        info = json.loads((bridge_dir / "tmux.json").read_text())
        socket, target = info["socket_path"], info["tmux_target"]
        if not isinstance(socket, str) or not isinstance(target, str):
            return None
        alive = subprocess.run(
            ["tmux", "-S", socket, "display-message", "-p", "-t", target, "#{pane_dead}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if alive.returncode != 0 or alive.stdout.strip() != "0":
            return None
        return socket, target
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


async def _check_pane(
    env: Mapping[str, str],
    key: str,
    capture: Callable[[str, str], str],
    predicate: Callable[[str], bool],
) -> bool:
    directory = _directory(env, key)
    if directory is None:
        return False

    def check() -> bool:
        pane = _pane(directory)
        return pane is not None and predicate(capture(*pane))

    return await asyncio.to_thread(check)


async def claude(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.claude_native.bridge import claude_pane_ready

    directory = _directory(env, "claude")
    if directory is None:
        return False
    return await asyncio.to_thread(
        lambda: _pane(directory) is not None and claude_pane_ready(directory)
    )


async def codex(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
    from omnigent.harnesses.codex_native.bridge import read_bridge_state

    directory = _directory(env, "codex")
    state = await asyncio.to_thread(read_bridge_state, directory) if directory else None
    if state is None:
        return False
    client = CodexAppServerClient(socket_path=Path(state.socket_path))
    try:
        async with asyncio.timeout(3):
            await client.connect()
            response = await client.request(
                "thread/read", {"threadId": state.thread_id, "includeTurns": False}
            )
            result = response.get("result")
            thread = result.get("thread") if isinstance(result, dict) else None
            return isinstance(thread, dict) and thread.get("id") == state.thread_id
    finally:
        await client.close()


async def opencode(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.opencode_native.bridge import read_bridge_state

    directory = _directory(env, "opencode")
    state = await asyncio.to_thread(read_bridge_state, directory) if directory else None
    if state is None:
        return False
    async with httpx.AsyncClient(
        base_url=state.server_base_url, headers=state.auth_headers(), timeout=3
    ) as client:
        response = await client.get(f"/session/{state.opencode_session_id}")
        response.raise_for_status()
        payload = response.json()
        return isinstance(payload, dict) and payload.get("id") == state.opencode_session_id


async def qwen(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.qwen_native.bridge import (
        _events_file_has_system_event,
        events_file_path,
    )

    directory = _directory(env, "qwen")
    if directory is None:
        return False
    return await asyncio.to_thread(
        lambda: (
            _pane(directory) is not None
            and _events_file_has_system_event(events_file_path(directory))
        )
    )


async def cursor(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.cursor_native.bridge import _IDLE_MARKERS, _TRUST_MARKER, _capture_pane

    return await _check_pane(
        env,
        "cursor",
        _capture_pane,
        lambda pane: _TRUST_MARKER not in pane and any(m in pane for m in _IDLE_MARKERS),
    )


async def kimi(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.kimi_native.bridge import _capture_pane, _kimi_tui_ready

    return await _check_pane(env, "kimi", _capture_pane, _kimi_tui_ready)


async def kiro(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.kiro_native.bridge import _capture_pane, _kiro_input_ready

    return await _check_pane(env, "kiro", _capture_pane, _kiro_input_ready)


async def devin(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.devin_native.bridge import _capture_pane, devin_input_ready

    return await _check_pane(env, "devin", _capture_pane, devin_input_ready)


async def antigravity(env: Mapping[str, str]) -> bool:
    from omnigent.harnesses.antigravity_native.bridge import (
        _AGY_ACTIVE_MARKER,
        _AGY_IDLE_MARKER,
        _capture_pane,
    )

    return await _check_pane(
        env,
        "antigravity",
        _capture_pane,
        lambda pane: _AGY_IDLE_MARKER in pane or _AGY_ACTIVE_MARKER in pane,
    )


async def pi(env: Mapping[str, str]) -> bool:
    directory = _directory(env, "pi")
    if directory is None:
        return False

    def check() -> bool:
        try:
            heartbeat = json.loads((directory / "input-ready").read_text())
            pid, timestamp = heartbeat["pid"], heartbeat["at"]
            if not isinstance(pid, int) or pid <= 0 or not isinstance(timestamp, (int, float)):
                return False
            os.kill(pid, 0)
            return 0 <= time.time() - timestamp / 1000 <= 3
        except (OSError, ValueError, KeyError, TypeError):
            return False

    return await asyncio.to_thread(check)
