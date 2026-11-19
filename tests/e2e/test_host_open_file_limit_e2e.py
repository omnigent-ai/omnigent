"""A host started under a low inherited open-file limit gives its runner headroom."""

from __future__ import annotations

import contextlib
import json
import resource
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.compat import apply_runner_env, runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_process_env
from tests._helpers.polling import poll_until
from tests._helpers.session import bundle_files, post_session_bundle

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc/<pid>/limits; Linux only"
)

# launchd's default soft RLIMIT_NOFILE on macOS.
_INHERITED_SOFT_LIMIT = 256
_LOWER_LIMIT_THEN_EXEC = (
    "import os, resource, sys\n"
    "_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
    f"resource.setrlimit(resource.RLIMIT_NOFILE, ({_INHERITED_SOFT_LIMIT}, hard))\n"
    "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
)
_AGENT_YAML = b"""\
name: open_file_limit_probe
prompt: You run shell commands when asked.
executor:
  model: gpt-4o-mini
  harness: openai-agents
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _soft_open_file_limit(pid: int) -> float:
    for line in Path(f"/proc/{pid}/limits").read_text().splitlines():
        if line.startswith("Max open files"):
            soft = line.split()[3]
            return float("inf") if soft == "unlimited" else int(soft)
    raise AssertionError(f"/proc/{pid}/limits has no open-file limit")


def _child_processes(parent_pid: int) -> dict[int, str]:
    children = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            if ppid == parent_pid:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
                children[int(entry.name)] = cmdline.decode(errors="replace")
    return children


def _forked_runner_pids(daemon_pid: int) -> list[int]:
    zygotes = [
        pid
        for pid, cmdline in _child_processes(daemon_pid).items()
        if "omnigent.runner._zygote" in cmdline
    ]
    return sorted(pid for zygote in zygotes for pid in _child_processes(zygote))


@pytest.mark.min_runner_version("0.24.0")
@pytest.mark.timeout(300)
def test_host_runner_raises_low_inherited_open_file_limit(
    live_server: str, tmp_path: Path
) -> None:
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard <= _INHERITED_SOFT_LIMIT:
        pytest.skip(f"hard open-file limit {hard} leaves no headroom to raise")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = [
        runner_executable(),
        "-c",
        _LOWER_LIMIT_THEN_EXEC,
        "-m",
        "omnigent.host._daemon_entry",
        "--server",
        live_server,
    ]
    with (
        httpx.Client(base_url=live_server, trust_env=False, timeout=30) as client,
        isolated_host(tmp_path / "host", live_server, command=command) as host,
    ):
        host.wait_online(client, timeout=90, poll_interval=0.5)
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"open_file_limit_probe.yaml": _AGENT_YAML}),
            metadata={"workspace": str(workspace), "host_id": host.host_id},
            timeout=120.0,
        )
        assert created.status_code in (200, 201), created.text
        session_id = created.json()["session_id"]
        poll_until(
            lambda: client.get(f"/v1/sessions/{session_id}").json().get("runner_online"),
            timeout_s=120,
        )
        poll_until(lambda: _forked_runner_pids(host.proc.pid), timeout_s=30)

        limits = {"daemon": _soft_open_file_limit(host.proc.pid)}
        for pid in _forked_runner_pids(host.proc.pid):
            limits[f"zygote-forked pid {pid}"] = _soft_open_file_limit(pid)

    lowered = {role: soft for role, soft in limits.items() if soft <= _INHERITED_SOFT_LIMIT}
    assert not lowered, f"kept the inherited soft limit of {_INHERITED_SOFT_LIMIT}: {limits}"


@pytest.mark.min_runner_version("0.24.0")
def test_host_status_reports_raised_open_file_limits(
    live_server: str, http_client: httpx.Client, tmp_path: Path
) -> None:
    """``host status --json`` reports the daemon and its children above the inherited limit."""
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard <= _INHERITED_SOFT_LIMIT:
        pytest.skip(f"hard open-file limit {hard} leaves no headroom to raise")
    command = [
        runner_executable(),
        "-c",
        _LOWER_LIMIT_THEN_EXEC,
        "-m",
        "omnigent.host._daemon_entry",
        "--server",
        live_server,
    ]

    with isolated_host(tmp_path / "host", live_server, command=command) as host:
        host.wait_online(http_client, timeout=60)
        status_env = apply_runner_env(
            isolated_process_env(
                host.home, {"OMNIGENT_CONFIG_HOME": str(host.home / ".omnigent")}, None
            )
        )
        daemon: dict[str, Any] = {}

        def _zygote_listed() -> bool:
            status = subprocess.run(
                [runner_executable(), "-m", "omnigent", "host", "status", "--json"],
                env=status_env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert status.returncode == 0, status.stderr
            [latest] = json.loads(status.stdout)["daemons"]
            daemon.update(latest)
            return len(latest["open_files"]) > 1

        # The zygote starts in the background after the host reports online.
        try:
            poll_until(_zygote_listed, timeout_s=60)
        except AssertionError as exc:
            raise AssertionError(f"host status never listed a runner zygote: {daemon}") from exc

    open_files = daemon["open_files"]
    assert daemon["pid"] == host.proc.pid
    assert host.proc.pid in {entry["pid"] for entry in open_files}, open_files
    assert all(entry["limit"] > _INHERITED_SOFT_LIMIT for entry in open_files), open_files
    assert all(entry["count"] > 0 and entry["near_limit"] is False for entry in open_files)
