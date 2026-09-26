"""Browser journeys for a host runner process that dies under an open session."""

from __future__ import annotations

import contextlib
import os
import re
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
from tests._helpers.runner_faults import disk_full_spec_cache_pythonpath
from tests.e2e_ui.conftest import _register_extra_agent

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LAUNCH_LINE = re.compile(r"Launched runner (\S+) for workspace .*?\(pid=(\d+)\)")
_RUNNER_EXIT_TEXT = re.compile(r"runner process exited")
# The error code determines the banner headline.
_EXPECTED_HEADLINE = "The session's runner process exited on the host."


def _launches(log_path: Path) -> list[tuple[str, int]]:
    """Parse every runner the host daemon spawned, in launch order."""
    if not log_path.exists():
        return []
    return [
        (rid, int(pid)) for rid, pid in _LAUNCH_LINE.findall(log_path.read_text(errors="replace"))
    ]


def _spawn_host_daemon(
    tmp_path: Path, base_url: str, mock_llm_url: str, *, pythonpath: str | None = None
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Start an isolated host; direct spawn so the logged PID is the runner."""
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": f"runner-exit-{host_id[:8]}"}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    if pythonpath is None:
        pythonpath = os.pathsep.join([str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]).rstrip(
            os.pathsep
        )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OMNIGENT_RUNNER_ZYGOTE": "0",
        "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "PYTHONPATH": pythonpath,
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                base_url,
            ],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _wait_for_host_online(base_url: str, host_id: str, timeout: float = 45.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* shows online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/v1/hosts", timeout=5.0)
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"host {host_id!r} never came online at {base_url}")


def _runner_online(base_url: str, runner_id: str) -> bool:
    """Return the server's ``online`` verdict for a runner tunnel."""
    try:
        resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=5.0)
    except httpx.HTTPError:
        return False
    return bool(resp.status_code == 200 and resp.json().get("online"))


def _session_status(base_url: str, session_id: str) -> str | None:
    """Return the server's current status for *session_id*."""
    try:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return resp.json().get("status")


def _create_host_session(base_url: str, host_id: str, agent_name: str, workspace: Path) -> str:
    """Register a smoke agent and create a session the host must serve."""
    agent_id = _register_extra_agent(base_url, agent_name, "You are a terse smoke-test assistant.")
    assert agent_id is not None
    workspace.mkdir()
    create = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
        timeout=90.0,
    )
    create.raise_for_status()
    return create.json()["id"]


def _wait_for_failed(base_url: str, session_id: str, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _session_status(base_url, session_id) != "failed":
        time.sleep(0.25)
    assert _session_status(base_url, session_id) == "failed", (
        "session never transitioned to failed after the runner process exit"
    )


def _stop_daemon(daemon: subprocess.Popen[bytes] | None) -> None:
    if daemon is None:
        return
    daemon.send_signal(signal.SIGTERM)
    try:
        daemon.wait(timeout=10)
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.wait()


@pytest.mark.timeout(300)
def test_host_runner_exit_fails_open_session(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A killed host runner fails the open session; the banner names the exit."""
    daemon: subprocess.Popen[bytes] | None = None
    killed_pids: list[int] = []
    try:
        daemon, host_id, daemon_log = _spawn_host_daemon(
            tmp_path, live_server, mock_llm_server_url
        )
        _wait_for_host_online(live_server, host_id)
        session_id = _create_host_session(
            live_server, host_id, "runner-exit-agent", tmp_path / "project"
        )

        # Wait for the launched runner to connect its tunnel.
        deadline = time.monotonic() + 60.0
        runner_id: str | None = None
        while time.monotonic() < deadline:
            launches = _launches(daemon_log)
            if launches:
                runner_id = launches[-1][0]
                if _runner_online(live_server, runner_id):
                    break
            time.sleep(0.25)
        assert runner_id is not None and _runner_online(live_server, runner_id), (
            "runner never connected its tunnel"
        )

        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)

        # Kill the connected runner process: a stand-in for any non-zero exit.
        pid = _launches(daemon_log)[-1][1]
        os.kill(pid, signal.SIGKILL)
        killed_pids.append(pid)

        _wait_for_failed(live_server, session_id)

        # Reload so the SPA renders the failed snapshot's synthesized error block.
        page.reload()
        error_pill = page.get_by_test_id("error-pill")
        expect(error_pill).to_be_visible(timeout=30_000)
        error_pill.click()
        message = page.get_by_test_id("error-message-content")
        expect(message).to_contain_text(_RUNNER_EXIT_TEXT, timeout=15_000)
        page.wait_for_timeout(2_500)
    finally:
        _stop_daemon(daemon)
        for extra_pid in killed_pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(extra_pid, signal.SIGKILL)


@pytest.mark.min_server_version("0.16.0")
@pytest.mark.timeout(300)
def test_runner_boot_crash_banner_names_the_cause(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """The failed-session banner states why the runner died, above its log."""
    daemon: subprocess.Popen[bytes] | None = None
    try:
        daemon, host_id, _daemon_log = _spawn_host_daemon(
            tmp_path,
            live_server,
            mock_llm_server_url,
            pythonpath=disk_full_spec_cache_pythonpath(
                tmp_path / "fault", _REPO_ROOT, os.environ.get("PYTHONPATH")
            ),
        )
        _wait_for_host_online(live_server, host_id)
        session_id = _create_host_session(
            live_server, host_id, "runner-boot-crash-agent", tmp_path / "project"
        )
        _wait_for_failed(live_server, session_id)

        page.goto(f"{live_server}/c/{session_id}")
        error_pill = page.get_by_test_id("error-pill")
        expect(error_pill).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("error-headline")).to_have_text(
            _EXPECTED_HEADLINE, timeout=15_000
        )

        error_pill.click()
        message = page.get_by_test_id("error-message-content")
        expect(message).to_contain_text(_RUNNER_EXIT_TEXT, timeout=15_000)
        # The runner's own reason is stated up front ...
        expect(message).to_contain_text("No space left on device")
        # ... instead of at the bottom of a traceback in the message body.
        expect(message).not_to_contain_text("Traceback")

        # The raw runner log, with the crash site, is one click away.
        page.get_by_role("button", name="View diagnostics").click()
        expect(page.get_by_test_id("error-diagnostics-content")).to_contain_text(
            "in create_app", timeout=15_000
        )
        page.wait_for_timeout(2_500)
    finally:
        _stop_daemon(daemon)
