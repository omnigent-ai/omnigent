"""Real subprocess boundary, using recorded Codex 0.163.0 output."""

import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import psutil
import pytest

from omnigent.mcp_disabled_servers import SubprocessCodexProbe
from tests._helpers.polling import poll_until_async


async def test_recorded_output_and_launch_context(tmp_path):
    fixture = Path(__file__).parent / "fixtures/mcp_disabled_servers/codex-0.163.0.json"
    script = (
        "import os,pathlib,sys; "
        "assert os.getcwd() == sys.argv[1]; "
        "assert os.environ['CODEX_HOME'] == sys.argv[1]; "
        "sys.stdout.write(pathlib.Path(sys.argv[2]).read_text())"
    )
    result = await SubprocessCodexProbe().effective_servers(
        [sys.executable, "-c", script, str(tmp_path), str(fixture.resolve())],
        env={"CODEX_HOME": str(tmp_path)},
        cwd=tmp_path,
        timeout_s=3,
    )
    assert result == {"fixture_http": True, "fixture_off": False, "fixture_stdio": True}


@pytest.mark.parametrize(
    "output", ["{", "{}", '[{"name":"missing_enabled"}]', '[{"name":"x","enabled":"false"}]']
)
async def test_invalid_output(tmp_path, output):
    with pytest.raises(ValueError):
        await SubprocessCodexProbe().effective_servers(
            [sys.executable, "-c", f"print({output!r})"], env={}, cwd=tmp_path, timeout_s=3
        )


async def test_nonzero_exit(tmp_path):
    with pytest.raises(ValueError):
        await SubprocessCodexProbe().effective_servers(
            [sys.executable, "-c", "raise SystemExit(1)"], env={}, cwd=tmp_path, timeout_s=3
        )


async def test_missing_binary(tmp_path):
    with pytest.raises(ValueError, match="Codex MCP list failed"):
        await SubprocessCodexProbe().effective_servers(
            [str(tmp_path / "missing")], env={}, cwd=tmp_path, timeout_s=3
        )


async def test_timeout_reaps_child(tmp_path):
    pid_file = tmp_path / "pid"
    script = (
        "import os,pathlib,time,sys; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    with pytest.raises(TimeoutError):
        await SubprocessCodexProbe().effective_servers(
            [sys.executable, "-c", script, str(pid_file)], env={}, cwd=tmp_path, timeout_s=0.3
        )
    pid = int(pid_file.read_text())
    with suppress(psutil.NoSuchProcess):
        assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.mark.parametrize("wrapper_exits", [False, True], ids=["live-wrapper", "exited-wrapper"])
async def test_timeout_reaps_pipe_holding_grandchild(tmp_path, wrapper_exits):
    pid_file = tmp_path / "grandchild.pid"
    child = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    wrapper = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]]); "
        + ("" if wrapper_exits else "time.sleep(30)")
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(TimeoutError):
            await SubprocessCodexProbe().effective_servers(
                [sys.executable, "-c", wrapper, str(pid_file)],
                env={},
                cwd=tmp_path,
                timeout_s=0.5,
            )
        assert loop.time() - started < 2
        pid = int(pid_file.read_text())

        def stopped():
            try:
                return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True

        await poll_until_async(stopped, timeout_s=1)
    finally:
        if pid_file.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


async def test_launcher_prefix_precedes_codex_arguments(tmp_path):
    script = (
        "import json,sys; "
        'assert sys.argv[1:] == ["-c", "base=true", "mcp", "list", "--json"]; '
        'print(json.dumps([{"name":"fixture","enabled":True}]))'
    )
    result = await SubprocessCodexProbe(launch_args=["-c", script]).effective_servers(
        [sys.executable, "-c", "base=true", "mcp", "list", "--json"],
        env={},
        cwd=tmp_path,
        timeout_s=3,
    )
    assert result == {"fixture": True}
