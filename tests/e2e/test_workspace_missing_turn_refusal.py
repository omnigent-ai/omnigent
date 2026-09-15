"""E2E repro: a missing host workspace is logged as an ERROR turn failure.

Journey (the real user path from the ticket):

1. Bring a host online (``omnigent`` host daemon registers with the server).
2. Create a session bound to that host in workspace directory ``W`` — the host
   launches gen1 runner and it connects, which proves ``W`` was valid.
3. The runner dies (here: SIGKILL, standing in for a host restart / crash) so
   its tunnel drops and the server declares the runner offline.
4. ``W`` is deleted on the host (the user removed the git worktree / project dir).
5. The user sends a message in the session. The runner is gone, so the server
   asks the still-online host to relaunch — the host checks ``W`` and refuses
   with the ``workspace_missing`` category.

Observed on the buggy build: the server consumes the message, records a
structured ``workspace_missing`` error item (correct, and must be preserved),
**and** publishes a ``failed`` status edge, which logs an ERROR from
``omnigent.server.routes.sessions._publish_status`` with the message

    session turn failed for <session_id> (origin=host_launch_failed
        code=workspace_missing prev=...): workspace path does not exist: <W>

That ERROR line is the KPI-counted turn-failure signature: an *expected*,
upstream host condition (the user deleted their own workspace) is attributed
as an Omnigent turn-failure defect. The fix reclassifies these expected host
launch refusals so
they are no longer logged as ``session turn failed`` at ERROR level, while the
structured error the user sees is preserved.

This drives the real stack — server subprocess, a real host daemon, a real
host-launched runner — so the workspace-missing refusal is produced organically
by the host's own ``workspace.is_dir()`` check, not scripted.

Run::

    .venv/bin/python -m pytest tests/e2e/test_workspace_missing_turn_refusal.py -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import (
    apply_server_env,
    compat_server_cwd,
    server_executable,
)
from tests.e2e.conftest import HEALTH_TIMEOUT_S, POLL_INTERVAL_S, find_free_port
from tests.e2e.test_host_e2e import (
    _pid_alive,
    _spawn_host_daemon,
    _wait_for_host_online,
)
from tests.e2e.test_host_runner_leak_5182 import (
    _launches,
    _runner_online,
    _wait_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Absolute source roots a host-launched runner must import from. Runners run
# with cwd=<workspace> (not the repo), so a *relative* PYTHONPATH entry like
# ``sdks/python-client`` won't resolve there — each root is prepended absolute.
_SOURCE_ROOTS = (
    _REPO_ROOT,
    _REPO_ROOT / "sdks" / "python-client",
    _REPO_ROOT / "sdks" / "ui",
)

# The KPI-counted message prefix from the ticket's signature evidence. It is
# emitted only by the ERROR branch of _publish_status on a ``failed`` edge.
_KPI_TURN_FAILED_PREFIX = "session turn failed for "


def _ensure_worktree_on_pythonpath() -> None:
    """Let host-launched runners import the branch's source from this worktree.

    The host daemon builds each runner subprocess env from ``os.environ`` and
    forwards ``PYTHONPATH`` (it is on ``_RUNNER_ENV_ALLOWLIST``). A launched
    runner runs with its ``cwd`` set to the *workspace*, not the repo, so
    unless the worktree source roots are on ``PYTHONPATH`` as absolute paths
    the runner's bare interpreter cannot resolve ``omnigent`` /
    ``omnigent_client`` / ``omnigent_ui_sdk`` — it exits with
    ``ModuleNotFoundError`` before it can connect its tunnel. Prepend the
    absolute source roots (mirrors what ``apply_server_env`` does for the
    server) so the daemon's launched runners import this branch's source.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = existing.split(os.pathsep) if existing else []
    roots = [str(root) for root in _SOURCE_ROOTS]
    if all(root in parts for root in roots):
        return
    prepend = [root for root in roots if root not in parts]
    os.environ["PYTHONPATH"] = os.pathsep.join([*prepend, *parts])


_AGENT_YAML = "\n".join(
    [
        "name: ws-missing-repro-agent",
        "description: Minimal agent for the workspace-missing repro.",
        "executor:",
        "  harness: openai-agents",
        "  model: gpt-5.4",
        "prompt: |",
        "  You are a terse smoke-test assistant.",
        "  Follow the user's instruction exactly.",
        "",
    ]
)


