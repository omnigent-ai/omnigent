"""Exercise native engine diagnostics through the real Codex process and exporter.

The model endpoint is an unreachable loopback address; no credentials or external
model call are needed. The installed Codex binary executes a real shell command.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
from pathlib import Path

import psutil
import pytest

_RUNNER = r"""
import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer
from omnigent.harnesses.codex_native.stderr_diagnostics import codex_app_server_diagnostic_env
from omnigent.process_logging import configure_process_logging

root = Path(sys.argv[1])
codex = sys.argv[2]
enabled = os.environ['OMNIGENT_HARNESS_STDERR_ENABLED'] == '1'

def send(batch):
    with (root / 'rows.jsonl').open('a') as stream:
        for row in batch:
            stream.write(json.dumps(row) + '\n')

configure_process_logging(
    'runner', log_path=root / 'runner.log', level=logging.INFO,
    log_to_stderr=False, debug_log_send=send,
)

async def main():
    home = root / 'codex-home'
    home.mkdir(mode=0o700)
    env = codex_app_server_diagnostic_env({
        'PATH': os.environ['PATH'], 'CODEX_HOME': str(home),
    })
    overrides = [
        'model_provider="diagnostic-probe"',
        'model_providers.diagnostic-probe={name="Offline",base_url="http://127.0.0.1:1",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}',
        'analytics.enabled=false',
        'feedback.enabled=false',
        'otel.metrics_exporter="none"',
        'check_for_update_on_startup=false',
    ]
    argv = [codex, 'app-server', '--listen', 'stdio://']
    for override in overrides:
        argv.extend(['-c', override])
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, cwd=root,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    server = CodexNativeAppServer(
        codex_path=codex, socket_path=root / 'unused.sock', codex_home=home,
        env=env, config_overrides=[], cwd=root, bridge_dir=root,
        session_id='native-diagnostics-e2e', recent_stderr=[],
    )
    server.proc = proc
    server.stderr_task = asyncio.create_task(server._stderr_loop())
    pending = {}
    failed_request = asyncio.Event()

    async def read_protocol():
        while line := await proc.stdout.readline():
            event = json.loads(line)
            request_id = event.get('id')
            if request_id in pending:
                pending.pop(request_id).set_result(event)
            elif event.get('method') == 'error':
                failed_request.set()

    reader = asyncio.create_task(read_protocol())
    next_id = 0

    async def request(method, params):
        nonlocal next_id
        next_id += 1
        future = asyncio.get_running_loop().create_future()
        pending[next_id] = future
        proc.stdin.write((json.dumps({
            'id': next_id, 'method': method, 'params': params,
        }) + '\n').encode())
        await proc.stdin.drain()
        result = await asyncio.wait_for(future, 15)
        assert 'error' not in result, result.get('error')
        return result['result']

    try:
        await request('initialize', {
            'clientInfo': {'name': 'omnigent-diagnostics-e2e', 'version': '1'},
            'capabilities': {'experimentalApi': True},
        })
        proc.stdin.write(b'{"method":"initialized","params":{}}\n')
        await proc.stdin.drain()
        thread = await request('thread/start', {
            'model': 'diagnostics-offline-model', 'cwd': str(root),
            'approvalPolicy': 'never', 'sandbox': 'read-only', 'ephemeral': True,
        })
        command = await request('command/exec', {
            'command': ['/usr/bin/printf', 'native-tool-ok\n'], 'cwd': str(root),
        })
        assert command['exitCode'] == 0, command
        assert 'native-tool-ok' in command['stdout'], command
        await request('turn/start', {
            'threadId': thread['thread']['id'],
            'input': [{'type': 'text', 'text': 'Offline diagnostic check.', 'text_elements': []}],
        })
        await asyncio.wait_for(failed_request.wait(), 15)
        if enabled:
            async with asyncio.timeout(10):
                while True:
                    path = root / 'rows.jsonl'
                    text = path.read_text() if path.exists() else ''
                    rows = [json.loads(line) for line in text.splitlines(keepends=True)
                            if line.endswith('\n')]
                    texts = [row['attributes'].get('text', '') for row in rows
                             if row['event_name'] == 'harness_diagnostic_output']
                    markers = ('codex_core::client', 'model=diagnostics-offline-model',
                               'http.method="POST"', 'responses')
                    has_span = any(all(marker in text for marker in markers) for text in texts)
                    has_error = any('http://127.0.0.1:1/responses' in text
                                    and 'request failed' in text.lower() for text in texts)
                    if has_span and has_error:
                        break
                    await asyncio.sleep(0.05)
        assert proc.returncode is None, 'diagnostics must arrive before native process exit'
        print('native-tool-and-runtime-error-observed', flush=True)
        if len(sys.argv) > 3 and sys.argv[3] == 'stall':
            await asyncio.Event().wait()
    finally:
        await server.close()
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader

