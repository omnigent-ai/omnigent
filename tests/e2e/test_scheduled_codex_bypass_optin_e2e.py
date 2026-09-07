"""End-to-end guard: scheduled codex tasks can opt out of approval stalls.

Bug: a ``codex-native`` scheduled task that needs to write a file stalls
indefinitely on an interactive ``codex_file_change_approval`` when it fires
unattended, and there is NO way to opt it out. The scheduled-task REST API
gates every non-null ``permission_mode`` to ``claude-native`` agents, so a
``PATCH /v1/scheduled-tasks/<id>`` with ``{"permission_mode":
"bypassPermissions"}`` — the one knob that would translate to Codex's
``--dangerously-bypass-approvals-and-sandbox`` launch flag — is rejected with
a 400.

This test drives the real journey against a live ``omnigent server`` over the
REST API a scheduling automation uses:

  1. create an hourly ``codex-native-ui`` scheduled task (no mode) -> 200
  2. PATCH ``permission_mode=bypassPermissions`` -> the unattended opt-in
     the report asks for. EXPECTED: accepted (200) and persisted.
     On the buggy build this is rejected with 400
     ("permission_mode is only supported for claude-native agents"), so this
     assertion is what fails until the fix lands (the fail->pass target).
  3. create-time opt-in of ``bypassPermissions`` on a codex task -> 200.
  4. PATCH ``permission_mode=acceptEdits`` (a Claude-only mode) -> still
     rejected 400. The expected behavior keeps rejecting Claude-only modes
     for Codex, so this must hold before AND after the fix.
  5. control: the identical ``bypassPermissions`` PATCH on a
     ``claude-native-ui`` task is accepted (200). This isolates the failure
     to the harness gate (the same run continues normally immediately after
     the approval is accepted, which isolates the failure to launch policy).

The complementary fire-path half (a codex ``bypassPermissions`` task launching
with ``--dangerously-bypass-approvals-and-sandbox`` instead of stalling) is a
unit-level concern covered under ``tests/server/scheduled/test_fire.py``; this
e2e guard covers the observable REST journey the report reproduces.

The scheduled-task create/update validation runs in-process at persist time
(it resolves the built-in agent's harness from the agent cache), so this
journey needs neither an online runner nor a real LLM — it spawns a bare
state server. The spawn recipe mirrors ``tests/_helpers/live_server.py``.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTH_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.5

_CODEX_AGENT_NAME = "codex-native-ui"
_CLAUDE_AGENT_NAME = "claude-native-ui"
_HOURLY_RRULE = "FREQ=HOURLY;INTERVAL=1"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@pytest.fixture()
def scheduled_tasks_server(tmp_path: Path) -> Iterator[httpx.Client]:
    """Spawn a bare ``omnigent server`` and yield a loopback HTTP client.

    Mirrors the ``tests/_helpers/live_server.py`` spawn recipe (worktree on
    ``PYTHONPATH`` via ``apply_server_env`` so the branch's source is what
    runs, ``server_executable() -m omnigent.cli server``). ``trust_env=False``
    keeps loopback requests off any ambient ``HTTP(S)_PROXY``.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "server.log"

    env = {**os.environ}
    apply_server_env(env, _REPO_ROOT)

    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen lifetime
    proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
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

    client = httpx.Client(base_url=base_url, trust_env=False, timeout=30)
    try:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error: object = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_handle.flush()
                raise RuntimeError(
                    f"server exited early (code {proc.returncode}):\n"
                    f"{log_path.read_text()[-3000:]}"
                )
            try:
                resp = client.get("/health")
                if resp.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(_POLL_INTERVAL_S)
        else:
            log_handle.flush()
            raise RuntimeError(
                f"server never became healthy within {_HEALTH_TIMEOUT_S:.0f}s "
                f"(last_error={last_error}):\n{log_path.read_text()[-3000:]}"
            )
        yield client
    finally:
        client.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()


def _headers() -> dict[str, str]:
    # A stable owner so the created tasks belong to a resolvable user.
    return {"X-Forwarded-Email": "alice@example.com"}


def _agent_id(client: httpx.Client, name: str) -> str:
    resp = client.get("/v1/agents", headers=_headers())
    resp.raise_for_status()
    by_name = {a["name"]: a["id"] for a in resp.json()["data"]}
    assert name in by_name, f"built-in agent {name!r} not registered (got {sorted(by_name)})"
    return by_name[name]


def _create_task(client: httpx.Client, agent_id: str, name: str, **extra: object) -> dict:
    body: dict[str, object] = {
        "name": name,
        "prompt": "Write ./artifact.txt with the current time.",
        "rrule": _HOURLY_RRULE,
        "agent_id": agent_id,
        "timezone": "UTC",
    }
    body.update(extra)
    resp = client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert resp.status_code == 200, f"create {name!r} failed: {resp.status_code} {resp.text}"
    return resp.json()


def test_codex_scheduled_task_can_opt_into_bypass_permissions(
    scheduled_tasks_server: httpx.Client,
) -> None:
    """A codex-native scheduled task must accept the bypassPermissions opt-in."""
    client = scheduled_tasks_server
    codex_id = _agent_id(client, _CODEX_AGENT_NAME)
    claude_id = _agent_id(client, _CLAUDE_AGENT_NAME)

    # 1. hourly codex task, no mode -> the unattended automation from the report.
    codex_task = _create_task(client, codex_id, "hourly codex artifact")
    codex_task_id = codex_task["id"]
    assert codex_task["permission_mode"] is None

    # 2. PATCH the bypass opt-in the report asks for. On the buggy build this is
    #    rejected 400 ("permission_mode is only supported for claude-native");
    #    the fix must accept and persist it (translated later to Codex's
    #    --dangerously-bypass-approvals-and-sandbox launch flag).
    patched = client.patch(
        f"/v1/scheduled-tasks/{codex_task_id}",
        json={"permission_mode": "bypassPermissions"},
        headers=_headers(),
    )
    assert patched.status_code == 200, (
        "codex-native scheduled task cannot opt into bypassPermissions "
        f"(HTTP {patched.status_code}: {patched.text}) — the unattended "
        "approval stall has no opt-out."
    )
    assert patched.json()["permission_mode"] == "bypassPermissions"

    got = client.get(f"/v1/scheduled-tasks/{codex_task_id}", headers=_headers())
    assert got.status_code == 200
    assert got.json()["permission_mode"] == "bypassPermissions"

    # 3. Create-time opt-in of bypassPermissions on a codex task is also allowed.
    created_with_bypass = _create_task(
        client, codex_id, "hourly codex artifact (bypass)", permission_mode="bypassPermissions"
    )
    assert created_with_bypass["permission_mode"] == "bypassPermissions"

    # 4. A Claude-only mode (acceptEdits) stays rejected for a codex agent —
    #    the expected behavior keeps this gate. Holds before and after fix.
    rejected = client.patch(
        f"/v1/scheduled-tasks/{codex_task_id}",
        json={"permission_mode": "acceptEdits"},
        headers=_headers(),
    )
    assert rejected.status_code == 400, rejected.text
    assert "permission_mode" in rejected.text

    # 5. Control: the identical opt-in on a claude-native task is accepted,
    #    isolating the failure to the harness launch policy (not the task).
    claude_task = _create_task(client, claude_id, "hourly claude artifact")
    claude_patched = client.patch(
        f"/v1/scheduled-tasks/{claude_task['id']}",
        json={"permission_mode": "bypassPermissions"},
        headers=_headers(),
    )
    assert claude_patched.status_code == 200, claude_patched.text
    assert claude_patched.json()["permission_mode"] == "bypassPermissions"
