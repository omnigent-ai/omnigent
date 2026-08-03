"""Idle-CPU benchmarks for the two native-harness poll loops.

Measures the per-unit steady-state cost that shows up on a runner hosting
many concurrent native sessions:

``terminal``
    ``TerminalInstance._idle_watch_loop_threaded`` — one daemon thread per
    live terminal, each tick fork+exec'ing tmux. Reported as child CPU
    (``RUSAGE_CHILDREN``, i.e. the tmux processes) plus tmux exec count.

``forwarder``
    ``forward_claude_transcript_to_session`` — one asyncio task per live
    session, each tick re-reading the bridge state files and rescanning the
    transcript. Reported as self CPU (``RUSAGE_SELF``), all in-process.

Both scenarios hold everything *idle*: no turns, no pane output, no
transcript growth. That is the state the runner spends most of its life in,
and the state that should cost near zero.

Run::

    uv run python -m dev.benchmarks.native_poll_cpu terminal --terminals 8 --seconds 20
    uv run python -m dev.benchmarks.native_poll_cpu forwarder --sessions 8 --seconds 20

Add ``--fanout`` to the forwarder run to give each session sub-agent
transcripts (the fan-out shape from the issue reports).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Terminals and forwarders are exercised through their public entry points so
# the benchmark measures the shipped loop, not a re-implementation.
import omnigent.claude_native_bridge as bridge_mod
from omnigent.claude_native_bridge import prepare_bridge_dir, record_hook_event
from omnigent.claude_native_forwarder import forward_claude_transcript_to_session
from omnigent.inner.terminal import TerminalInstance


@dataclass
class CpuSample:
    """CPU accounting for one timed window.

    :param wall_s: Elapsed wall-clock seconds.
    :param self_s: User+system CPU seconds burned by this process.
    :param child_s: User+system CPU seconds burned by reaped children.
    """

    wall_s: float
    self_s: float
    child_s: float

    def report(self, *, units: int, label: str) -> str:
        """Render a one-line summary normalised per unit.

        :param units: Number of terminals / sessions under measurement.
        :param label: Unit noun for the report, e.g. ``"terminal"``.
        :returns: Formatted multi-line report.
        """
        total = self.self_s + self.child_s
        core = self.wall_s
        return "\n".join(
            [
                f"wall            {self.wall_s:8.2f}s",
                f"cpu self        {self.self_s:8.3f}s  ({self.self_s / core:6.2%} of one core)",
                f"cpu children    {self.child_s:8.3f}s  ({self.child_s / core:6.2%} of one core)",
                f"cpu total       {total:8.3f}s  ({total / core:6.2%} of one core)",
                f"per {label:<11} {total / max(units, 1) / core:8.4%} of one core",
            ]
        )


def _cpu_now() -> tuple[float, float]:
    """Return (self, children) CPU seconds consumed so far.

    :returns: Tuple of user+system seconds for this process and for
        reaped children.
    """
    me = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (me.ru_utime + me.ru_stime, kids.ru_utime + kids.ru_stime)


@contextmanager
def measure() -> Iterator[list[CpuSample]]:
    """Measure CPU across the ``with`` body.

    :yields: A single-element list that receives the :class:`CpuSample`
        once the body completes.
    """
    out: list[CpuSample] = []
    self_before, child_before = _cpu_now()
    wall_before = time.monotonic()
    yield out
    wall = time.monotonic() - wall_before
    self_after, child_after = _cpu_now()
    out.append(
        CpuSample(
            wall_s=wall,
            self_s=self_after - self_before,
            child_s=child_after - child_before,
        )
    )


# --------------------------------------------------------------------------
# Terminal idle-watcher scenario (#2702)
# --------------------------------------------------------------------------


class _ExecCounter:
    """Count tmux subprocess invocations made by the watcher threads."""

    def __init__(self) -> None:
        """Initialise the counter and install nothing yet."""
        self.count = 0
        self._lock = threading.Lock()
        self._original = subprocess.run

    def install(self) -> None:
        """Wrap :func:`subprocess.run` so every call is tallied."""

        def _counting_run(*args: object, **kwargs: object) -> object:
            with self._lock:
                self.count += 1
            return self._original(*args, **kwargs)  # type: ignore[arg-type]

        subprocess.run = _counting_run  # type: ignore[assignment]

    def restore(self) -> None:
        """Put the original :func:`subprocess.run` back."""
        subprocess.run = self._original  # type: ignore[assignment]


async def _bench_terminal(
    *, terminals: int, seconds: float, poll_interval: float, warmup: float
) -> None:
    """Run N idle terminals with status watchers and report CPU.

    :param terminals: How many tmux terminals to launch.
    :param seconds: Measurement window in seconds.
    :param poll_interval: Watcher poll interval; matches the claude-native
        status watcher's 0.2s by default.
    :param warmup: Seconds to run before measuring, so the window captures
        steady state rather than the settle period.
    :returns: None.
    """
    if shutil.which("tmux") is None:
        sys.exit("tmux not found on PATH — the terminal scenario needs it")

    root = Path(tempfile.mkdtemp(prefix="omnigent-bench-term-"))
    instances: list[TerminalInstance] = []
    try:
        for index in range(terminals):
            private = root / f"t{index}"
            private.mkdir(parents=True, exist_ok=True)
            instance = TerminalInstance(
                name="bench",
                session_key=f"t{index}",
                socket_path=private / "tmux.sock",
                private_dir=private,
                command="sleep",
                args=["3600"],
            )
            await instance.launch(cwd=private)
            instances.append(instance)

        # Let the panes settle so the measurement window is genuinely idle
        # (the first frames after launch still repaint).
        await asyncio.sleep(2.0)

        for instance in instances:
            instance.start_idle_watcher_thread(
                on_activity=lambda: None,
                on_idle=lambda: None,
                on_exit=lambda: None,
                idle_threshold_s=1.0,
                poll_interval_s=poll_interval,
            )
        # Let the watchers reach steady state before the window opens.
        await asyncio.sleep(warmup)

        counter = _ExecCounter()
        counter.install()
        try:
            with measure() as samples:
                await asyncio.sleep(seconds)
        finally:
            counter.restore()
            for instance in instances:
                instance._stop_idle_watcher_thread()

        sample = samples[0]
        print(f"scenario        terminal idle watcher (poll={poll_interval}s)")
        print(f"terminals       {terminals}")
        print(sample.report(units=terminals, label="terminal"))
        print(f"tmux invocations{counter.count:8d}  ({counter.count / sample.wall_s:.1f}/s)")
        print(
            "per terminal    "
            f"{counter.count / max(terminals, 1) / sample.wall_s:.2f} tmux invocations/s"
        )
    finally:
        for instance in instances:
            await instance.close()
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# Transcript forwarder scenario (#3000)
# --------------------------------------------------------------------------


class _SinkHandler(BaseHTTPRequestHandler):
    """Accept and discard every request the forwarder posts."""

    def log_message(self, format: str, *args: object) -> None:
        """Silence the stdlib access log.

        :param format: Unused format string.
        :param args: Unused format arguments.
        :returns: None.
        """
        del format, args

    def _respond(self, status: int) -> None:
        """Send an empty JSON response.

        :param status: HTTP status code to return.
        :returns: None.
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self) -> None:
        """Accept a posted event.

        :returns: None.
        """
        self._respond(202)

    def do_PATCH(self) -> None:
        """Accept a conversation patch.

        :returns: None.
        """
        self._respond(200)

    def do_GET(self) -> None:
        """Accept a read.

        :returns: None.
        """
        self._respond(200)


