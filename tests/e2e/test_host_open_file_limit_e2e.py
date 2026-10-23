"""A host daemon started under a low soft open-file limit raises it for its processes.

The inherited soft limit of 256 matches what macOS launchd gives GUI apps and the
processes they spawn. Children's limits are read only on Linux; on macOS host status
reports the daemon's recorded limit for them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.compat import apply_runner_env, runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_process_env
from tests._helpers.polling import poll_until

resource = pytest.importorskip("resource")

_INHERITED_SOFT_LIMIT = 256

_EXEC_UNDER_LOW_LIMIT = f"""\
import os, resource, sys
hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
resource.setrlimit(resource.RLIMIT_NOFILE, ({_INHERITED_SOFT_LIMIT}, hard))
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
"""


@pytest.mark.min_runner_version("0.20.0")
def test_host_raises_inherited_open_file_limit_and_reports_descriptor_use(
    live_server: str, http_client: httpx.Client, tmp_path: Path
) -> None:
    """``host status --json`` reports the daemon and its children above the inherited limit."""
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard <= _INHERITED_SOFT_LIMIT:
        pytest.fail(f"hard open-file limit {hard} leaves no room to raise the soft limit")
    command = [
        runner_executable(),
        "-c",
        _EXEC_UNDER_LOW_LIMIT,
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
