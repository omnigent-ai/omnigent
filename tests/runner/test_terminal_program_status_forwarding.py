"""OSC reports reach an attached terminal through the real tmux boundary."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native.status_file import SessionStatusPoller
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals.registry import TerminalRegistry


def _report(state: str, app: str = "codex", extra: str = "") -> bytes:
    return f"\x1b]7501;state={state}:app={app}{extra}\x1b\\".encode()


def _wrapped(data: bytes) -> bytes:
    return b"\x1bPtmux;" + data.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"


@pytest.fixture
def tmux_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[str]:
    binary = shutil.which("tmux")
    if binary is None:
        pytest.skip("tmux is not installed")
    with tempfile.TemporaryDirectory(prefix="osc7501-", dir="/tmp") as temporary:
        root = Path(temporary)
        (root / "tmux").symlink_to(binary)
        monkeypatch.setenv("PATH", f"{root}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.delenv("TMUX", raising=False)
        # Keep Unix socket paths below the kernel limit, including on remote workers.
        monkeypatch.setattr(tempfile, "tempdir", temporary)
        yield binary


_CHILD = """
import os, pathlib, select, sys, time, tty
root = pathlib.Path(sys.argv[1])
root.joinpath('pid').write_text(str(os.getpid()))
tty.setraw(0)
while not (root / 'go').exists():
    time.sleep(0.01)
data = bytes.fromhex(sys.argv[2])
if sys.argv[3] == 'query':
    os.write(1, b'\\x1b]7501;?\\x1b\\\\')
    reply = b''
    deadline = time.monotonic() + 5
    while not reply.endswith(b'\\x1b\\\\') and time.monotonic() < deadline:
        if select.select([0], [], [], 0.1)[0]:
            reply += os.read(0, 4096)
    root.joinpath('reply').write_bytes(reply)
os.write(1, data)
while not (root / 'exit').exists():
    time.sleep(0.01)
