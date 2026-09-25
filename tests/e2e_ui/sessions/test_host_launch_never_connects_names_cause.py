"""Browser journey for a host-launched runner that never connects.

A real host spawns a runner that SIGSTOP freezes before its tunnel dial.
After the send fails, check the error headline and expanded server cause.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests.e2e_ui.conftest import _register_extra_agent

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The error code determines the banner headline.
_EXPECTED_HEADLINE = "The session's runner isn't connected to the server."
# The expanded body names the failed connection phase.
_EXPECTED_CAUSE_SUBSTRING = "never connected to the server"
_RUNNER_TOKEN_PATTERN = re.compile(r"runner_token_[0-9a-f]+")

_LAUNCH_LINE = re.compile(r"Launched runner (\S+) for workspace .*?\(pid=(\d+)\)")


def _launches(log_path: Path) -> list[tuple[str, int]]:
    """Parse every runner the host daemon spawned, in launch order."""
    if not log_path.exists():
        return []
    return [
        (rid, int(pid)) for rid, pid in _LAUNCH_LINE.findall(log_path.read_text(errors="replace"))
    ]


class _RunnerWedger:
    """SIGSTOP every runner the daemon spawns, before it can connect."""

    def __init__(self, daemon_log: Path) -> None:
        self._daemon_log = daemon_log
        self._stop = threading.Event()
        self.wedged: dict[str, int] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            for rid, pid in _launches(self._daemon_log):
                if rid in self.wedged:
                    continue
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGSTOP)
                self.wedged[rid] = pid
            time.sleep(0.02)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        for pid in self.wedged.values():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGCONT)


def _spawn_host_daemon(tmp_path: Path, base_url: str) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Start an isolated host; direct spawn gives SIGSTOP time to land."""
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": f"stuck-launch-{host_id[:8]}"}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OMNIGENT_RUNNER_ZYGOTE": "0",
        "PYTHONPATH": os.pathsep.join([str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]).rstrip(
            os.pathsep
        ),
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
            # The server may still be starting.
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


@pytest.mark.timeout(300)
def test_host_session_that_never_connects_names_the_cause(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """The stuck-session error names the failed runner-connect phase."""
    daemon: subprocess.Popen[bytes] | None = None
    wedger: _RunnerWedger | None = None
    try:
        daemon, host_id, daemon_log = _spawn_host_daemon(tmp_path, live_server)
        _wait_for_host_online(live_server, host_id)
        wedger = _RunnerWedger(daemon_log)

        agent_id = _register_extra_agent(
            live_server, "stuck-launch-agent", "You are a terse smoke-test assistant."
        )
        assert agent_id is not None
        workspace = tmp_path / "project"
        workspace.mkdir()

        create = httpx.post(
            f"{live_server}/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
            },
            timeout=90.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        # Reject runs where SIGSTOP lost the pre-connect race.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not wedger.wedged:
            time.sleep(0.05)
        assert wedger.wedged, "the host never logged a runner launch"
        gen1_id = next(iter(wedger.wedged))
        time.sleep(2.0)
        if _runner_online(live_server, gen1_id):
            pytest.fail(
                f"fault injection lost the race: runner {gen1_id} connected "
                "before the SIGSTOP landed"
            )

        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)

        # The send relaunches; the wedger freezes that runner too.
        composer.fill("hello? is anything happening?")
        composer.press("Enter")
        error_pill = page.get_by_test_id("error-pill")
        expect(error_pill).to_be_visible(timeout=150_000)
        expect(page.get_by_test_id("error-headline")).to_have_text(
            _EXPECTED_HEADLINE, timeout=15_000
        )

        # Keep the named runner and phase visible for the recording.
        error_pill.click()
        expect(page.get_by_text(_EXPECTED_CAUSE_SUBSTRING)).to_be_visible(timeout=15_000)
        expect(page.get_by_text(_RUNNER_TOKEN_PATTERN)).to_be_visible()
        page.wait_for_timeout(2_500)

        assert not _runner_online(live_server, gen1_id), (
            "precondition broken: the wedged runner connected its tunnel"
        )
    finally:
        if wedger is not None:
            wedger.close()
        if daemon is not None:
            daemon.send_signal(signal.SIGTERM)
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
        if wedger is not None:
            for pid in wedger.wedged.values():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
