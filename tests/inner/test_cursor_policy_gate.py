"""Cursor native tools pass a protected, fail-closed gate before they run."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from omnigent.inner.cursor_executor import CursorExecutor, _CursorSessionState
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

_SHELL_REQUEST = json.dumps({"tool_name": "Shell", "tool_input": {"command": "printf hello"}})
_SHELL_CALL = ("PHASE_TOOL_CALL", {"name": "Shell", "arguments": {"command": "printf hello"}})

requires_policy_gate = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="Cursor isolates the filesystem only on Linux and macOS",
)


@pytest.fixture()
def home_and_workspace(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    """An owner-only home and a workspace beside it, on a path short enough for a Unix socket."""
    base = Path(tempfile.mkdtemp(prefix="cg-", dir="/tmp")).resolve()
    home = base / "home"
    home.mkdir(mode=0o700)
    workspace = base / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    try:
        yield home, workspace
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _verdict(action: str, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(action=action, reason=reason)


def _hook_command(gate_home: Path) -> str:
    config = json.loads((gate_home / ".cursor" / "hooks.json").read_text())
    return config["hooks"]["preToolUse"][0]["command"]


def _run_hook(argv: list[str], home: Path, request: str) -> dict[str, str]:
    completed = subprocess.run(
        argv,
        input=request,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
        env={"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    return json.loads(completed.stdout)


async def _ask_hook(gate_home: Path, request: str = _SHELL_REQUEST) -> dict[str, str]:
    argv = shlex.split(_hook_command(gate_home))
    return await asyncio.to_thread(_run_hook, argv, gate_home, request)


@requires_policy_gate
@pytest.mark.asyncio
async def test_hook_gates_native_tools_before_execution(
    home_and_workspace: tuple[Path, Path],
) -> None:
    _, workspace = home_and_workspace
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key")
    requests: list[tuple[str, dict[str, object]]] = []

    async def allow(phase: str, data: dict[str, object]) -> object:
        requests.append((phase, data))
        return _verdict("POLICY_ACTION_ALLOW")

    async def deny(_phase: str, _data: dict[str, object]) -> object:
        return _verdict("POLICY_ACTION_DENY", "blocked")

    executor._policy_evaluator = allow
    gate = await executor._start_policy_gate(str(workspace))
    try:
        hook = json.loads((gate.home / ".cursor" / "hooks.json").read_text())["hooks"][
            "preToolUse"
        ][0]
        assert hook["failClosed"] is True
        assert shlex.split(hook["command"])[0] == str(Path(sys.executable).resolve())
        assert stat.S_IMODE(gate.home.stat().st_mode) == 0o700
        assert stat.S_IMODE((gate.home / "policy.sock").stat().st_mode) == 0o600
        # Nothing the agent's tools can edit holds the hook.
        assert not (workspace / ".cursor").exists()

        assert await _ask_hook(gate.home) == {"permission": "allow"}
        assert requests == [_SHELL_CALL]

        executor._policy_evaluator = deny
        denied = await _ask_hook(gate.home)
        assert denied["permission"] == "deny" and "blocked" in denied["agent_message"]

        # A missing evaluator, a malformed request and a closed gate all deny.
        executor._policy_evaluator = None
        assert (await _ask_hook(gate.home))["permission"] == "deny"
        assert (await _ask_hook(gate.home, "not-json"))["permission"] == "deny"
        gate.server.close()
        await gate.server.wait_closed()
        assert (await _ask_hook(gate.home))["permission"] == "deny"
    finally:
        await gate.close()
    assert not gate.home.exists()


@requires_policy_gate
@pytest.mark.asyncio
async def test_hook_waits_for_interactive_approval(
    home_and_workspace: tuple[Path, Path],
) -> None:
    _, workspace = home_and_workspace
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key", permission_mode="ask")
    decisions = iter((False, True))

    async def allow(_phase: str, _data: dict[str, object]) -> object:
        return _verdict("POLICY_ACTION_ALLOW")

    async def approve(_name: str, _data: dict[str, object]) -> bool:
        return next(decisions)

    executor._policy_evaluator = allow
    executor._elicitation_handler = approve
    gate = await executor._start_policy_gate(str(workspace))
    try:
        assert (await _ask_hook(gate.home))["permission"] == "deny"
        assert await _ask_hook(gate.home) == {"permission": "allow"}
    finally:
        await gate.close()


@requires_policy_gate
@pytest.mark.asyncio
async def test_closing_the_gate_abandons_a_pending_approval(
    home_and_workspace: tuple[Path, Path],
) -> None:
    """Teardown returns while an approval is unanswered, and the waiting tool is denied."""
    _, workspace = home_and_workspace
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key", permission_mode="ask")
    parked = asyncio.Event()

    async def allow(_phase: str, _data: dict[str, object]) -> object:
        return _verdict("POLICY_ACTION_ALLOW")

    async def never_answers(_name: str, _data: dict[str, object]) -> bool:
        parked.set()
        await asyncio.Event().wait()
        return True

    executor._policy_evaluator = allow
    executor._elicitation_handler = never_answers
    gate = await executor._start_policy_gate(str(workspace))
    hook = asyncio.create_task(_ask_hook(gate.home))
    await asyncio.wait_for(parked.wait(), timeout=10)
    await asyncio.wait_for(gate.close(), timeout=5)
    assert (await asyncio.wait_for(hook, timeout=10))["permission"] == "deny"


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the macOS sandbox")
@pytest.mark.asyncio
async def test_sandboxed_process_cannot_use_the_gate_on_macos(
    home_and_workspace: tuple[Path, Path],
) -> None:
    """A process under a sandbox profile is refused even though it can reach the socket."""
    _, workspace = home_and_workspace
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key")
    requests: list[object] = []

    async def allow(_phase: str, data: dict[str, object]) -> object:
        requests.append(data)
        return _verdict("POLICY_ACTION_ALLOW")

    executor._policy_evaluator = allow
    gate = await executor._start_policy_gate(str(workspace))
    try:
        sandboxed = [
            "/usr/bin/sandbox-exec",
            "-p",
            "(version 1)(allow default)",
            *shlex.split(_hook_command(gate.home)),
        ]
        refused = await asyncio.to_thread(_run_hook, sandboxed, gate.home, _SHELL_REQUEST)
        assert refused["permission"] == "deny"
        assert requests == []
        assert await _ask_hook(gate.home) == {"permission": "allow"}
    finally:
        await gate.close()


@pytest.mark.skipif(sys.platform != "linux", reason="exercises Linux mount namespaces")
def test_peer_in_another_mount_namespace_is_refused_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = CursorExecutor(api_key="test-key")
    peer, other = socket.socketpair()
    try:
        writer = cast(asyncio.StreamWriter, SimpleNamespace(get_extra_info=lambda _name: peer))
        assert executor._trusted_policy_peer(writer)
        own_namespace = os.stat("/proc/self/ns/mnt")
        real_stat = os.stat

        def different_peer_namespace(path: str) -> object:
            if path == f"/proc/{os.getpid()}/ns/mnt":
                return SimpleNamespace(
                    st_dev=own_namespace.st_dev, st_ino=own_namespace.st_ino + 1
                )
            return real_stat(path)

        monkeypatch.setattr("omnigent.inner.cursor_executor.os.stat", different_peer_namespace)
        assert not executor._trusted_policy_peer(writer)
    finally:
        peer.close()
        other.close()


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], dict[str, object]]:
    """Replace the optional SDK's launch API; the real executor still owns the gate."""
    bridge: dict[str, object] = {}
    agent: dict[str, object] = {}

    class FakeClient:
        async def aclose(self) -> None:
            pass

    class FakeAgent:
        async def aclose(self) -> None:
            pass

        @staticmethod
        async def create(**kwargs: object) -> FakeAgent:
            agent.update(kwargs)
            return FakeAgent()

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            self.values = kwargs

    async def launch_bridge(**kwargs: object) -> FakeClient:
        bridge.update(kwargs)
        return FakeClient()

    sdk = ModuleType("cursor_sdk")
    sdk.__path__ = []  # type: ignore[attr-defined]
    sdk.AsyncAgent = FakeAgent  # type: ignore[attr-defined]
    sdk.AsyncClient = SimpleNamespace(launch_bridge=launch_bridge)  # type: ignore[attr-defined]
    sdk.LocalAgentOptions = FakeOptions  # type: ignore[attr-defined]
    sdk.CustomTool = object  # type: ignore[attr-defined]
    vendor = ModuleType("cursor_sdk._vendor")
    vendor.resolve_bridge_path = lambda: "/usr/bin/true"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cursor_sdk", sdk)
    monkeypatch.setitem(sys.modules, "cursor_sdk._vendor", vendor)
    return bridge, agent


