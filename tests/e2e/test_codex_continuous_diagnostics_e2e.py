"""Exercise stderr -> runner logging -> local file and structured sink.

The source is a synthetic Codex subprocess; capture and delivery use production
code in an isolated runner process. No LLM credentials or live log sink needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

_CHILD = """
import sys
sys.stderr.write('startup diagnostic\\n')
sys.stderr.flush()
print('started', flush=True)
sys.stdin.readline()
sys.stderr.write('runtime diagnostic password=synthetic-secret\\n')
sys.stderr.flush()
print('runtime-written', flush=True)
sys.stdin.readline()
sys.stderr.write('final fragment')
"""

_RUNNER = """
import asyncio
import json
import logging
import sys
from pathlib import Path
from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
from omnigent.process_logging import configure_process_logging

root = Path(sys.argv[1])
def send(batch):
    with (root / 'rows.jsonl').open('a') as stream:
        for row in batch:
            stream.write(json.dumps(row) + '\\n')

configure_process_logging(
    'runner', log_path=root / 'runner.log', level=logging.INFO,
    log_to_stderr=False, debug_log_send=send,
)

async def main():
    server = CodexNativeAppServer(
        codex_path=sys.executable, socket_path=root / 'codex.sock',
        codex_home=root / 'codex-home', env={}, config_overrides=[],
        cwd=root, bridge_dir=root, session_id='codex-continuous-e2e', recent_stderr=[],
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(root / 'child.py'), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    server.proc = proc
    server.stderr_task = asyncio.create_task(server._stderr_loop())
    try:
        for _ in range(2):
            print((await proc.stdout.readline()).decode().strip(), flush=True)
            await asyncio.to_thread(sys.stdin.readline)
            proc.stdin.write(b'continue\\n')
            await proc.stdin.drain()
        await proc.wait()
    finally:
        await server.close()

asyncio.run(main())
"""


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    # The sender can be halfway through its next JSONL record.
    complete = path.read_text().split("\n")[:-1]
    return [json.loads(line) for line in complete]


async def _wait_for_text(path: Path, text: str) -> None:
    async with asyncio.timeout(10):
        while not any(text in row["attributes"].get("text", "") for row in _rows(path)):
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("enabled", [True, False])
async def test_codex_diagnostics_reach_local_and_structured_logs_during_runtime(
    tmp_path: Path, enabled: bool
) -> None:
    (tmp_path / "child.py").write_text(_CHILD)
    (tmp_path / "runner.py").write_text(_RUNNER)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(tmp_path / "runner.py"),
        str(tmp_path),
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "OMNIGENT_HARNESS_STDERR_ENABLED": "1" if enabled else "0",
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None
    rows_path = tmp_path / "rows.jsonl"
    try:
        assert await asyncio.wait_for(proc.stdout.readline(), 10) == b"started\n"
        if enabled:
            await _wait_for_text(rows_path, "startup diagnostic")
        proc.stdin.write(b"runtime\n")
        await proc.stdin.drain()
        assert await asyncio.wait_for(proc.stdout.readline(), 10) == b"runtime-written\n"
        if enabled:
            await _wait_for_text(rows_path, "runtime diagnostic")
            assert proc.returncode is None, "runtime logs must arrive before process exit"
        proc.stdin.write(b"stop\n")
        await proc.stdin.drain()
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
        assert proc.returncode == 0, (stdout.decode(), stderr.decode())
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()

    local = (tmp_path / "runner.log").read_text()
    rows = _rows(rows_path)
    assert "synthetic-secret" not in local + json.dumps(rows)
    if enabled:
        for marker in ("startup diagnostic", "runtime diagnostic", "final fragment"):
            assert marker in local
            assert any(marker in row["attributes"]["text"] for row in rows)
        for row in rows:
            assert row["source"] == "runner"
            assert row["session_id"] == "codex-continuous-e2e"
            assert row["event_name"] == "harness_diagnostic_output"
            assert row["attributes"]["source_kind"] == "codex_app_server_stderr"
    else:
        assert rows == []
        assert "diagnostic" not in local
