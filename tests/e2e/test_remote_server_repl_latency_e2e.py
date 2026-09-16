"""Remote-server REPL latency e2e.

Reported journey: from a machine far from the server region (e.g. India ->
US-East-1 for `isaac omni`), run ``omnigent run <agent> --server <url>``;
startup stalls for 60+ seconds before the REPL accepts input and turns feel
sluggish.

The suite drives that journey twice against the same live server: once at
loopback RTT (~0) and once through a TCP proxy that injects a fixed one-way
delay simulating the WAN RTT. Two properties are asserted:

- startup (spawn -> REPL prompt ready) finishes within an absolute budget at
  the simulated WAN RTT — the user-facing "how long until I can type";
- a steady-state turn's latency added by the RTT stays within a small
  sequential round-trip budget, so the per-turn path never becomes chatty
  (the delta between the two runs divided by the RTT is the round-trip depth).

Usage::

    python -m pytest tests/e2e/test_remote_server_repl_latency_e2e.py -v -s
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PROMPT_READY = "❯"

# Simulated one-way WAN delay. 100 ms each way ~= a 200 ms RTT, the ballpark
# of the India -> US-East-1 path named in the report.
_ONE_WAY_DELAY_S = 0.1
_RTT_S = 2 * _ONE_WAY_DELAY_S

# Absolute launch budget (spawn -> prompt ready) at the simulated WAN RTT,
# with a mock LLM and a trivial agent. A bounded launch is a couple of
# sequential round trips plus a few seconds of local process bring-up; the
# reported 60+ s startups (and the ~18 s reproduced even at loopback RTT)
# come from the serial host-daemon/runner/terminal launch pipeline, which is
# what this budget fails on.
_STARTUP_BUDGET_S = 10.0

# Sequential round-trip budget for one steady-state turn (send -> reply
# rendered). Keeps the per-turn client<->server path from becoming chatty,
# which would hit far-from-server users hardest.
_TURN_ROUND_TRIP_BUDGET = 12

# Absolute jitter allowance on top of the RTT-proportional turn budget so
# same-machine scheduling noise cannot fail the comparison by itself.
_JITTER_S = 3.0

_LAUNCH_TIMEOUT_S = 240.0
_TURN_TIMEOUT_S = 120.0

_REQUEST_LINE_RE = re.compile(rb"(?:^|\r\n)(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD) /")


def _strip_ansi(text: str) -> str:
    """
    Remove ANSI escape sequences before substring search.

    :param text: Raw PTY output.
    :returns: Plain text.
    """
    return _ANSI_RE.sub("", text)


class _ConnCounter:
    """Incremental HTTP request-line counter for one proxied connection."""

    def __init__(self, proxy: "LatencyProxy") -> None:
        self._proxy = proxy
        self._residual = b""
        self._offset = 0
        self._counted_upto = 0
        self._upgraded = False

    def feed(self, data: bytes) -> None:
        """
        Count request lines in the next client->server chunk.

        :param data: Raw bytes read from the client socket.
        :returns: None.
        """
        if self._upgraded or not data:
            return
        buf = self._residual + data
        base = self._offset
        for match in _REQUEST_LINE_RE.finditer(buf):
            absolute = base + match.start()
            if absolute >= self._counted_upto:
                self._proxy.record_request()
                self._counted_upto = absolute + 1
        if b"Upgrade: websocket" in buf or b"upgrade: websocket" in buf:
            # Tunnel frames after the WS handshake are opaque; method-looking
            # bytes inside them must not inflate the request count.
            self._upgraded = True
        keep = min(len(buf), 16)
        self._residual = buf[len(buf) - keep :]
        self._offset = base + len(buf) - keep


class LatencyProxy:
    """TCP proxy that injects a fixed one-way delay in both directions."""

    def __init__(self, upstream_host: str, upstream_port: int, one_way_delay_s: float) -> None:
        self._upstream = (upstream_host, upstream_port)
        self._delay = one_way_delay_s
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.request_count = 0
        self.connection_count = 0
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port = self._listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def record_request(self) -> None:
        """
        Record one client->server HTTP request.

        :returns: None.
        """
        with self._lock:
            self.request_count += 1

    def snapshot(self) -> tuple[int, int]:
        """
        Return the current (requests, connections) counters.

        :returns: Counter pair.
        """
        with self._lock:
            return self.request_count, self.connection_count

    def stop(self) -> None:
        """
        Stop accepting connections and release the listener.

        :returns: None.
        """
        self._stopping.set()
        with contextlib.suppress(OSError):
            self._listener.close()

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            try:
                server = socket.create_connection(self._upstream, timeout=10)
            except OSError:
                client.close()
                continue
            with self._lock:
                self.connection_count += 1
            counter = _ConnCounter(self)
            self._pump(client, server, counter)
            self._pump(server, client, None)

    def _pump(self, src: socket.socket, dst: socket.socket, counter: _ConnCounter | None) -> None:
        # Reader and writer are decoupled through a delivery queue so a burst
        # of chunks pays the one-way delay once, not once per chunk.
        chunks: queue.Queue[tuple[float, bytes]] = queue.Queue()

        def reader() -> None:
            while True:
                try:
                    data = src.recv(65536)
                except OSError:
                    data = b""
                if counter is not None:
                    counter.feed(data)
                chunks.put((time.monotonic() + self._delay, data))
                if not data:
                    return

        def writer() -> None:
            while True:
                deliver_at, data = chunks.get()
                delay = deliver_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if not data:
                    with contextlib.suppress(OSError):
                        dst.shutdown(socket.SHUT_WR)
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=writer, daemon=True).start()


@dataclass(frozen=True)
class JourneyTiming:
    """Timings and traffic counters for one remote-REPL journey."""

    prompt_ready_s: float
    first_reply_s: float
    second_reply_s: float
    startup_requests: int
    first_turn_requests: int
    second_turn_requests: int
    connections: int

    def as_dict(self) -> dict[str, Any]:
        """
        Serialize for the diagnostic printout.

        :returns: Plain dict of the fields.
        """
        return {
            "prompt_ready_s": round(self.prompt_ready_s, 2),
            "first_reply_s": round(self.first_reply_s, 2),
            "second_reply_s": round(self.second_reply_s, 2),
            "startup_requests": self.startup_requests,
            "first_turn_requests": self.first_turn_requests,
            "second_turn_requests": self.second_turn_requests,
            "connections": self.connections,
        }


def _write_probe_agent(directory: Path) -> Path:
    """
    Write a minimal single-model agent spec for the journey.

    :param directory: Existing temp directory to write into.
    :returns: Path to the agent YAML.
    """
    agent_yaml = directory / "latency-probe.yaml"
    agent_yaml.write_text(
        "name: latency-probe\n"
        "description: Minimal agent for the remote-REPL latency journey.\n"
        "executor:\n"
        "  model: gpt-4o\n"
        "prompt: |\n"
        "  You are a tiny probe agent. Reply briefly.\n"
    )
    return agent_yaml


def _build_repl_env(mock_llm_server_url: str, tmp_home: Path) -> dict[str, str]:
    """
    Build the pexpect environment for ``omnigent run`` with the mock LLM.

    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_home: Isolated HOME for this run.
    :returns: Env mapping for ``pexpect.spawn``.
    """
    sdk_paths = [
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        str(_REPO_ROOT),
    ]
    existing_pp = os.environ.get("PYTHONPATH", "")
    merged_pp = (
        os.pathsep.join([*sdk_paths, existing_pp]) if existing_pp else os.pathsep.join(sdk_paths)
    )
    config_home = tmp_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n",
    )
    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "HOME": str(tmp_home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "PYTHONPATH": merged_pp,
        "TERM": "xterm-256color",
        "LINES": "40",
        "COLUMNS": "120",
        "PROMPT_TOOLKIT_NO_CPR": "1",
    }
    for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE", "CLAUDECODE", "CODEX", "DATABRICKS_TOKEN"):
        env.pop(key, None)
    return env


def _await_reply(child: Any, reply_marker: str, timeout: float) -> float:
    """
    Wait until the REPL renders *reply_marker* and return the elapsed time.

    :param child: Live pexpect child.
    :param reply_marker: Unique assistant-reply substring to wait for.
    :param timeout: Seconds to wait before failing.
    :returns: Seconds from call to the marker rendering.
    """
    start = time.monotonic()
    deadline = start + timeout
    buffer = ""
    while time.monotonic() < deadline:
        try:
            buffer += child.read_nonblocking(size=65536, timeout=0.25)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
        if reply_marker in _strip_ansi(buffer):
            return time.monotonic() - start
    raise AssertionError(
        f"assistant reply {reply_marker!r} not rendered within {timeout}s; "
        f"tail: {_strip_ansi(buffer)[-800:]!r}"
    )


def _drive_remote_repl(
    server_url: str,
    proxy: LatencyProxy,
    mock_llm_server_url: str,
    tmp_dir: Path,
) -> JourneyTiming:
    """
    Run the reported journey once and measure each phase.

    Journey: ``omnigent run <agent> --server <url>`` -> wait for the REPL
    prompt -> send a message -> wait for the reply -> send a second message
    -> wait for its reply -> exit.

    :param server_url: Server URL the CLI should target (the proxy).
    :param proxy: The proxy fronting the live server, for traffic counters.
    :param mock_llm_server_url: Mock LLM base URL for the spawned runner.
    :param tmp_dir: Fresh directory for HOME + the agent spec.
    :returns: Measured timings and counters.
    """
    marker = uuid.uuid4().hex[:6]
    reply_one = f"PROBE-ONE-{marker}"
    reply_two = f"PROBE-TWO-{marker}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"{reply_one} done."}, {"text": f"{reply_two} done."}],
        key="default",
    )
    tmp_home = tmp_dir / "home"
    tmp_home.mkdir(parents=True, exist_ok=True)
    env = _build_repl_env(mock_llm_server_url, tmp_home)
    agent_yaml = _write_probe_agent(tmp_dir)

    start = time.monotonic()
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "run", str(agent_yaml), "--server", server_url],
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        timeout=_LAUNCH_TIMEOUT_S,
        dimensions=(40, 120),
    )
    try:
        child.expect(_PROMPT_READY, timeout=_LAUNCH_TIMEOUT_S)
        prompt_ready_s = time.monotonic() - start
        startup_requests, _ = proxy.snapshot()

        child.send("hello one")
        child.send("\r")
        first_reply_s = _await_reply(child, reply_one, _TURN_TIMEOUT_S)
        after_first, _ = proxy.snapshot()

        child.send("hello two")
        child.send("\r")
        second_reply_s = _await_reply(child, reply_two, _TURN_TIMEOUT_S)
        after_second, connections = proxy.snapshot()
    finally:
        with contextlib.suppress(Exception):
            child.sendcontrol("d")
            child.expect(pexpect.EOF, timeout=30)
        with contextlib.suppress(Exception):
            child.close(force=True)

    return JourneyTiming(
        prompt_ready_s=prompt_ready_s,
        first_reply_s=first_reply_s,
        second_reply_s=second_reply_s,
        startup_requests=startup_requests,
        first_turn_requests=after_first - startup_requests,
        second_turn_requests=after_second - after_first,
        connections=connections,
    )


@pytest.fixture(scope="module")
def latency_measurements(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, JourneyTiming]:
    """
    Measure the journey at loopback RTT and at the simulated WAN RTT.

    Both runs traverse an identical proxy hop so the only variable is the
    injected delay.

    :param live_server: Live e2e server base URL.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path_factory: Pytest temp factory.
    :returns: ``{"direct": ..., "delayed": ...}`` measurements.
    """
    parsed = urlparse(live_server)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80

    direct_proxy = LatencyProxy(host, port, one_way_delay_s=0.0)
    delayed_proxy = LatencyProxy(host, port, one_way_delay_s=_ONE_WAY_DELAY_S)
    try:
        direct = _drive_remote_repl(
            direct_proxy.url,
            direct_proxy,
            mock_llm_server_url,
            tmp_path_factory.mktemp("remote_latency_direct"),
        )
        delayed = _drive_remote_repl(
            delayed_proxy.url,
            delayed_proxy,
            mock_llm_server_url,
            tmp_path_factory.mktemp("remote_latency_delayed"),
        )
    finally:
        direct_proxy.stop()
        delayed_proxy.stop()

    print(
        "\nRemote-latency measurements (rtt=%.3fs): %s"
        % (
            _RTT_S,
            json.dumps({"direct": direct.as_dict(), "delayed": delayed.as_dict()}, indent=2),
        )
    )
    return {"direct": direct, "delayed": delayed}


@pytest.mark.timeout(900)
def test_remote_repl_startup_time_within_budget(
    latency_measurements: dict[str, JourneyTiming],
) -> None:
    """Launch reaches an input-ready REPL within the startup budget."""
    delayed = latency_measurements["delayed"]
    assert delayed.prompt_ready_s <= _STARTUP_BUDGET_S, (
        f"`omnigent run <agent> --server <url>` took {delayed.prompt_ready_s:.1f}s to reach an "
        f"input-ready REPL at a {_RTT_S * 1000:.0f}ms simulated WAN RTT with a mock LLM and a "
        f"trivial agent — budget is {_STARTUP_BUDGET_S:.0f}s. The launch pipeline (host daemon "
        f"spawn, runner bring-up polling, terminal attach) keeps users staring at "
        f"'Launching your agent…'; on real far-region setups this is the reported 60+s "
        f"startup"
    )


@pytest.mark.timeout(900)
def test_remote_repl_turn_round_trip_depth(
    latency_measurements: dict[str, JourneyTiming],
) -> None:
    """Steady-state turn latency added by WAN RTT stays within budget."""
    direct = latency_measurements["direct"]
    delayed = latency_measurements["delayed"]
    added = delayed.second_reply_s - direct.second_reply_s
    budget = _TURN_ROUND_TRIP_BUDGET * _RTT_S + _JITTER_S
    assert added <= budget, (
        f"steady-state turn added {added:.1f}s at a {_RTT_S * 1000:.0f}ms RTT "
        f"(~{added / _RTT_S:.0f} sequential round trips; "
        f"{delayed.second_turn_requests} requests observed during the turn) "
        f"— budget is {_TURN_ROUND_TRIP_BUDGET} round trips (+{_JITTER_S}s jitter). "
        f"Every message a far user sends stalls this long"
    )