"""


def _read_available(fd: int) -> bytes:
    output = bytearray()
    while select.select([fd], [], [], 0)[0]:
        try:
            data = os.read(fd, 65536)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not data:
            break
        output.extend(data)
    return bytes(output)


async def _launch_local_codex(
    registry: TerminalRegistry, resources: SessionResourceRegistry, root: Path, args: list[str]
) -> None:
    requests = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "terminal_codex_main"})

    async with httpx.AsyncClient(
        base_url="http://native.test", transport=httpx.MockTransport(capture)
    ) as client:
        await codex_native._launch_codex_terminal(
            client,
            "osc-test",
            codex_args=(),
            command="codex",
            thread_id=None,
            remote_url="ws://127.0.0.1:9876",
            env={},
        )
    body = requests[0]
    body["spec"].update(
        command=sys.executable, args=args, cwd=str(root), tmux_start_on_attach=False
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as server_client:
        app = create_runner_app(
            server_client=server_client, terminal_registry=registry, resource_registry=resources
        )
        async with httpx.AsyncClient(
            base_url="http://runner.test", transport=httpx.ASGITransport(app=app)
        ) as client:
            response = await client.post("/v1/sessions/osc-test/resources/terminals", json=body)
            assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "role,app,observe,ending",
    [
        ("codex-native", "codex", False, "exit"),
        ("local-codex", "codex", False, "exit"),
        ("claude-native", "claude-code", False, "exit"),
        ("claude-native", "claude-code", True, "exit"),
        ("claude-native", "claude-code", False, "clear"),
        ("codex-native", "codex", False, "foreign"),
        (None, "codex", False, "baseline"),
    ],
)
def test_real_tmux_forwards_native_reports_without_synthetic_cleanup(
    role: str | None,
    app: str,
    observe: bool,
    ending: str,
    tmux_environment: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OMNIGENT_FEATURES", "claude_osc_status" if observe else "-claude_osc_status"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    working = _report("working", app)
    blocked = _report("blocked", app, ":kind=question")
    clear = _report("clear", app)
    foreign = _report("working", "other-app")
    nested = _report("blocked", app, ":id=child:kind=permission")
    data = b"BEFORE" + working + b"MIDDLE" + foreign + nested + blocked
    if ending == "clear":
        data += clear
    if ending == "foreign":
        data = foreign + nested
    if ending == "baseline":
        data = b"BEFORE" + working + _wrapped(blocked)
    data += b"AFTER"

    async def exercise() -> None:
        registry = TerminalRegistry()
        resources = SessionResourceRegistry(terminal_registry=registry)
        master, slave = pty.openpty()
        attach = None
        try:
            command_args = ["-c", _CHILD, str(tmp_path), data.hex(), "query" if role else "plain"]
            if role == "local-codex":
                await _launch_local_codex(registry, resources, tmp_path, command_args)
            else:
                await resources.launch_required_terminal(
                    "osc-test",
                    "agent",
                    "main",
                    TerminalEnvSpec(
                        command=sys.executable,
                        args=command_args,
                        os_env=OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none")),
                        tmux_allow_passthrough=role is None,
                        keep_alive_after_exit=True,
                    ),
                    resource_role=role,
                )
            terminal = registry.get(
                "osc-test", "codex" if role == "local-codex" else "agent", "main"
            )
            assert terminal is not None
            assert (terminal.program_status is not None) == observe
            await terminal._tmux("set-option", "-w", "remain-on-exit", "on")
            attach = subprocess.Popen(
                [
                    tmux_environment,
                    "-S",
                    str(terminal.socket_path),
                    "attach",
                    "-t",
                    terminal.tmux_target,
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            output = bytearray()
            async with asyncio.timeout(10):
                while not output:
                    output.extend(_read_available(master))
                    await asyncio.sleep(0.01)
                (tmp_path / "go").touch()
                while b"AFTER" not in output:
                    output.extend(_read_available(master))
                    await asyncio.sleep(0.01)
            if role:
                child_pid = int((tmp_path / "pid").read_text())
                reply = (tmp_path / "reply").read_bytes()
                assert (
                    reply == b"\x1b]7501;?:states=idle,working,done,blocked,error:"
                    b"kinds=permission,question,auth\x1b\\"
                )
                assert b"\x1b]7501;?" not in output
                assert terminal.command_pid_sync() == child_pid
                # tmux redraws screen text separately from passthrough control strings.
                assert re.findall(rb"\x1b\]7501;[^\x1b]*\x1b\\", output) == re.findall(
                    rb"\x1b\]7501;[^\x1b]*\x1b\\", data
                )
                assert (b"AFTER" if ending == "foreign" else b"BEFOREMIDDLEAFTER") in output
                if observe:
                    assert terminal.program_status is not None
                    status = terminal.program_status.status
                    assert status is not None and status.state == "blocked"
                elif role == "claude-native":
                    sessions = tmp_path / "sessions"
                    sessions.mkdir()
                    (sessions / f"{child_pid}.json").write_text(
                        json.dumps(
                            {
                                "kind": "interactive",
                                "status": "waiting",
                                "waitingFor": "input needed",
                            }
                        )
                    )
                    statuses = []
                    poller = resources._build_claude_native_status_poller(
                        session_id="osc-test",
                        instance=terminal,
                        on_status=lambda status, reason: statuses.append((status, reason)),
                        on_failure=lambda reason: pytest.fail(reason),
                        on_replay=lambda status, reason: None,
                    )
                    assert isinstance(poller, SessionStatusPoller)
                    poller.tick()
                    assert poller.active
                    assert statuses == [("running", "input needed")]
            else:
                # allow-passthrough alone drops plain OSC, while DCS reaches the client.
                assert working not in output
                assert blocked in output
            (tmp_path / "exit").touch()
            async with asyncio.timeout(10):
                while (
                    await terminal._tmux_output("display-message", "-p", "#{pane_dead}")
                ).strip() != "1":
                    output.extend(_read_available(master))
                    await asyncio.sleep(0.01)
                output.extend(_read_available(master))
            if role:
                assert terminal.command_pid_sync() is None
                assert output.count(clear) == (1 if ending == "clear" else 0)
                assert _report("clear", "other-app") not in output
                assert b"state=clear\x1b" not in output
                assert re.findall(rb"\x1b\]7501;[^\x1b]*\x1b\\", output) == re.findall(
                    rb"\x1b\]7501;[^\x1b]*\x1b\\", data
                )
            assert not (terminal.private_dir / "program-status.json").exists()
        finally:
            if attach is not None:
                attach.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    attach.wait(timeout=5)
                if attach.poll() is None:
                    attach.kill()
                    attach.wait()
            os.close(master)
            os.close(slave)
            await registry.shutdown()

    asyncio.run(exercise())
