"""E2E regression: runner-less sessions stay ``running`` after a restart.

Reproduces two live symptoms end-to-end, driving the real user
journey against actual server + runner subprocesses (no internal-function
pokes, no hand-written session rows):

1. **Orphaned "running" after a server restart.** A runner-bound session with
   an in-flight turn persists ``live_status="running"`` on its conversation
   row. When the server is shut down and a replacement server comes up on the
   same database, the replacement has no live status cache, so it falls back
   to the persisted ``running`` value. A server recycle also leaves the
   runner's liveness lease (``runner_last_seen``) in place, because the
   replacement cannot tell a dead runner from one still reconnecting; once
   that lease lapses (``RUNNER_LIVENESS_TTL_S``), ``omnigent host status
   --sessions`` (which reads ``GET /v1/sessions``) must stop reporting the
   runner-less session as ``running``.

2. **``stop-session`` falsely succeeds.** ``omnigent host stop-session`` POSTs
   a ``stop_session`` event to ``POST /v1/sessions/{id}/events``. On the
   replacement server no runner client resolves
   (``_stop_session_via_runner`` returns a no-op ``False``) and there is no
   live host tunnel, yet the route still returns ``2xx``. The CLI prints
   "Stopped session" while the row stays ``running`` — a destructive action
   reported as success that did nothing.

The journey is faithful: we bring a runner online, create a session bound to
it, and drive a real (blocked) turn so the framework itself persists
``running`` — we never fabricate the end state. Then we shut the server down,
start a fresh one on the same DB, and observe exactly what the CLI observes.

Runs against the mock LLM server — no real credentials needed::

    .venv/bin/python -m pytest \
        tests/e2e/test_orphaned_running_session_stop.py -v

The assertions encode the CORRECT post-fix behavior, so the test fails on the
buggy build (both facets) and passes once a fix reconciles the orphaned status
and/or makes ``stop-session`` truthful.
"""

from __future__ import annotations

import os
import secrets
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.db.enum_codecs import SESSION_LIVE_STATUS
from omnigent.runner.identity import token_bound_runner_id
from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests.e2e.conftest import (
    _REPO_ROOT,
    configure_mock_llm,
    find_free_port,
    lookup_agent_id,
    send_user_message_to_session,
    set_fallback_mock_llm,
    upload_agent,
)
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S
from tests.e2e.test_host_e2e import _write_smoke_agent_yaml

# Persisted live_status codes that read as an active session on the wire.
# ``_session_status_from_cache`` collapses running/waiting -> "running".
_ACTIVE_LIVE_STATUS_CODES = {SESSION_LIVE_STATUS["running"], SESSION_LIVE_STATUS["waiting"]}