def _transcript_lines(turns: int) -> list[str]:
    """Build a plausible finished-session transcript.

    :param turns: How many user/assistant exchanges to synthesise.
    :returns: JSONL lines for the transcript file.
    """
    lines: list[str] = []
    for turn in range(turns):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"user-{turn}",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"question {turn}"}],
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": f"assistant-{turn}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "answer " * 40}],
                        "usage": {"input_tokens": 1200, "output_tokens": 300},
                    },
                }
            )
        )
    return lines


def _make_session(root: Path, index: int, *, fanout: int) -> tuple[Path, str]:
    """Create one idle bridge dir + transcript (+ sub-agent transcripts).

    :param root: Directory holding the synthetic Claude project tree.
    :param index: Session ordinal, used to build unique ids.
    :param fanout: Number of sub-agent transcripts to create.
    :returns: Tuple of (bridge_dir, omnigent session id).
    """
    session_id = f"conv_bench_{index}"
    workspace = root / f"ws{index}"
    workspace.mkdir(parents=True, exist_ok=True)
    bridge_dir = prepare_bridge_dir(
        session_id,
        bridge_id=f"bridge_bench_{index}",
        workspace=workspace,
    )

    claude_session = str(uuid.uuid4())
    project_dir = root / "projects" / f"proj{index}"
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript = project_dir / f"{claude_session}.jsonl"
    transcript.write_text("\n".join(_transcript_lines(12)) + "\n", encoding="utf-8")

    subagents = project_dir / claude_session / "subagents"
    if fanout:
        subagents.mkdir(parents=True, exist_ok=True)
        for agent in range(fanout):
            (subagents / f"agent-{agent}.jsonl").write_text(
                "\n".join(_transcript_lines(6)) + "\n", encoding="utf-8"
            )
            (subagents / f"agent-{agent}.meta.json").write_text(
                json.dumps({"agent_id": f"agent-{agent}", "description": "bench"}),
                encoding="utf-8",
            )

    # The rest of the bridge dir a live session accumulates. Without these the
    # directory is far smaller than in production, which understates every
    # per-tick cost that scales with how many files sit beside the transcript.
    for name, payload in (
        ("server.json", {"base_url": "http://127.0.0.1:6767"}),
        ("tool_relay.json", {"port": 8123}),
        ("tmux.json", {"socket_path": str(workspace / "tmux.sock"), "target": "main"}),
        ("permission_hook.json", {"enabled": True}),
    ):
        (bridge_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    (bridge_dir / "message_deltas.jsonl").write_text(
        json.dumps({"message_id": "m1", "text": "hi", "final": True}) + "\n",
        encoding="utf-8",
    )

    # A statusLine snapshot, so the model/cost scans have real input.
    (bridge_dir / "context.json").write_text(
        json.dumps(
            {
                "context_window_size": 200000,
                "model": {"display_name": "claude-opus-4-8"},
                "current_usage": {"input_tokens": 14400, "output_tokens": 3600},
                "cost": {"total_cost_usd": 0.42},
            }
        ),
        encoding="utf-8",
    )
    # Two hook events: a turn that ran, then stopped. This is the exact
    # steady state the issue reports — harness stopped, forwarder polling.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": claude_session,
            "transcript_path": str(transcript),
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": claude_session,
            "transcript_path": str(transcript),
        },
    )
    return bridge_dir, session_id


