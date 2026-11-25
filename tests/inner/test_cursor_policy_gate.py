"""Cursor native tools pass a protected, fail-closed gate before they run."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from omnigent.inner import cursor_policy_gate_hook
from omnigent.inner.cursor_executor import CursorExecutor, _CursorSessionState
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from tests.inner.cursor_sdk_fake import install_fake_cursor_sdk

_SHELL_REQUEST = json.dumps({"tool_name": "Shell", "tool_input": {"command": "printf hello"}})
_SHELL_CALL = ("PHASE_TOOL_CALL", {"name": "Shell", "arguments": {"command": "printf hello"}})

_STDLIB = Path(os.__file__).resolve().parent

requires_policy_gate = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="Cursor isolates the filesystem only on Linux and macOS",
)


@pytest.fixture()
def home_and_workspace(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """An owner-only home and a workspace beside it, on a path short enough for a Unix socket."""
    base = short_tmp_parent.resolve()
    home = base / "home"
    home.mkdir(mode=0o700)
    workspace = base / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home, workspace


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
async def test_gate_works_under_a_home_too_long_for_a_socket_address(
    home_and_workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, workspace = home_and_workspace
    long_home = home / ("h" * 120)
    long_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(long_home))
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key")

    async def allow(_phase: str, _data: dict[str, object]) -> object:
        return _verdict("POLICY_ACTION_ALLOW")

    executor._policy_evaluator = allow
    launch_cwd = os.getcwd()
    gate = await executor._start_policy_gate(str(workspace))
    try:
        assert len(str(gate.home / "policy.sock")) > 108
        assert os.getcwd() == launch_cwd
        assert await _ask_hook(gate.home) == {"permission": "allow"}
    finally:
        await gate.close()


@requires_policy_gate
@pytest.mark.asyncio
async def test_workspace_holding_the_hooks_python_is_rejected(
    home_and_workspace: tuple[Path, Path],
) -> None:
    """Cursor's tools write the workspace, so it cannot hold what the hook imports."""
    home, _ = home_and_workspace
    executor = CursorExecutor(cwd=str(_STDLIB), api_key="test-key")
    with pytest.raises(RuntimeError, match="outside the workspace"):
        await executor._start_policy_gate(str(_STDLIB))
    assert not list(home.glob(".omnigent-cursor-*"))


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