def _spawn_server(
    *,
    port: int,
    db_path: Path,
    artifact_dir: Path,
    log_path: Path,
    config_home: Path,
    mock_llm_server_url: str,
    tunnel_token: str,
) -> tuple[subprocess.Popen[bytes], str]:
    """Start an ``omnigent server`` subprocess on *db_path* and wait for health.

    Mirrors the ``live_server`` conftest fixture's spawn recipe (worktree
    PYTHONPATH via ``apply_server_env``, mock ``OPENAI_BASE_URL``, a
    ``_policy_llm_`` server config, and a fixed runner tunnel token) so the
    two servers in this test boot identically to the rest of the e2e suite —
    the only twist is that both point at the SAME sqlite database.

    An isolated ``OMNIGENT_CONFIG_HOME`` keeps the server off any ambient
    user/gateway config so the mock LLM is the only backend in play.

    :param port: TCP port to bind.
    :param db_path: Shared sqlite database path.
    :param artifact_dir: Per-server artifact location.
    :param log_path: File to capture the server's stdout/stderr.
    :param config_home: Clean ``OMNIGENT_CONFIG_HOME`` directory.
    :param mock_llm_server_url: Base URL of the mock LLM server.
    :param tunnel_token: Runner tunnel token for the server allowlist.
    :returns: The server process handle and its base URL.
    :raises RuntimeError: If the server exits early or never turns healthy.
    """
    env = _base_env(config_home, mock_llm_server_url)
    apply_server_env(env, _REPO_ROOT)
    env["OMNIGENT_RUNNER_TUNNEL_TOKEN"] = tunnel_token

    server_cfg = log_path.parent / f"server-{port}.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "_policy_llm_",
                    "connection": {
                        "base_url": f"{mock_llm_server_url}/v1",
                        "api_key": "mock-key",
                    },
                }
            }
        )
    )
    args = [
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
        "--config",
        str(server_cfg),
    ]
    log_handle = open(log_path, "w")  # noqa: SIM115 — closed by the caller on teardown
    proc = subprocess.Popen(
        args,
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://localhost:{port}"
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_handle.close()
            tail = log_path.read_text()[-3000:] if log_path.exists() else ""
            raise RuntimeError(
                f"Server on port {port} exited early (code={proc.returncode}).\n{tail}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0, trust_env=False).status_code == 200:
                return proc, base_url
        except httpx.HTTPError:
            # The subprocess may still be starting; retry until the deadline.
            continue
        time.sleep(POLL_INTERVAL_S)
    proc.kill()
    log_handle.close()
    tail = log_path.read_text()[-3000:] if log_path.exists() else ""
    raise RuntimeError(f"Server on port {port} never turned healthy.\n{tail}")


def _spawn_runner(
    *,
    base_url: str,
    runner_id: str,
    tunnel_token: str,
    log_path: Path,
    config_home: Path,
    mock_llm_server_url: str,
) -> subprocess.Popen[bytes]:
    """Spawn a runner subprocess bound to *base_url*'s tunnel allowlist.

    Mirrors the ``live_server`` fixture's sibling-runner spawn: the runner id
    is derived from the shared binding token so the server accepts its
    WebSocket upgrade, and ``OPENAI_BASE_URL`` points its agent executor at
    the mock LLM.

    :param base_url: Server URL the runner connects back to.
    :param runner_id: Token-bound runner id the server expects.
    :param tunnel_token: Shared binding token.
    :param log_path: File to capture the runner's output.
    :param config_home: Clean ``OMNIGENT_CONFIG_HOME`` directory.
    :param mock_llm_server_url: Base URL of the mock LLM server.
    :returns: The runner process handle.
    """
    env = _base_env(config_home, mock_llm_server_url)
    # The runner must import the worktree checkout (not a stale install) or the
    # harness spawn fails. The live_server fixture achieves this by inheriting
    # the server env (which ran apply_server_env) into the runner; replicate
    # that here since apply_runner_env is neutralize-only and never prepends.
    apply_server_env(env, _REPO_ROOT)
    runner_env = apply_runner_env(
        {
            **env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": tunnel_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
        }
    )
    log_handle = open(log_path, "w")  # noqa: SIM115 — child inherits the descriptor
    try:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.runner._entry"],
            env=runner_env,
            cwd=compat_runner_cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        log_handle.close()
        raise
    log_handle.close()
    return proc


def _base_env(config_home: Path, mock_llm_server_url: str) -> dict[str, str]:
    """Build the shared subprocess environment (mock LLM + clean config home).

    :param config_home: Clean ``OMNIGENT_CONFIG_HOME`` directory.
    :param mock_llm_server_url: Base URL of the mock LLM server.
    :returns: A fresh environment dict.
    """
    return {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OMNIGENT_CONFIG_HOME": str(config_home),
    }


def _terminate(proc: subprocess.Popen[bytes] | None, *, timeout: float = 10.0) -> None:
    """SIGTERM a subprocess and hard-kill it if it doesn't exit in time.

    :param proc: The process handle, or ``None``.
    :param timeout: Seconds to wait for a graceful exit.
    """
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _list_session(client: httpx.Client, session_id: str) -> dict[str, object] | None:
    """Return the ``GET /v1/sessions`` list item for *session_id*, or ``None``.

    This is exactly the surface ``omnigent host status --sessions`` reads:
    the paginated session list, whose ``status`` field carries the
    running/idle/failed rollup the CLI prints.

    :param client: HTTP client pointed at a live server.
    :param session_id: Session/conversation id to find.
    :returns: The matching list item dict, or ``None`` when absent.
    """
    resp = client.get(
        "/v1/sessions",
        params={"include_archived": "true", "limit": 1000},
    )
    resp.raise_for_status()
    for item in resp.json().get("data", []):
        if item.get("id") == session_id:
            return item
    return None


def _persisted_live_status(db_path: Path, session_id: str) -> int | None:
    """Read the raw persisted ``live_status`` code for *session_id*.

    The metadata row's ``id`` is a 16-byte packed Uuid16 column, so the hex
    session id is packed to bytes for the lookup.

    :param db_path: The shared sqlite database path.
    :param session_id: Conversation id (32-char hex).
    :returns: The ``SmallInteger`` live_status code, or ``None`` if unset.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT live_status FROM omnigent_conversation_metadata WHERE id = ?",
            (bytes.fromhex(session_id),),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _expire_runner_lease(db_path: Path, runner_id: str) -> None:
    """Age *runner_id*'s liveness lease past :data:`RUNNER_LIVENESS_TTL_S`.

    A server recycle keeps the runner's ``runner_last_seen`` stamp so a
    replacement does not mistake a reconnecting runner for a dead one. A runner
    that never comes back is only recognised as gone once that lease lapses;
    backdating the stamp stands in for waiting out the TTL.

    :param db_path: The shared sqlite database path.
    :param runner_id: Runner id whose sessions' lease should lapse.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE omnigent_conversation_metadata SET runner_last_seen = ? WHERE runner_id = ?",
            (int(time.time()) - RUNNER_LIVENESS_TTL_S - 1, runner_id),
        )
        conn.commit()
    finally:
        conn.close()


def _runner_online(client: httpx.Client, runner_id: str) -> bool:
    """Return whether *runner_id* has a live tunnel on the server.

    :param client: HTTP client pointed at a live server.
    :param runner_id: Runner id from the session row.
    :returns: ``True`` only when the status route reports ``online: true``.
    """
    resp = client.get(f"/v1/runners/{runner_id}/status")
    if resp.status_code != 200:
        return False
    return resp.json().get("online") is True


@pytest.mark.timeout(300)
def test_runner_less_session_remains_running_after_shutdown_and_stop(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A runner-less session must not stay ``running``.

    Drives the real journey — bring a runner online, create a bound session,
    block an in-flight turn so the framework persists ``live_status=running``,
    shut the server down, start a replacement on the same DB — then asserts the
    two post-fix expectations the CLI relies on:

    * Facet 1: once the dead runner's liveness lease has lapsed, the replacement
      server must not report the runner-less session as ``running`` in
      ``GET /v1/sessions``.
    * Facet 2: ``stop_session`` must not report success (``2xx``) while leaving
      the session ``running``.
    """
    tunnel_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(tunnel_token)
    db_path = tmp_path / "shared.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    config_home = tmp_path / "config-home"
    config_home.mkdir()

    port_a = find_free_port()
    server_a: subprocess.Popen[bytes] | None = None
    server_b: subprocess.Popen[bytes] | None = None
    runner: subprocess.Popen[bytes] | None = None
    client_a: httpx.Client | None = None
    client_b: httpx.Client | None = None

    try:
        # ── 1. Boot server A on the shared DB, then a bound runner. ───────
        server_a, base_a = _spawn_server(
            port=port_a,
            db_path=db_path,
            artifact_dir=artifact_dir / "a",
            log_path=logs / "server-a.log",
            config_home=config_home,
            mock_llm_server_url=mock_llm_server_url,
            tunnel_token=tunnel_token,
        )
        client_a = httpx.Client(base_url=base_a, timeout=30.0, trust_env=False)
        # Server-level policy classifier always ALLOWs (matches the suite's
        # live_server fixture) so the turn we drive reaches the agent's model.
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )

        runner = _spawn_runner(
            base_url=base_a,
            runner_id=runner_id,
            tunnel_token=tunnel_token,
            log_path=logs / "runner.log",
            config_home=config_home,
            mock_llm_server_url=mock_llm_server_url,
        )
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            if _runner_online(client_a, runner_id):
                break
            time.sleep(0.5)
        assert _runner_online(client_a, runner_id), f"Runner {runner_id} never came online"

        # ── 2. Upload agent + create a session bound to the runner. ───────
        agent_name = upload_agent(client_a, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(client_a, agent_name)
        create = client_a.post("/v1/sessions", json={"agent_id": agent_id})
        create.raise_for_status()
        session_id = create.json()["id"]
        client_a.patch(
            f"/v1/sessions/{session_id}", json={"runner_id": runner_id}
        ).raise_for_status()

        # ── 3. Drive a turn that blocks in-flight → persists running. ─────
        configure_mock_llm(mock_llm_server_url, [{"text": "ORPHANED_RUNNING_HOLD", "block": True}])
        send_user_message_to_session(
            client_a,
            session_id=session_id,
            content="Reply with the literal string ORPHANED_RUNNING_HOLD and nothing else.",
        )

        # The mock holds the LLM request open, so the turn stays in flight.
        gate_deadline = time.monotonic() + 30.0
        gate_pending = False
        while time.monotonic() < gate_deadline:
            g = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0, trust_env=False)
            if g.status_code == 200 and g.json().get("pending") is True:
                gate_pending = True
                break
            time.sleep(POLL_INTERVAL_S)
        assert gate_pending, "The blocked turn never reached the mock LLM (no pending gate)"

        # Server A should now report the session running, and — critically —
        # the framework must persist that running status on the conversation
        # row (this is the state the replacement server later inherits).
        status_deadline = time.monotonic() + 30.0
        persisted_running = False
        item = None
        while time.monotonic() < status_deadline:
            item = _list_session(client_a, session_id)
            code = _persisted_live_status(db_path, session_id)
            if (
                item is not None
                and item.get("status") == "running"
                and code in _ACTIVE_LIVE_STATUS_CODES
            ):
                persisted_running = True
                break
            time.sleep(POLL_INTERVAL_S)
        assert persisted_running, (
            "Precondition failed: the in-flight turn did not persist a running "
            f"live_status (server status={item and item.get('status')!r}, "
            f"persisted code={_persisted_live_status(db_path, session_id)!r})"
        )

        # ── 4. Shut down: server + its runner both go away. ───────────────
        _terminate(server_a, timeout=15.0)
        server_a = None
        _terminate(runner, timeout=10.0)
        runner = None
        # Persisted running survives the shutdown (the bug: no repair on the
        # way down and no reconciliation on the way up).
        assert _persisted_live_status(db_path, session_id) in _ACTIVE_LIVE_STATUS_CODES, (
            "Precondition failed: live_status was repaired on shutdown, so the "
            "orphaned-running scenario cannot be exercised"
        )

        # ── 5. Start replacement server B on the SAME database. ───────────
        port_b = find_free_port()
        server_b, base_b = _spawn_server(
            port=port_b,
            db_path=db_path,
            artifact_dir=artifact_dir / "b",
            log_path=logs / "server-b.log",
            config_home=config_home,
            mock_llm_server_url=mock_llm_server_url,
            tunnel_token=tunnel_token,
        )
        client_b = httpx.Client(base_url=base_b, timeout=30.0, trust_env=False)
        # The runner is gone for good, but the replacement honours its liveness
        # lease until the TTL lapses (a reconnecting runner looks identical).
        _expire_runner_lease(db_path, runner_id)

        # ── 6. Observe what the CLI observes on the replacement server. ────
        item_b = _list_session(client_b, session_id)
        assert item_b is not None, f"Session {session_id} missing from replacement server list"
        status_after_restart = item_b.get("status")
        runner_still_online = _runner_online(client_b, runner_id)

        # Facet 2 driver: POST the same stop_session event the CLI sends.
        stop_resp = client_b.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "stop_session", "data": {}},
        )
        stop_ok = 200 <= stop_resp.status_code < 300
        item_after_stop = _list_session(client_b, session_id)
        status_after_stop = item_after_stop.get("status") if item_after_stop else None

        # ── Facet 1: runner-less session must not read "running". ─────────
        facet1_bug = status_after_restart == "running" and not runner_still_online
        # ── Facet 2: stop must not falsely succeed. ───────────────────────
        facet2_bug = stop_ok and status_after_stop == "running"

        assert not (facet1_bug or facet2_bug), (
            "Runner-less running-session bug reproduced.\n"
            f"  Facet 1 (orphaned running after restart): "
            f"status={status_after_restart!r}, runner_online={runner_still_online} "
            f"→ {'BUG' if facet1_bug else 'ok'}\n"
            f"  Facet 2 (stop-session false success): "
            f"stop HTTP {stop_resp.status_code} ({stop_resp.text!r}), "
            f"status_after_stop={status_after_stop!r} "
            f"→ {'BUG' if facet2_bug else 'ok'}"
        )
    finally:
        if client_a is not None:
            client_a.close()
        if client_b is not None:
            client_b.close()
        _terminate(server_a, timeout=10.0)
        _terminate(server_b, timeout=10.0)
        _terminate(runner, timeout=10.0)