def _spawn_server(*, tmp_path: Path, mock_llm_server_url: str) -> tuple[subprocess.Popen, str, Path]:
    """Spawn an ``omnigent server`` that accepts host-launched runners.

    Unlike the shared ``live_server`` fixture this omits the
    ``OMNIGENT_RUNNER_TUNNEL_TOKEN`` allow-list (so the host's own
    per-launch runner tokens are accepted) and owns a known server-log
    path this test can read to inspect the ERROR line the ticket is about.

    :param tmp_path: Per-test temp dir for the DB, artifacts, and log.
    :param mock_llm_server_url: Mock LLM base URL for the server + its
        host-launched runners.
    :returns: ``(process, base_url, server_log_path)``.
    :raises RuntimeError: If the server does not pass health in time.
    """
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "ws_missing.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    server_log = tmp_path / "server.log"

    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
    }
    apply_server_env(env, _REPO_ROOT)
    # No OMNIGENT_RUNNER_TUNNEL_TOKEN: accept any token-bound runner, the
    # deployed-server posture the host relies on for its launched runners.
    env.pop("OMNIGENT_RUNNER_TUNNEL_TOKEN", None)

    log_handle = open(server_log, "w")  # noqa: SIM115 — lives for the Popen lifetime; closed by the caller
    proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        proc.kill()
        log_handle.close()
        raise RuntimeError(
            f"server didn't pass health within {HEALTH_TIMEOUT_S}s; "
            f"log tail:\n{server_log.read_text()[-3000:]}"
        )
    if proc.poll() is not None:
        log_handle.close()
        raise RuntimeError(
            f"server exited early (code {proc.returncode}); "
            f"log tail:\n{server_log.read_text()[-3000:]}"
        )
    return proc, base_url, server_log


