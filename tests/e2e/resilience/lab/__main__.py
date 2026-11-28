"""Start a resilience lab and inject faults by hand while using the web UI.

Usage::

    uv run --no-sync python -m tests.e2e.resilience.lab [--mode runner] [--front direct]

The lab creates one claude-native session and prints its web URL. Every
unscripted message gets a short mock reply; ``slow`` and ``hold`` script a
longer next turn. Type ``help`` at the prompt for fault commands.
"""

from __future__ import annotations

import argparse
import functools
import shlex
import threading
import time
from collections.abc import Callable

from tests.e2e.resilience.lab.lab import Lab, LabConfig
from tests.e2e.resilience.lab.model import MockModel
from tests.e2e.resilience.lab.proxy import Fault, FaultProxy

_HELP = """\
Faults (LINK is client, host or model; TAGS narrow it, e.g. runner.tunnel,host.tunnel,client.sse):
  blackhole LINK SECONDS [TAGS]   hold all bytes, like a half-open network
  refuse LINK SECONDS [TAGS]      refuse new connections
  delay LINK SECONDS MS [TAGS]    add latency per chunk
  reset LINK [TAGS]               RST live connections now
  close LINK [TAGS]               FIN live connections now
  restart-server SECONDS          stop the server, wait, start it (a deploy)
  crash-server SECONDS            SIGKILL the server, wait, start it
  sleep-host SECONDS              freeze host processes and drop their network
  kill-runner                     SIGKILL the runner
Model:
  slow SECONDS                    next message streams its reply over SECONDS
  hold                            next message's reply waits for `release`
  release                         release a held reply
Other:
  status | conns [LINK] | events [N] | help | quit"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--mode", choices=["host", "runner"], default="host")
    parser.add_argument("--front", choices=["ingress", "direct"], default="ingress")
    args = parser.parse_args()

    lab = Lab(LabConfig(mode=args.mode, front=args.front))
    print("starting lab...")
    lab.start()
    try:
        assert lab.model is not None
        lab.model.set_fallback("Mock reply from the resilience lab.")
        session_id = lab.create_claude_session()
        print(lab.describe())
        print(f"\nOpen the session: {lab.proxies.client.url}/c/{session_id}\n")
        print(_HELP)
        _console(lab, session_id)
    finally:
        lab.stop()
        print(f"lab stopped; artifacts kept in {lab.root}")


def _console(lab: Lab, session_id: str) -> None:
    console = _Console(lab, session_id)
    while True:
        try:
            line = input("lab> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            continue
        parts = shlex.split(line)
        if parts[0] in ("quit", "exit"):
            return
        handler = getattr(console, "do_" + parts[0].replace("-", "_"), None)
        if handler is None:
            print(f"unknown command {parts[0]!r}; try `help`")
            continue
        try:
            handler(parts[1:])
        except (IndexError, ValueError) as exc:
            print(f"error: {exc}; try `help`")


class _Console:
    """One method per console command; each receives the command's arguments."""

    def __init__(self, lab: Lab, session_id: str) -> None:
        self.lab = lab
        self.session_id = session_id
        self.proxies = {
            "client": lab.proxies.client,
            "host": lab.proxies.host,
            "model": lab.proxies.model,
        }

    def _link(self, name: str) -> FaultProxy:
        if name not in self.proxies:
            raise ValueError(f"unknown link {name!r}; use client, host or model")
        return self.proxies[name]

    @staticmethod
    def _tags(args: list[str], index: int) -> set[str] | None:
        return set(args[index].split(",")) if len(args) > index else None

    @staticmethod
    def _timed(fault: Fault, seconds: float) -> None:
        threading.Timer(seconds, fault.clear).start()
        print(f"{fault.description} for {seconds:g}s")

    @staticmethod
    def _in_background(action: Callable[[], None], label: str) -> None:
        def _run() -> None:
            action()
            print(f"\n{label} finished")

        threading.Thread(target=_run, daemon=True).start()
        print(f"{label} started")

    def do_help(self, args: list[str]) -> None:
        print(_HELP)

    def do_blackhole(self, args: list[str]) -> None:
        self._timed(self._link(args[0]).blackhole(self._tags(args, 2)), float(args[1]))

    def do_refuse(self, args: list[str]) -> None:
        self._timed(self._link(args[0]).refuse(self._tags(args, 2)), float(args[1]))

    def do_delay(self, args: list[str]) -> None:
        fault = self._link(args[0]).delay(float(args[2]) / 1000, self._tags(args, 3))
        self._timed(fault, float(args[1]))

    def do_reset(self, args: list[str]) -> None:
        print(f"reset {self._link(args[0]).reset(self._tags(args, 1))} connection(s)")

    def do_close(self, args: list[str]) -> None:
        print(f"closed {self._link(args[0]).close(self._tags(args, 1))} connection(s)")

    def do_restart_server(self, args: list[str], *, graceful: bool = True) -> None:
        action = functools.partial(
            self.lab.restart_server, downtime_s=float(args[0]), graceful=graceful
        )
        self._in_background(action, "restart-server" if graceful else "crash-server")

    def do_crash_server(self, args: list[str]) -> None:
        self.do_restart_server(args, graceful=False)

    def do_sleep_host(self, args: list[str]) -> None:
        self._in_background(functools.partial(self._sleep_host, float(args[0])), "sleep-host")

    def _sleep_host(self, seconds: float) -> None:
        with self.lab.sleep_host():
            time.sleep(seconds)

    def do_kill_runner(self, args: list[str]) -> None:
        print(f"killed {self.lab.kill_runner()}")

    def do_slow(self, args: list[str]) -> None:
        words = " ".join(f"word{i}" for i in range(40))
        self._model.reply(
            [{"text": words, "chunk_delay": float(args[0]) / 45}], required_tools=["Bash"]
        )
        print("next message streams slowly")

    def do_hold(self, args: list[str]) -> None:
        self._model.reply([{"text": "Released reply.", "block": True}], required_tools=["Bash"])
        print("next message's reply is held; `release` to let it finish")

    def do_release(self, args: list[str]) -> None:
        print("released" if self._model.release_gate() else "nothing held")

    def do_status(self, args: list[str]) -> None:
        snapshot = self.lab.snapshot(self.session_id)
        fields = ("status", "runner_online", "host_online")
        print(" ".join(f"{name}={snapshot.get(name)}" for name in fields))

    def do_conns(self, args: list[str]) -> None:
        for name in [args[0]] if args else list(self.proxies):
            for conn in self._link(name).connections():
                age = f"{conn.age_s:7.1f}s"
                print(f"{name:6} #{conn.id:<4} {conn.tag:14} {age} {conn.request_line[:70]}")

    def do_events(self, args: list[str]) -> None:
        for event in self.lab.events.events()[-(int(args[0]) if args else 20) :]:
            stamp = time.strftime("%H:%M:%S", time.localtime(event.wall))
            print(f"{stamp} {event.source:14} {event.kind:20} {event.fields}")

    @property
    def _model(self) -> MockModel:
        assert self.lab.model is not None
        return self.lab.model


if __name__ == "__main__":
    main()
