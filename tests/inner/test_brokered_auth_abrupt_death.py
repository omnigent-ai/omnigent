"""Real-process abrupt runner death acceptance tests."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from omnigent.inner._proc import process_alive


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_sigkill_runner_reaps_signer_worker_helpers_and_socket(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "if os.fork() == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    while True: time.sleep(1)\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "state.json"
    runner = tmp_path / "runner.py"
    runner.write_text(
        """
import asyncio
import json
import os
import psutil
import sys
from pathlib import Path

from omnigent.inner import _liveness_exec, _proc
from omnigent.inner.model_egress import FrozenModelRoute
from omnigent.inner.model_signer import SignerLaunchConfig, SubprocessModelSigner

async def main():
    state_path, worker_path = map(Path, sys.argv[1:])
    signer = SubprocessModelSigner(SignerLaunchConfig(
        binding_id="test-fake-provider-v1",
        endpoint="https://model.test/v1",
        routes=(FrozenModelRoute("POST", "model.test", "/v1/responses"),),
    ))
    readiness = await signer.start()
    read_fd, write_fd = os.pipe()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(_liveness_exec.__file__)),
        "--liveness-fd",
        str(read_fd),
        sys.executable,
        str(worker_path),
        pass_fds=(read_fd,),
        **_proc.spawn_kwargs(),
    )
    _proc.remember_process_group(proc)
    os.close(read_fd)
    await asyncio.sleep(0.3)
    pids = [child.pid for child in psutil.Process().children(recursive=True)]
    state_path.write_text(json.dumps({
        "pids": pids,
        "socket": str(readiness.socket_path),
        "public_dir": str(readiness.socket_path.parent),
    }))
    while True:
        await asyncio.sleep(3600)

asyncio.run(main())
""".strip()
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(runner), str(state_path), str(worker)],
        start_new_session=True,
    )
    descendant_pids: list[int] = []
    try:
        deadline = time.monotonic() + 15
        while not state_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert state_path.exists()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        descendant_pids = [int(pid) for pid in state["pids"]]
        assert len(descendant_pids) >= 4

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (
            any(process_alive(pid) for pid in descendant_pids) or Path(state["socket"]).exists()
        ):
            time.sleep(0.05)

        survivors = [pid for pid in descendant_pids if process_alive(pid)]
        details = []
        for pid in survivors:
            with contextlib.suppress(psutil.Error):
                survivor = psutil.Process(pid)
                details.append((pid, survivor.ppid(), survivor.status(), survivor.cmdline()))
        assert not survivors, details
        assert not Path(state["socket"]).exists()
        assert not Path(state["public_dir"]).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for pid in descendant_pids:
            if process_alive(pid):
                with contextlib.suppress(psutil.Error):
                    child = psutil.Process(pid)
                    child.kill()
                    child.wait(timeout=3)