@requires_policy_gate
@pytest.mark.asyncio
async def test_launch_uses_private_home_and_cursor_sandbox(
    home_and_workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, workspace = home_and_workspace
    bridge, agent = _install_fake_sdk(monkeypatch)
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key")
    state = _CursorSessionState()
    await executor._ensure_session(state, model="auto-smart", tools=[])
    try:
        assert state.policy_gate is not None
        command = bridge["command"]
        assert isinstance(command, list)
        # The bridge starts with an empty environment and the private home.
        assert command[:3] == ["/usr/bin/env", "-i", f"HOME={state.policy_gate.home}"]
        assert command[3] == "PATH=/usr/local/bin:/usr/bin:/bin"
        assert Path(str(bridge["state_root"])).is_relative_to(state.policy_gate.home)
        options = agent["local"].values  # type: ignore[attr-defined]
        assert options["sandbox_options"] == {"enabled": True}
        assert options["auto_review"] is False
        assert options["setting_sources"] == ["user"]
        assert not (workspace / ".cursor").exists()
    finally:
        await executor._close_state(state)
    assert state.policy_gate is None


@requires_policy_gate
@pytest.mark.asyncio
async def test_missing_api_key_fails_before_creating_the_policy_home(
    home_and_workspace: tuple[Path, Path],
) -> None:
    home, workspace = home_and_workspace
    executor = CursorExecutor(cwd=str(workspace), api_key="")
    executor._api_key = None
    with pytest.raises(RuntimeError, match="API key"):
        await executor._ensure_session(_CursorSessionState(), model="auto-smart", tools=[])
    assert not list(home.glob(".omnigent-cursor-*"))


@requires_policy_gate
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "sandbox", "message"),
    [
        ("sys_os_shell", OSEnvSandboxSpec(type="none"), "active sandbox"),
        ("sys_terminal_launch", None, "terminal tools"),
    ],
)
async def test_bridged_tools_that_could_reach_the_policy_home_are_rejected(
    tmp_path: Path, tool: str, sandbox: OSEnvSandboxSpec | None, message: str
) -> None:
    """Omnigent's own OS and terminal tools must not be able to edit the hook."""
    os_env = (
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=sandbox) if sandbox else None
    )
    executor = CursorExecutor(cwd=str(tmp_path), os_env=os_env, api_key="test-key")
    with pytest.raises(RuntimeError, match=message):
        await executor._ensure_session(
            _CursorSessionState(), model="auto-smart", tools=[{"name": tool}]
        )


@pytest.mark.asyncio
async def test_platform_without_cursor_isolation_has_no_policy_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.inner.cursor_executor.sys.platform", "win32")
    executor = CursorExecutor(api_key="test-key")
    with pytest.raises(RuntimeError, match="Linux or macOS"):
        await executor._start_policy_gate(os.getcwd())
