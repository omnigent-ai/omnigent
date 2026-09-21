"""Native readiness requires live input evidence, never just a launch result."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.harness_plugins import native_provider_for_key
from omnigent.native.native_dispatch import resolve_hook
from omnigent.runner.native import readiness


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "claude",
        "codex",
        "opencode",
        "pi",
        "qwen",
        "cursor",
        "kimi",
        "kiro",
        "devin",
        "antigravity",
    ],
)
async def test_missing_bridge_never_reports_ready(key: str) -> None:
    provider = native_provider_for_key(key)
    assert provider is not None
    probe = resolve_hook(provider, "input_ready")
    assert probe is not None
    assert await probe({}) is False


@pytest.mark.parametrize("key", ["goose", "hermes"])
def test_heuristic_only_harness_is_explicitly_unsupported(key: str) -> None:
    provider = native_provider_for_key(key)
    assert provider is not None
    assert resolve_hook(provider, "input_ready") is None


@pytest.mark.asyncio
async def test_codex_requires_live_thread_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.codex_native import app_server, bridge

    state = SimpleNamespace(socket_path="/unused.sock", thread_id="thread_readiness")
    monkeypatch.setattr(bridge, "read_bridge_state", lambda _: state)
    client = SimpleNamespace(
        connect=AsyncMock(),
        request=AsyncMock(return_value={"result": {"thread": {"id": state.thread_id}}}),
        close=AsyncMock(),
    )
    monkeypatch.setattr(app_server, "CodexAppServerClient", lambda **_: client)
    env = {"HARNESS_CODEX_NATIVE_BRIDGE_DIR": str(tmp_path)}
    assert await readiness.codex(env) is True
    client.request.assert_awaited_once_with(
        "thread/read", {"threadId": state.thread_id, "includeTurns": False}
    )
    client.close.assert_awaited_once()
    client.request.side_effect = RuntimeError("app-server unavailable")
    with pytest.raises(RuntimeError):
        await readiness.codex(env)
    assert client.close.await_count == 2


@pytest.mark.asyncio
async def test_pi_requires_fresh_live_input_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = {"HARNESS_PI_NATIVE_BRIDGE_DIR": str(tmp_path)}
    kill = Mock()
    monkeypatch.setattr(readiness.os, "kill", kill)
    assert await readiness.pi(env) is False
    (tmp_path / "input-ready").write_text(
        json.dumps({"pid": 123, "at": (time.time() - 30) * 1000})
    )
    assert await readiness.pi(env) is False
    (tmp_path / "input-ready").write_text(json.dumps({"pid": 123, "at": time.time() * 1000}))
    assert await readiness.pi(env) is True
    kill.side_effect = ProcessLookupError()
    assert await readiness.pi(env) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("live,ready", [(False, True), (True, False), (True, True)])
async def test_claude_requires_live_pane_and_mounted_prompt(
    live: bool,
    ready: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent.harnesses.claude_native import bridge

    monkeypatch.setattr(readiness, "_pane", lambda _: ("socket", "pane") if live else None)
    monkeypatch.setattr(bridge, "claude_pane_ready", lambda _: ready)
    assert await readiness.claude({"HARNESS_CLAUDE_NATIVE_BRIDGE_DIR": str(tmp_path)}) is (
        live and ready
    )


def test_dead_tmux_pane_is_not_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "tmux.json").write_text(
        json.dumps({"socket_path": "/socket", "tmux_target": "main"})
    )
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="1"))
    monkeypatch.setattr(readiness.subprocess, "run", run)
    assert readiness._pane(tmp_path) is None
