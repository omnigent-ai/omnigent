"""Disposable host and sleeper children for process-group shutdown tests.

Uses the production signal/notification and subprocess-spawn paths. No server,
harness, credentials, maintenance sweep, or real runner session is started.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from omnigent.host import connect, crash_reporting
from omnigent.host.connect import HostProcess, _RunnerHandle
from omnigent.host.identity import HostIdentity
from omnigent.host.runner_zygote import ZygoteManager
from omnigent.runner._zygote import (
    _ZYGOTE_TEST_CHILD_EXIT_ENV_VAR,
    _ZYGOTE_TEST_CHILD_SLEEP_ENV_VAR,
)


async def main() -> None:
    """Wait for a signal delivered to this disposable foreground group."""
    root = Path(sys.argv[1])
    mode, behavior = sys.argv[2:4]
    logging.basicConfig(level=logging.INFO)

    class ExitEvidence(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if getattr(record, "event_name", None) == "host_exiting":
                (root / "exit.json").write_text(json.dumps(record.attributes))

    logging.getLogger().addHandler(ExitEvidence())
    host = HostProcess(HostIdentity(host_id="a" * 32, name="signal-test"), "http://127.0.0.1:1")
    host._process_id = "disposable-process"
    host._connection_id = "disposable-connection"
    host._zygote = ZygoteManager(log_path=root / "zygote.log") if mode == "zygote" else None
    if behavior == "repeat":
        connect.SHUTDOWN_NOTIFY_TIMEOUT_S = 20
    if mode == "direct":
        real_popen = subprocess.Popen

        def sleeper_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
            assert args[-1] == "omnigent.runner._entry"
            code = "import time; time.sleep(60)"
            if behavior == "blocked_cleanup":
                code = (
                    "import signal, time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, lambda *_: "
                    f"Path({str(root / 'cleanup-waiting')!r}).touch()); "
                    f"Path({str(root / 'runner-ready')!r}).touch(); time.sleep(60)"
                )
            return real_popen([sys.executable, "-c", code], **kwargs)

        connect.subprocess.Popen = sleeper_popen
    env = dict(os.environ)
    env[_ZYGOTE_TEST_CHILD_EXIT_ENV_VAR] = "0"
    env[_ZYGOTE_TEST_CHILD_SLEEP_ENV_VAR] = "60"
    runner, log_path = await asyncio.to_thread(host._spawn_runner_proc, env, "signal-", root)
    host._runners["disposable-runner"] = _RunnerHandle(
        proc=runner, log_path=log_path, session_id="disposable-session"
    )
    runner_group = os.getpgid(runner.pid)

    class Peer:
        async def send(self, raw: str) -> None:
            frame = json.loads(raw)
            # Give every foreground-group member time to handle the signal.
            await asyncio.sleep(0.15)
            with (root / "frames.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"frame": frame, "runner_alive": runner.poll() is None}) + "\n"
                )
            if frame["kind"] == "host.shutdown" and behavior not in {"unresponsive", "repeat"}:
                host._shutdown_ack.set()

    host._ws = Peer()
    host._run_task = asyncio.current_task()
    restore = host._install_shutdown_handlers()
    try:
        if behavior == "blocked_cleanup":
            async with asyncio.timeout(5):
                while not (root / "runner-ready").exists():
                    await asyncio.sleep(0.01)
        if behavior == "prior_crash":
            runner.terminate()
            await asyncio.to_thread(runner.wait, timeout=5)
        (root / "ready.json").write_text(
            json.dumps(
                {
                    "host_pid": os.getpid(),
                    "host_group": os.getpgrp(),
                    "runner_pid": runner.pid,
                    "runner_group": runner_group,
                }
            )
        )
        if behavior == "blocked_loop":
            import time

            time.sleep(20)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
    finally:
        host._tearing_down = True
        if host._shutdown_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await host._shutdown_task
        host._cleanup_runners()
        if host._zygote is not None:
            host._zygote.stop()
        restore()
        (root / "stopped").touch()


if __name__ == "__main__":
    if sys.argv[3] == "ignored_hup":
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    original_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        asyncio.run(main())
        crash_reporting.await_signal_exit()
    except KeyboardInterrupt:
        assert all(signal.getsignal(sig) == handler for sig, handler in original_handlers.items())
        (Path(sys.argv[1]) / "handlers-restored").touch()
        (Path(sys.argv[1]) / "forced-interrupt").touch()
        raise SystemExit(130) from None
