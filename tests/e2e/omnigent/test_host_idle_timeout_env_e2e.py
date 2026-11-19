"""E2E: idle-timeout knobs set in the host environment must reach host-spawned runners.

Journey: run the host with both idle knobs set to ``0``, start a claude-native
session on it, then read the spawned runner's log head: the pane reaper logs the
window it was armed with at boot, so a host that drops the knobs leaves it on
the 1-hour default.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import Host, isolated_host
from tests._helpers.live_server import isolated_local_server
from tests._helpers.native_session import create_native_session
from tests.e2e._harness_probes import cli_unavailable_reason

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="reads /proc/<pid>/environ to observe the spawned runner's environment",
)

_IDLE_KNOBS = {
    "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S": "0",
    "OMNIGENT_HARNESS_IDLE_TIMEOUT_S": "0",
}
_CONTROL = {"OMNIGENT_LOG_LEVEL": "INFO"}
_RUNNER_STARTED = re.compile(r"Runner started: (\S+) \(pid=(\d+)\)")
_REAPER_STARTED = re.compile(r"native pane reaper started \(idle_timeout=[^)]*\)")
_HOST_ONLINE_TIMEOUT_S = 90.0
_RUNNER_TIMEOUT_S = 60.0


def _wait_for(predicate, *, timeout: float, what: str, diagnostics):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"{what} did not appear within {timeout}s\n{diagnostics()}")


def _proc_environ(pid: int) -> dict[str, str]:
    env: dict[str, str] = {}
    for chunk in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if b"=" in chunk:
            key, _, value = chunk.partition(b"=")
            env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def _runner_log(host: Host) -> Path | None:
    logs = sorted((host.home / ".omnigent" / "logs" / "runner").glob("runner-*.log"))
    return logs[-1] if logs else None


# Hosts < 0.20.0 do not forward the idle-timeout env knobs to spawned runners.
@pytest.mark.min_runner_version("0.20.0")
@pytest.mark.parametrize("spawn", ["zygote", "direct"])
def test_host_env_idle_timeout_knobs_reach_spawned_runner(
    tmp_path: Path, mock_llm_server_url: str, spawn: str
) -> None:
    if (reason := cli_unavailable_reason("claude")) is not None:
        pytest.skip(reason)
    host_env: dict[str, str] = {**_IDLE_KNOBS, **_CONTROL}
    if spawn == "direct":
        # /proc shows exec-time environment; a zygote-forked runner never re-execs.
        host_env["OMNIGENT_RUNNER_ZYGOTE"] = "0"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with (
        isolated_local_server(tmp_path / "server") as base_url,
        isolated_host(
            tmp_path / "host",
            base_url,
            env=host_env,
            config={
                "providers": {
                    "mock-claude": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": mock_llm_server_url,
                            "api_key": "mock-key",
                            "models": {"default": "claude-sonnet-4-20250514"},
                        },
                    }
                }
            },
        ) as host,
        httpx.Client(base_url=base_url, timeout=30.0) as client,
    ):
        host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=0.5)
        host_environ = _proc_environ(host.proc.pid)
        assert {k: host_environ.get(k) for k in _IDLE_KNOBS} == _IDLE_KNOBS

        created = create_native_session(
            client,
            base_url,
            harness="claude",
            metadata={"host_id": host.host_id, "workspace": str(workspace)},
        )
        session_id = str(created["session_id"])
        try:
            # A crash/retry would spawn a second runner, so pair the PID with the
            # newest start line to match the lexicographically last runner log.
            started = _wait_for(
                lambda: (
                    (
                        matches := list(
                            _RUNNER_STARTED.finditer(host.log_path.read_text(errors="replace"))
                        )
                    )
                    and matches[-1]
                ),
                timeout=_RUNNER_TIMEOUT_S,
                what="host 'Runner started' line",
                diagnostics=lambda: host.log_path.read_text(errors="replace")[-3000:],
            )
            reaper_line = _wait_for(
                lambda: (
                    (log := _runner_log(host))
                    and _REAPER_STARTED.search(log.read_text(errors="replace"))
                ),
                timeout=_RUNNER_TIMEOUT_S,
                what="runner 'native pane reaper started' line",
                diagnostics=lambda: (
                    log.read_text(errors="replace")[-3000:]
                    if (log := _runner_log(host))
                    else "no runner log yet; host log tail:\n"
                    + host.log_path.read_text(errors="replace")[-3000:]
                ),
            ).group(0)

            runner_pid = int(started.group(2))
            if spawn == "zygote":
                assert re.search(
                    rf"Forked runner via zygote \(zygote pid=\d+, runner pid={runner_pid}\)",
                    host.log_path.read_text(errors="replace"),
                ), "runner fell back to direct spawning instead of exercising the zygote"
            if spawn == "direct":
                runner_environ = _proc_environ(runner_pid)
                assert runner_environ.get("OMNIGENT_LOG_LEVEL") == "INFO", (
                    "control variable missing: the host→runner env chain is broken"
                )
                assert {k: runner_environ.get(k) for k in _IDLE_KNOBS} == _IDLE_KNOBS, (
                    f"idle knobs dropped between host and runner; runner log: {reaper_line}"
                )
            assert "idle_timeout=0.0s" in reaper_line and "; DISABLED" in reaper_line, (
                f"host had OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S=0 but the runner armed its "
                f"pane reaper with the default: {reaper_line}"
            )
        finally:
            client.delete(f"/v1/sessions/{session_id}")
