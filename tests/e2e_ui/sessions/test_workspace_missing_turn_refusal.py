"""Exercise missing-workspace refusal through the browser and a real host.

After a host restart removes the workspace, the SPA must show the structured
error while server logs classify the expected refusal below ERROR level.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests.e2e_ui.conftest import _build_hello_world_bundle

_REPO_ROOT = Path(__file__).resolve().parents[3]

_WORKSPACE_MISSING_PREFIX = "workspace path does not exist"
_WORKSPACE_MISSING_HEADLINE = "The session workspace no longer exists on the host."

_POLL_S = 0.5


def _absolute_pythonpath() -> str:
    """Absolutize PYTHONPATH for runners started inside the workspace."""
    entries = [str(_REPO_ROOT), str(_REPO_ROOT / "sdks" / "python-client")]
    for raw in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if not raw:
            continue
        path = Path(raw)
        entries.append(str(path if path.is_absolute() else _REPO_ROOT / path))
    seen: set[str] = set()
    return os.pathsep.join(e for e in entries if not (e in seen or seen.add(e)))


def _spawn_host_daemon(
    home: Path, server_url: str, *, log_name: str = "host-daemon.log"
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn a host with a unique name and a stable ID across restarts.

    Reusing *home* preserves its ID when the test simulates a host reboot.
    """
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    config_path = omni_dir / "config.yaml"
    if config_path.exists():
        host_id = yaml.safe_load(config_path.read_text())["host"]["host_id"]
    else:
        host_id = uuid.uuid4().hex
        config_path.write_text(
            yaml.safe_dump(
                {"host": {"host_id": host_id, "name": f"e2e-ui-host-{host_id[:12]}"}},
                default_flow_style=False,
                sort_keys=True,
            )
        )
    daemon_log = home / log_name
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": _absolute_pythonpath(),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
        # Direct spawn makes the logged PID the runner we need to kill.
        "OMNIGENT_RUNNER_ZYGOTE": "0",
    }
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", server_url],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _wait_until(predicate, timeout_s: float, message: str) -> None:
    """Poll *predicate* until it succeeds or times out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_S)
    raise AssertionError(message)


def _launched_runner_pids(log_path: Path) -> list[int]:
    """Find every launched PID so a superseded runner cannot stay online."""
    if not log_path.exists():
        return []
    return [
        int(pid)
        for pid in re.findall(
            r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)",
            log_path.read_text(),
        )
    ]


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_missing_workspace_turn_shows_structured_error_without_error_funnel(
    page: Page,
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> None:
    """Keep the error card while logging the refusal below ERROR level."""
    if request.config.getoption("--ui-base-url"):
        pytest.skip("needs the suite-spawned server (host daemon + server log access)")

    workspace = tmp_path / "universe-worktree"
    workspace.mkdir()
    daemon, host_id, daemon_log = _spawn_host_daemon(tmp_path / "host-home", live_server)
    client = httpx.Client(base_url=live_server, timeout=30.0)
    session_id: str | None = None
    try:
        _wait_until(
            lambda: any(
                h["host_id"] == host_id and h["status"] == "online"
                for h in client.get("/v1/hosts").json().get("hosts", [])
            ),
            timeout_s=30.0,
            message=f"host {host_id} never came online for {live_server}",
        )

        import json as _json

        create_resp = client.post(
            "/v1/sessions",
            data={"metadata": _json.dumps({"host_id": host_id, "workspace": str(workspace)})},
            files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        )
        create_resp.raise_for_status()
        session_id = create_resp.json()["session_id"]
        runner_id = client.get(f"/v1/sessions/{session_id}").json().get("runner_id")
        assert runner_id, f"host-bound create did not bind a runner: {session_id}"
        _wait_until(
            lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online") is True,
            timeout_s=60.0,
            message=(
                f"runner {runner_id} never connected — workspace launch should "
                f"succeed while the directory exists. Daemon log:\n"
                f"{daemon_log.read_text()[-3000:]}"
            ),
        )

        # Stop the host and all runners so none can race the workspace deletion.
        runner_pids = _launched_runner_pids(daemon_log)
        assert runner_pids, (
            f"no launched-runner pid in daemon log:\n{daemon_log.read_text()[-3000:]}"
        )
        daemon.send_signal(signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            daemon.wait(timeout=15)
        for pid in runner_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        # Wait for keepalive expiry before asking the host to relaunch.
        _wait_until(
            lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online") is False,
            timeout_s=75.0,
            message=f"runner {runner_id} never went offline after the host reboot",
        )
        _wait_until(
            lambda: (
                not any(
                    h["host_id"] == host_id and h["status"] == "online"
                    for h in client.get("/v1/hosts").json().get("hosts", [])
                )
            ),
            timeout_s=30.0,
            message=f"host {host_id} never went offline after the daemon was killed",
        )

        shutil.rmtree(workspace)

        # Reconnect the same host identity without restoring the workspace.
        daemon, _, daemon_log = _spawn_host_daemon(
            tmp_path / "host-home", live_server, log_name="host-daemon-after-reboot.log"
        )
        _wait_until(
            lambda: any(
                h["host_id"] == host_id and h["status"] == "online"
                for h in client.get("/v1/hosts").json().get("hosts", [])
            ),
            timeout_s=30.0,
            message=f"host {host_id} never came back online after the reboot",
        )

        marker = f"hello after workspace cleanup {uuid.uuid4().hex[:8]}"
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(marker)
        composer.press("Enter")

        expect(page.get_by_text(marker)).to_be_visible(timeout=30_000)

        pill = page.get_by_test_id("error-pill").filter(has_text=_WORKSPACE_MISSING_HEADLINE)
        expect(pill).to_have_count(1, timeout=30_000)
        expander = pill.locator('button[aria-expanded="false"]')
        if expander.count() > 0:
            expander.first.click()
        expect(pill.get_by_test_id("error-message-content")).to_contain_text(
            f"{_WORKSPACE_MISSING_PREFIX}: {workspace}"
        )

        _wait_until(
            lambda: client.get(f"/v1/sessions/{session_id}").json().get("status") == "failed",
            timeout_s=30.0,
            message=f"session {session_id} never reached status=failed",
        )

        # App logs may be in the named process log, not captured stdout.
        server_logs = sorted(tmp_path_factory.getbasetemp().glob("e2e_ui_server*/server.log"))
        assert server_logs, "suite-spawned server log not found under the pytest basetemp"
        log_files = list(server_logs)
        for stdout_log in server_logs:
            banner = re.search(
                r"^\s*log:\s+(\S+)", stdout_log.read_text(errors="replace"), re.MULTILINE
            )
            if banner:
                process_log = Path(banner.group(1)).expanduser()
                if process_log.exists():
                    log_files.append(process_log)
        log_lines = [
            line for log in log_files for line in log.read_text(errors="replace").splitlines()
        ]
        funnel_lines = [
            line
            for line in log_lines
            if f"session turn failed for {session_id}" in line
            and _WORKSPACE_MISSING_PREFIX in line
            and "ERROR" in line
        ]
        assert not funnel_lines, (
            "expected workspace_missing refusal was logged through the "
            "ERROR-level 'session turn failed' funnel that error dashboards "
            f"attribute to server defects:\n{funnel_lines}"
        )
        # A refusal record also proves the scan reached the live server log.
        refusal_lines = [
            line
            for line in log_lines
            if f"session turn refused for {session_id}" in line
            and _WORKSPACE_MISSING_PREFIX in line
        ]
        assert refusal_lines, (
            "the workspace_missing refusal left no categorical "
            f"'session turn refused' record in the server logs: {log_files}"
        )
    finally:
        if session_id is not None:
            with contextlib.suppress(Exception):
                client.delete(f"/v1/sessions/{session_id}")
        client.close()
        if daemon.poll() is None:
            daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)