async def _bench_forwarder(
    *, sessions: int, seconds: float, fanout: int, poll_interval: float, warmup: float
) -> None:
    """Run N idle transcript forwarders and report CPU.

    :param sessions: How many forwarder tasks to run concurrently.
    :param seconds: Measurement window in seconds.
    :param fanout: Sub-agent transcripts per session.
    :param poll_interval: Forwarder poll interval in seconds.
    :param warmup: Seconds to run before measuring. Must exceed the gate's
        settle window, or the measurement captures the tail of the last
        activity burst instead of steady-state idle.
    :returns: None.
    """
    root = Path(tempfile.mkdtemp(prefix="omnigent-bench-fwd-"))
    # Point the bridge root at the scratch tree so nothing touches a real
    # ~/.omnigent or a live session's bridge dir.
    bridge_mod._TRUSTED_PARENT = root
    bridge_mod._BRIDGE_ROOT = root / "claude-native"
    bridge_mod._BRIDGE_ROOT_PARENT = root

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SinkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    tasks: list[asyncio.Task[None]] = []
    try:
        for index in range(sessions):
            bridge_dir, session_id = _make_session(root, index, fanout=fanout)
            tasks.append(
                asyncio.create_task(
                    forward_claude_transcript_to_session(
                        base_url=base_url,
                        headers={},
                        session_id=session_id,
                        bridge_dir=bridge_dir,
                        agent_name="claude-native-bench",
                        start_at_end=False,
                        poll_interval_s=poll_interval,
                    )
                )
            )

        # Warm-up: let the first polls drain the backlog so the measured
        # window is steady-state idle rather than initial catch-up.
        await asyncio.sleep(warmup)

        with measure() as samples:
            await asyncio.sleep(seconds)

        sample = samples[0]
        print(f"scenario        transcript forwarder (poll={poll_interval}s, fanout={fanout})")
        print(f"sessions        {sessions}")
        print(sample.report(units=sessions, label="session"))
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> None:
    """Parse args and run the requested scenario.

    :param argv: Command-line arguments, defaulting to ``sys.argv[1:]``.
    :returns: None.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="scenario", required=True)

    term = sub.add_parser("terminal", help="idle tmux watcher CPU (#2702)")
    term.add_argument("--terminals", type=int, default=8)
    term.add_argument("--seconds", type=float, default=20.0)
    term.add_argument("--poll-interval", type=float, default=0.2)
    term.add_argument("--warmup", type=float, default=6.0)

    fwd = sub.add_parser("forwarder", help="idle transcript forwarder CPU (#3000)")
    fwd.add_argument("--sessions", type=int, default=8)
    fwd.add_argument("--seconds", type=float, default=20.0)
    fwd.add_argument("--fanout", type=int, default=0)
    fwd.add_argument("--poll-interval", type=float, default=0.25)
    fwd.add_argument("--warmup", type=float, default=15.0)

    args = parser.parse_args(argv)
    print(f"python          {sys.version.split()[0]}  pid={os.getpid()}")
    if args.scenario == "terminal":
        asyncio.run(
            _bench_terminal(
                terminals=args.terminals,
                seconds=args.seconds,
                poll_interval=args.poll_interval,
                warmup=args.warmup,
            )
        )
    else:
        asyncio.run(
            _bench_forwarder(
                sessions=args.sessions,
                seconds=args.seconds,
                fanout=args.fanout,
                poll_interval=args.poll_interval,
                warmup=args.warmup,
            )
        )


if __name__ == "__main__":
    main()