asyncio.run(main())
"""


def _installed_codex() -> str:
    codex = shutil.which(os.environ.get("OMNIGENT_CODEX_PATH", "codex"))
    if codex is None:
        pytest.skip("installed Codex CLI required for native diagnostics e2e")
    return codex


async def _start_runner(
    tmp_path: Path, *, enabled: bool, stall: bool = False
) -> asyncio.subprocess.Process:
    codex = _installed_codex()
    runner = tmp_path / "runner.py"
    runner.write_text(_RUNNER)
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(runner),
        str(tmp_path),
        codex,
        *(["stall"] if stall else []),
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "OMNIGENT_HARNESS_STDERR_ENABLED": "1" if enabled else "0",
        },
        # Codex inherits this private group so a runner timeout reaps both.
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _stop_runner(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.communicate(), 5)
    except TimeoutError:
        # A slow graceful exit is followed by force-kill cleanup below.
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await asyncio.wait_for(proc.communicate(), 5)


@pytest.mark.posix_only
@pytest.mark.parametrize("enabled", [True, False])
async def test_real_codex_runtime_diagnostics_are_exported_before_exit(
    tmp_path: Path, enabled: bool
) -> None:
    proc = await _start_runner(tmp_path, enabled=enabled)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 60)
        assert proc.returncode == 0, (stdout.decode(), stderr.decode())
        assert b"native-tool-and-runtime-error-observed" in stdout
    finally:
        await _stop_runner(proc)

    rows_path = tmp_path / "rows.jsonl"
    rows = (
        [json.loads(line) for line in rows_path.read_text().splitlines()]
        if rows_path.exists()
        else []
    )
    diagnostics = [row for row in rows if row["event_name"] == "harness_diagnostic_output"]
    if not enabled:
        assert diagnostics == []
        return
    assert diagnostics
    markers = (
        "codex_core::client",
        "model=diagnostics-offline-model",
        'http.method="POST"',
        "responses",
    )
    assert any(
        all(marker in row["attributes"]["text"] for marker in markers) for row in diagnostics
    )
    assert any(
        "http://127.0.0.1:1/responses" in row["attributes"]["text"]
        and "request failed" in row["attributes"]["text"].lower()
        and any(
            target in row["attributes"]["text"]
            for target in ("codex_http_client::client", "codex_client::default_client")
        )
        for row in diagnostics
    )
    for row in diagnostics:
        assert row["session_id"] == "native-diagnostics-e2e"
        assert row["source"] == "runner"
        assert row["attributes"]["source_kind"] == "codex_app_server_stderr"
        assert len(row["attributes"]["text"].encode()) <= 65536


@pytest.mark.posix_only
async def test_real_codex_is_reaped_if_diagnostic_runner_times_out(tmp_path: Path) -> None:
    proc = await _start_runner(tmp_path, enabled=True, stall=True)
    children: list[psutil.Process] = []
    try:
        assert proc.stdout is not None
        ready = await asyncio.wait_for(proc.stdout.readline(), 45)
        assert b"native-tool-and-runtime-error-observed" in ready
        children = psutil.Process(proc.pid).children(recursive=True)
        assert children, "the real native process must still be alive"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(proc.communicate(), 0.05)
    finally:
        await _stop_runner(proc)
    _, alive = await asyncio.to_thread(psutil.wait_procs, children, timeout=5)
    assert not alive, "runner timeout must not leave a native app-server behind"