def _other_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the gate's own user id differ from the hook process's."""
    own_uid = os.getuid()
    monkeypatch.setattr("omnigent.inner.cursor_executor.os.getuid", lambda: own_uid + 1)


def _failed_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_writer: object) -> bool:
        raise OSError("peer credentials unavailable")

    monkeypatch.setattr(CursorExecutor, "_trusted_policy_peer", staticmethod(fail))


@requires_policy_gate
@pytest.mark.asyncio
@pytest.mark.parametrize("untrusted_peer", [_other_user, _failed_lookup])
async def test_peer_that_cannot_be_identified_as_this_user_is_denied(
    home_and_workspace: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    untrusted_peer: Callable[[pytest.MonkeyPatch], None],
) -> None:
    _, workspace = home_and_workspace
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key")
    requests: list[object] = []

    async def allow(_phase: str, data: dict[str, object]) -> object:
        requests.append(data)
        return _verdict("POLICY_ACTION_ALLOW")

    executor._policy_evaluator = allow
    gate = await executor._start_policy_gate(str(workspace))
    try:
        with pytest.MonkeyPatch.context() as patched:
            untrusted_peer(patched)
            assert (await _ask_hook(gate.home))["permission"] == "deny"
        assert requests == []
    finally:
        await gate.close()


@requires_policy_gate
@pytest.mark.asyncio
async def test_unresolved_ask_is_denied_without_prompting(
    home_and_workspace: tuple[Path, Path],
) -> None:
    """The server resolves ASK before answering; one that reaches the gate is not approved here."""
    _, workspace = home_and_workspace
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key", permission_mode="ask")
    prompts: list[str] = []

    async def ask(_phase: str, _data: dict[str, object]) -> object:
        return _verdict("POLICY_ACTION_ASK", "needs approval")

    async def approve(name: str, _data: dict[str, object]) -> bool:
        prompts.append(name)
        return True

    executor._policy_evaluator = ask
    executor._elicitation_handler = approve
    gate = await executor._start_policy_gate(str(workspace))
    try:
        denied = await _ask_hook(gate.home)
        assert denied["permission"] == "deny" and "needs approval" in denied["agent_message"]
        assert prompts == []
    finally:
        await gate.close()


@pytest.mark.skipif(sys.platform == "win32", reason="the hook connects over a Unix socket")
@pytest.mark.parametrize(
    "reply",
    [
        b"not json\n",
        b'{"permission": "maybe"}\n',
        b'{"permission": "allow", "agent_message": 5}\n',
        # No line end: the gate went away mid-answer.
        b'{"permission": "allow"}',
        b'{"permission": "allow", "pad": "' + b"x" * (16 * 1024) + b'"}\n',
    ],
    ids=["malformed", "unknown-permission", "non-text-message", "unterminated", "oversized"],
)
def test_hook_denies_on_an_unusable_gate_reply(short_tmp_parent: Path, reply: bytes) -> None:
    socket_path = short_tmp_parent / "policy.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)

    def answer() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.makefile("rb").readline()
            connection.sendall(reply)

    server = threading.Thread(target=answer, daemon=True)
    server.start()
    try:
        hook = Path(cursor_policy_gate_hook.__file__)
        argv = [sys.executable, "-I", str(hook), str(socket_path)]
        assert _run_hook(argv, short_tmp_parent, _SHELL_REQUEST)["permission"] == "deny"
    finally:
        server.join(timeout=10)
        listener.close()


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


@requires_policy_gate
@pytest.mark.asyncio
async def test_launch_uses_private_home_and_cursor_sandbox(
    home_and_workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, workspace = home_and_workspace
    sdk = install_fake_cursor_sdk(monkeypatch)
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key")
    state = _CursorSessionState()
    await executor._ensure_session(state, model="auto-smart", tools=[])
    try:
        assert state.policy_gate is not None
        bridge = sdk["launch_kwargs"][-1]
        command = bridge["command"]
        assert isinstance(command, list)
        # The bridge starts with an empty environment and the private home.
        assert command[:3] == ["/usr/bin/env", "-i", f"HOME={state.policy_gate.home}"]
        assert command[3] == "PATH=/usr/local/bin:/usr/bin:/bin"
        assert Path(str(bridge["state_root"])).is_relative_to(state.policy_gate.home)
        options = sdk["local_options"][-1].options
        assert options["sandbox_options"] == {"enabled": True}
        assert options["auto_review"] is False
        assert options["setting_sources"] == ["user"]
        assert not (workspace / ".cursor").exists()
    finally:
        await executor._close_state(state)
    # Closing the session closes the bridge client and removes the private home.
    assert state.policy_gate is None
    assert sdk["client_closed"] == 1
    assert not list(home.glob(".omnigent-cursor-*"))


@requires_policy_gate
@pytest.mark.asyncio
async def test_failed_agent_creation_closes_the_client_and_removes_the_policy_home(
    home_and_workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, workspace = home_and_workspace
    sdk = install_fake_cursor_sdk(monkeypatch, create_exc=RuntimeError("agent creation failed"))
    executor = CursorExecutor(cwd=str(workspace), api_key="test-key")
    state = _CursorSessionState()
    with pytest.raises(RuntimeError, match="agent creation failed"):
        await executor._ensure_session(state, model="auto-smart", tools=[])
    assert state.agent is None and state.policy_gate is None
    assert sdk["client_closed"] == 1
    assert not list(home.glob(".omnigent-cursor-*"))


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
        ("sys_os_shell", OSEnvSandboxSpec(type="none"), "cannot isolate bridged OS tools"),
        ("sys_os_shell", None, "cannot isolate bridged OS tools"),
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
