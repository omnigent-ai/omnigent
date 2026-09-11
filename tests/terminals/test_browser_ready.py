"""Exercise startup color negotiation against a real private tmux server."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance, create_terminal_instance
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.util.terminal_browser_ready import BROWSER_READY_OPTION, browser_ready_commands
from tests.terminals.test_control_bridge import _FakeWebSocket, _kill_and_join, _kill_tmux

_PROBE = """
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

saved = termios.tcgetattr(0)
tty.setraw(0)
os.write(1, b"\\x1b]10;?\\x07\\x1b]11;?\\x07")
response = b""
deadline = time.monotonic() + 0.5
while time.monotonic() < deadline:
    readable, _, _ = select.select([0], [], [], max(0, deadline - time.monotonic()))
    if readable:
        response += os.read(0, 4096)
    if b"\\x1b]10;rgb:" in response and b"\\x1b]11;rgb:" in response:
        break
termios.tcsetattr(0, termios.TCSANOW, saved)
Path(sys.argv[1]).write_bytes(response)
time.sleep(30)
"""


class _InteractiveWebSocket(_FakeWebSocket):
    def __init__(self) -> None:
        super().__init__(inbound=[])
        self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def receive(self) -> dict[str, object]:
        return await self.incoming.get()

    def init(self, background: str) -> None:
        self.incoming.put_nowait(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {"type": "init", "foreground": "#18181b", "background": background}
                ),
            }
        )


async def _wait_for_result(path: Path) -> bytes:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.02)
    return path.read_bytes()


@pytest.fixture
async def waiting_terminal(tmp_path: Path) -> AsyncIterator[tuple[TerminalInstance, Path]]:
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    private_dir = Path(tempfile.mkdtemp(prefix="theme-test-"))
    result = tmp_path / "palette.bin"
    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=private_dir / "tmux.sock",
        private_dir=private_dir,
        command=sys.executable,
        args=["-c", _PROBE, str(result)],
        tmux_start_on_browser_ready=True,
    )
    try:
        await instance.launch()
        assert not result.exists()
        yield instance, result
    finally:
        await _kill_tmux(instance.socket_path)


@pytest.mark.parametrize("background", ["#ffffff", "#131517"])
async def test_startup_probe_sees_browser_palette_once(
    waiting_terminal: tuple[TerminalInstance, Path], background: str
) -> None:
    instance, result = waiting_terminal
    websocket = _InteractiveWebSocket()
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            websocket,  # type: ignore[arg-type]
            socket_path=str(instance.socket_path),
            tmux_target=instance.tmux_target,
            read_only=False,
        )
    )
    try:
        async with asyncio.timeout(1):
            while not websocket.sent:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        assert not result.exists(), "Attaching without browser readiness released the probe"
        websocket.init(background)
        websocket.init(background)
        response = await _wait_for_result(result)
        rgb = "/".join(background[index : index + 2] * 2 for index in (1, 3, 5))
        assert f"\x1b]11;rgb:{rgb}\x07".encode() in response
        assert b"\x1b]10;rgb:1818/1818/1b1b\x07" in response
        assert response.count(b"\x1b]11;") == 1
        assert (
            await instance._tmux_output("show-option", "-gv", BROWSER_READY_OPTION) == "started\n"
        )
    finally:
        await _kill_and_join(instance.socket_path, task)


async def test_no_browser_still_starts_headless(
    waiting_terminal: tuple[TerminalInstance, Path],
) -> None:
    instance, result = waiting_terminal
    await _wait_for_result(result)
    assert await instance._tmux_output("show-option", "-gv", BROWSER_READY_OPTION) == "started\n"


async def test_read_only_browser_cannot_set_palette_or_release_startup(
    waiting_terminal: tuple[TerminalInstance, Path],
) -> None:
    instance, result = waiting_terminal
    websocket = _InteractiveWebSocket()
    websocket.init("#ff0000")
    task = asyncio.create_task(
        bridge_tmux_control_to_websocket(
            websocket,  # type: ignore[arg-type]
            socket_path=str(instance.socket_path),
            tmux_target=instance.tmux_target,
            read_only=True,
        )
    )
    try:
        await asyncio.sleep(0.2)
        assert not result.exists()
        assert (
            await instance._tmux_output("show-option", "-gv", BROWSER_READY_OPTION) == "pending\n"
        )
        assert "ff0000" not in await instance._tmux_output("show-option", "-wv", "window-style")
    finally:
        await _kill_and_join(instance.socket_path, task)


@pytest.mark.parametrize(
    "color", [None, 0, {}, "white", "#fff", "#123456\nkill-server", "#123456'", "#123456;"]
)
def test_invalid_browser_color_cannot_become_tmux_syntax(color: object) -> None:
    assert browser_ready_commands("main", color, "#ffffff") is None
    assert browser_ready_commands("main", "#18181b", color) is None


def test_browser_ready_is_plumbed_from_runtime_spec(tmp_path: Path) -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    result = create_terminal_instance(
        name="codex",
        session_key="main",
        spec=TerminalEnvSpec(
            command="bash",
            os_env=OSEnvSpec(type="caller_process", cwd=str(tmp_path)),
            tmux_start_on_browser_ready=True,
        ),
    )
    try:
        assert result.instance.tmux_start_on_browser_ready is True
    finally:
        shutil.rmtree(result.instance.private_dir, ignore_errors=True)
