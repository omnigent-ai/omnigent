"""Browser e2e: a deleted host workspace fails the turn as a structured refusal.

Reported journey (every observed occurrence was a worktree-style path that was
cleaned up between turns):

1. bring a host online (``omnigent host --server <url>``)
2. create a session on that host in workspace directory ``W`` — the runner
   launches and connects, proving ``W`` was valid
3. the runner goes away (host restart / process kill) and ``W`` is deleted
   (worktree cleanup)
4. the user reopens the session and sends a message → the turn fails with
   ``workspace path does not exist: W``

This test drives that journey against a REAL ``omnigent host`` daemon spawned
next to the suite's live server (no host stubs): the host's ``_handle_launch``
performs the real ``workspace.is_dir()`` refusal, the server persists the
structured ``workspace_missing`` error turn, and the SPA renders the error
card in chat.

Two contracts are asserted:

- The user-facing behavior to preserve: the message is consumed (not silently
  dropped) and a structured, sanitized error card is rendered — headline "The
  session workspace no longer exists on the host." with the canonical detail
  ``workspace path does not exist: <W>`` (single source:
  ``omnigent.host.frames.workspace_missing_message``).
- The defect: this *expected, categorical* refusal must not be funneled
  through the generic ERROR-level ``session turn failed for <id>: ...`` log in
  ``_publish_status`` — that ERROR record is the fingerprint error dashboards
  attribute to an Omnigent server defect, counting routine workspace cleanup
  as one.
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

# Canonical refusal text prefix (see omnigent/host/frames.py:
# workspace_missing_message) and the SPA's friendly headline for the
# ``workspace_missing`` code (web/src/components/blocks/StatusBlocks.tsx).
_WORKSPACE_MISSING_PREFIX = "workspace path does not exist"
_WORKSPACE_MISSING_HEADLINE = "The session workspace no longer exists on the host."

_POLL_S = 0.5


def _absolute_pythonpath() -> str:
    """PYTHONPATH for the host daemon and its runner subprocesses.

    The daemon spawns runners with ``cwd=<workspace>``, so any *relative*
    entries in the ambient ``PYTHONPATH`` (CI sets e.g. ``sdks/python-client``)
    stop resolving inside the runner. Absolutize them against the repo root
    and prepend the worktree so both the daemon and its runners import the
    checked-out source.

    :returns: An ``os.pathsep``-joined PYTHONPATH of absolute entries.
    """
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
    """Spawn a real ``omnigent host`` daemon registered with *server_url*.

    Pre-seeds ``config.yaml`` with a unique ``(host_id, name)`` so repeated
    runs against the session-scoped server don't collide on the host store's
    unique owner+name row (same convention as ``tests/e2e/test_host_e2e.py``).

    Calling this again with the same *home* reuses the persisted ``host_id``,
    so a restart reconnects as the *same* host — this is how the test models a
    host reboot: kill the daemon, wipe the worktree, bring the same host back.

    :param home: Per-test dir used as the daemon's ``HOME``.
    :param server_url: The live server base URL, e.g. ``"http://127.0.0.1:5x"``.
    :param log_name: Filename for the daemon's captured stderr under *home*;
        a restart uses a fresh name so its launch/refusal lines are isolated.
    :returns: ``(process, host_id, daemon_log_path)``.
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
        # Direct-spawn runners (no copy-on-write zygote) so the daemon's
        # "Launched runner ... (pid=NNNN)" line names the real runner process
        # and a SIGKILL of that pid reliably takes the runner offline. This is
        # only a harness-determinism knob: the workspace.is_dir() refusal and
        # its error handling are identical on either spawn path.
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
    """Poll *predicate* until truthy or fail with *message*.

    :param predicate: Zero-arg callable returning truthy when ready.
    :param timeout_s: Max seconds to wait.
    :param message: Assertion message on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_S)
    raise AssertionError(message)


def _launched_runner_pids(log_path: Path) -> list[int]:
    """All runner PIDs the host daemon logged launching.

    A session create can supersede its first runner (the server may prelaunch
    and then relaunch), so return *every* launched pid — any orphan left alive
    when the daemon dies keeps its server tunnel, and the server would still
    report the runner online.

    :param log_path: Path to the captured daemon stderr log.
    :returns: Launched runner PIDs in log order (empty until one is logged).
    """
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
    """A deleted workspace fails the turn structurally, not as an ERROR log.

    Drives the deleted-workspace journey end to end against a real host
    daemon and asserts (a) the structured ``workspace_missing`` error card
    renders in chat with the canonical sanitized message, and (b) the
    expected refusal is not logged through the ERROR-level ``session turn
    failed`` funnel that error dashboards attribute to server defects.

    :param page: Playwright page fixture.
    :param live_server: Spawned server base URL.
    :param tmp_path: Per-test temp dir (host HOME + workspace).
    :param tmp_path_factory: Session temp factory — locates the server log.
    :param request: Pytest request — detects the ``--ui-base-url`` override.
    """
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

        # The user creates a session on that host in workspace W (exists).
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

        # --- Host reboot wipes the worktree ---------------------------------
        # A hard daemon kill models a host reboot: the daemon can neither
        # relaunch the runner nor report the runner's exit as a crash, so the
        # server simply observes the host (and its runner) drop off. This is
        # what makes the reproduction deterministic — no live runner survives
        # to race the workspace deletion, and no spurious
        # ``runner_failed_to_start`` pre-empts the real launch-time refusal.
        runner_pids = _launched_runner_pids(daemon_log)
        assert runner_pids, (
            f"no launched-runner pid in daemon log:\n{daemon_log.read_text()[-3000:]}"
        )
        daemon.send_signal(signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            daemon.wait(timeout=15)
        # The runners are children of the now-dead daemon; kill the orphans so
        # their tunnels drop and the server sees the runner go offline too.
        for pid in runner_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        # The server deregisters a runner's tunnel only when its keepalive
        # lapses, which trails the process death by ~30s. Wait it out so the
        # next message provokes a *fresh* launch — which the host refuses
        # because the workspace is gone — rather than being routed to the
        # stale tunnel of the now-dead runner.
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

        # Worktree cleanup happens while the host is down (a git worktree prune,
        # a wiped /tmp on reboot, etc.): the workspace directory disappears.
        shutil.rmtree(workspace)

        # The same host comes back online (host_id persists in config.yaml),
        # but the session's workspace is now gone.
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

        # The user reopens the session and sends a message → the server asks
        # the host to relaunch the runner, and the host refuses the launch
        # because the workspace directory no longer exists.
        marker = f"hello after workspace cleanup {uuid.uuid4().hex[:8]}"
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(marker)
        composer.press("Enter")

        # The message is consumed into the transcript, not silently dropped...
        expect(page.get_by_text(marker)).to_be_visible(timeout=30_000)

        # ...and the structured workspace_missing error card renders, carrying
        # the canonical sanitized detail (never raw host/runner log text).
        pill = page.get_by_test_id("error-pill").filter(has_text=_WORKSPACE_MISSING_HEADLINE)
        expect(pill).to_have_count(1, timeout=30_000)
        expander = pill.locator('button[aria-expanded="false"]')
        if expander.count() > 0:
            expander.first.click()
        expect(pill.get_by_test_id("error-message-content")).to_contain_text(
            f"{_WORKSPACE_MISSING_PREFIX}: {workspace}"
        )

        # The turn is terminal server-side.
        _wait_until(
            lambda: client.get(f"/v1/sessions/{session_id}").json().get("status") == "failed",
            timeout_s=30.0,
            message=f"session {session_id} never reached status=failed",
        )

        # Fail→pass target: the expected categorical refusal must not be
        # funneled through the ERROR-level generic turn-failure log in
        # ``_publish_status`` — that record is the fingerprint that counts
        # routine workspace cleanup as an Omnigent server defect.
        #
        # The spawned server mirrors app logs to stderr only when attached to
        # a terminal, so the canonical record lands in the process log file
        # its startup banner names (``log: <path>``); scan that file plus the
        # captured stdout, wherever each exists.
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
        # The refusal must stay observable as its own categorical record —
        # this also proves the scan above is reading the live server log.
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