def _register_agent(client: httpx.Client) -> str:
    """Register the openai-agents smoke agent via a bundle-only create.

    :param client: HTTP client pointed at the server.
    :returns: The durable ``agent_id`` to bind host sessions to.
    """
    yaml_bytes = _AGENT_YAML.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("agent.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = resp.json()["session_id"]
    agent_resp = client.get(f"/v1/sessions/{session_id}/agent")
    agent_resp.raise_for_status()
    return agent_resp.json()["id"]


def _error_items(client: httpx.Client, session_id: str) -> list[dict]:
    """Return the transcript's ``type=error`` items for a session."""
    resp = client.get(f"/v1/sessions/{session_id}/items")
    resp.raise_for_status()
    return [item for item in resp.json()["data"] if item.get("type") == "error"]


@pytest.mark.timeout(600)
def test_missing_workspace_relaunch_is_not_logged_as_turn_failure(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A deleted host workspace must not be logged as an ERROR turn failure.

    The expected/upstream ``workspace_missing`` host
    refusal is surfaced to the user as a structured error (which this test
    requires to be preserved), but on the buggy build it is *also* published
    as a ``failed`` status edge and logged at ERROR as
    ``session turn failed for <id> ...`` — the KPI-counted signature that
    misattributes it as an Omnigent turn-failure defect.

    Regression assertions:

    * The structured ``workspace_missing`` error item is preserved with the
      sanitized ``workspace path does not exist: <W>`` reason (holds before
      and after the fix).
    * The server does NOT log an ERROR ``session turn failed for <session_id>``
      for this expected condition. Fails on the buggy build (the ERROR line is
      emitted); passes once the refusal is reclassified out of the turn-failure
      ERROR path.
    """
    server_proc: subprocess.Popen | None = None
    server_log: Path | None = None
    daemon = None
    gen1_pid: int | None = None
    try:
        # Host-launched runners inherit PYTHONPATH from the daemon; make sure
        # the worktree is on it so they can import omnigent (see helper).
        _ensure_worktree_on_pythonpath()

        server_proc, base_url, server_log = _spawn_server(
            tmp_path=tmp_path / "server",
            mock_llm_server_url=mock_llm_server_url,
        )
        client = httpx.Client(
            base_url=base_url,
            timeout=120,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )

        daemon = _spawn_host_daemon(
            tmp_path=tmp_path / "host",
            live_server=base_url,
            mock_llm_server_url=mock_llm_server_url,
        )
        _wait_for_host_online(client, daemon.host_id, timeout=60.0)

        agent_id = _register_agent(client)

        # A workspace that exists at create time so the host-bound create
        # launches gen1 successfully (proving W was valid).
        workspace = tmp_path / "worktree" / "universe"
        workspace.mkdir(parents=True)

        create = client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": daemon.host_id,
                "workspace": str(workspace),
            },
            timeout=90.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]
        runner_id = create.json()["runner_id"]
        assert runner_id is not None, create.text

        gen1_id, gen1_pid = _wait_for(
            lambda: (_launches(daemon.daemon_log) or [None])[0],
            timeout=60.0,
            what="the host daemon to log gen1's launch",
        )
        _wait_for(
            lambda: _runner_online(client, runner_id),
            timeout=90.0,
            what=f"gen1 runner {runner_id} to connect its tunnel",
        )
        assert _pid_alive(gen1_pid), f"gen1 (pid={gen1_pid}) died before it was superseded"

        # Runner dies (host restart / crash stand-in): its tunnel closes and
        # the server declares it offline, so the next message must relaunch.
        os.kill(gen1_pid, signal.SIGKILL)
        _wait_for(
            lambda: not _runner_online(client, runner_id),
            timeout=120.0,
            what=f"the server to declare runner {runner_id} offline after it was killed",
        )

        # The user removed the workspace on the host (git worktree cleanup).
        shutil.rmtree(workspace)
        assert not workspace.exists()

        # Sending a message is the real runner-start attempt: the host is
        # asked to relaunch and refuses because W is gone.
        msg = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "are you still there?"}],
                },
            },
            timeout=120.0,
        )
        assert msg.status_code in (200, 202), f"unexpected status: {msg.status_code}: {msg.text}"

        # The structured workspace-missing error is surfaced (and must be
        # preserved by any fix): a single type=error item carrying the
        # sanitized reason rebuilt from the authorized workspace.
        error_items = _wait_for(
            lambda: _error_items(client, session_id) or None,
            timeout=60.0,
            what="the server to persist the workspace-missing error item",
        )
        assert len(error_items) == 1, f"expected exactly one error item, got {error_items!r}"
        err = error_items[0]
        assert err["code"] == "workspace_missing", err
        assert err["message"] == f"workspace path does not exist: {workspace}", err

        # The refusal must be recorded somewhere in the server log (a fix must
        # not silently swallow the condition) — holds before (ERROR line) and
        # after (reclassified warning) the fix.
        log_text = server_log.read_text()
        assert str(workspace) in log_text, (
            "the server never logged the workspace-missing refusal at all"
        )

        # THE BUG: the expected *workspace-missing* host refusal must NOT be
        # logged as ``session turn failed for <session_id> ... workspace_missing``
        # — the KPI-counted turn-failure signature. Present on the buggy build;
        # absent once the refusal is reclassified out of the ERROR turn-failure
        # path.
        #
        # Scoped to the workspace_missing signature on purpose: an offline
        # runner may crash-exit before this point (a distinct, legitimate
        # ``runner_failed_to_start`` turn failure that the reclassification
        # does not touch), so a blanket "no turn-failure line" guard would never go
        # green after the fix. This guard fails on the buggy build for the
        # workspace-missing line alone and passes once that one is reclassified.
        offending = [
            line
            for line in log_text.splitlines()
            if f"{_KPI_TURN_FAILED_PREFIX}{session_id}" in line
            and "workspace_missing" in line
        ]
        assert offending == [], (
            "an expected workspace-missing host refusal was logged as a "
            f"turn failure (KPI-counted). Offending server log line(s):\n"
            + "\n".join(offending)
        )
    finally:
        if gen1_pid is not None and _pid_alive(gen1_pid):
            try:
                os.kill(gen1_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if daemon is not None:
            daemon.proc.send_signal(signal.SIGTERM)
            try:
                daemon.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.proc.kill()
                daemon.proc.wait()
        if server_proc is not None:
            if server_proc.poll() is None:
                server_proc.send_signal(signal.SIGTERM)
                try:
                    server_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server_proc.kill()
                    server_proc.wait()
