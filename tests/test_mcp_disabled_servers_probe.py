"""Real subprocess boundary, using recorded Codex 0.163.0 output."""

import os
import sys
from pathlib import Path

import pytest

from omnigent.mcp_disabled_servers import SubprocessCodexProbe


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
    with pytest.raises(OSError):
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
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


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
