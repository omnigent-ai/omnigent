"""E2E repro: cancelling opencode-native startup leaks ``opencode serve``.

Drives the reported journey end-to-end through the real product stack:
connect a host daemon -> create an ``opencode-native-ui`` session (the runner
spawns a real per-session ``opencode serve``) -> cancel the startup by
deleting the session while the server's readiness probe is still pending ->
verify the spawned ``opencode serve`` subprocess is reaped instead of
outliving its session.

On the buggy build the deletion lands in the startup window between
``subprocess.Popen`` and the forwarder adopting the server, so the child
``opencode serve`` is orphaned (observable via ``pgrep -af 'opencode
serve'``) — exactly the reported symptom. A fixed build reaps the child
within the grace window.

Needs an ``opencode`` binary on PATH (any supported version); no LLM
credentials are required because the session is cancelled before any turn.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from omnigent.native_coding_agents import OPENCODE_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

pytestmark = pytest.mark.skipif(
    shutil.which("opencode") is None,
    reason="opencode-native startup-cancel e2e needs an `opencode` binary on PATH",
)

_SERVE_APPEAR_TIMEOUT_S = 120.0
_REAP_GRACE_S = 30.0


def _spawn_host_daemon(*, tmp_path: Path, live_server: str) -> subprocess.Popen[bytes]:
    """Spawn an ``omnigent host`` daemon pointed at the test server."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    # A pinned CI `opencode` wrapper synthesizes a provider config from these;
    # a real binary ignores them. No turn runs, so no model is ever reached.
    # The host->runner env filter drops unknown names, so list them in the
    # passthrough var (itself forwarded) to reach the runner's `opencode`.
    env.setdefault("OPENCODE_MODEL", "claude-sonnet-4-5")
    env.setdefault("GATEWAY_BASE_URL", "http://127.0.0.1:9")
    passthrough = {"OPENCODE_MODEL", "GATEWAY_BASE_URL"}
    existing = {p for p in env.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "").split(",") if p}
    env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = ",".join(sorted(existing | passthrough))
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


def _online_host_id(client: httpx.Client, timeout: float = 30.0) -> str:
    """Poll ``GET /v1/hosts`` until at least one host is online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _opencode_serve_pids(workspace: Path) -> list[int]:
    """Return pids of ``opencode serve`` processes running in ``workspace``.

    The runner launches each per-session server with ``cwd=<session
    workspace>``, so matching on the process cwd scopes the scan to this
    test's disposable session even on a busy host.
    """
    resolved = workspace.resolve()
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            cmdline = raw.replace(b"\x00", b" ").decode(errors="replace")
            cwd = Path(os.readlink(entry / "cwd")).resolve()
        except OSError:
            continue
        if "opencode" in cmdline and " serve " in f"{cmdline} " and cwd == resolved:
            pids.append(int(entry.name))
    return pids


def test_opencode_native_startup_cancel_reaps_serve(
    http_client: httpx.Client,
    tmp_path: Path,
    live_server: str,
) -> None:
    """Deleting an opencode-native session during startup must reap ``opencode serve``.

    Journey: launch an opencode-native session; while the runner's
    ``opencode serve`` readiness probe is still pending, cancel the startup by
    deleting the session; the spawned server must not outlive the session.
    """
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json()["data"] if a["name"] == OPENCODE_NATIVE_AGENT_NAME), None
    )
    assert agent_id is not None, "opencode-native-ui agent not seeded"

    workspace = tmp_path / "ws"
    workspace.mkdir()

    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    leaked: list[int] = []
    try:
        host_id = _online_host_id(http_client)
        create = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        # Wait for the runner to spawn the per-session `opencode serve`, then
        # cancel immediately: the readiness probe polls every 0.5s and the
        # server takes seconds to answer, so first sighting of the process
        # lands inside the pending-probe window the report describes.
        deadline = time.monotonic() + _SERVE_APPEAR_TIMEOUT_S
        while time.monotonic() < deadline:
            if _opencode_serve_pids(workspace):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                f"opencode serve never spawned for {session_id} within "
                f"{_SERVE_APPEAR_TIMEOUT_S}s (workspace={workspace})"
            )

        # The user-facing cancellation: delete the session while startup is
        # in flight. Retried briefly in case the server has not finished
        # materializing the session it is asked to delete.
        delete_deadline = time.monotonic() + 30.0
        while True:
            delete = http_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
            if delete.status_code < 300:
                break
            if time.monotonic() >= delete_deadline:
                raise AssertionError(
                    f"DELETE /v1/sessions/{session_id} kept failing: "
                    f"{delete.status_code} {delete.text[:200]}"
                )
            time.sleep(POLL_INTERVAL_S)

        # The session is gone; its `opencode serve` must be reaped too.
        reap_deadline = time.monotonic() + _REAP_GRACE_S
        while time.monotonic() < reap_deadline:
            leaked = _opencode_serve_pids(workspace)
            if not leaked:
                break
            time.sleep(POLL_INTERVAL_S)
        assert not leaked, (
            f"opencode serve outlived its cancelled session {session_id}: "
            f"pids={leaked} still running {_REAP_GRACE_S}s after DELETE "
            "(startup cancellation leaks the spawned server)"
        )
    finally:
        for pid in leaked:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
