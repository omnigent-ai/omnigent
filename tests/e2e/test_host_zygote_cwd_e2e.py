"""E2E regression tests: the ``--server`` host daemon and its
``omnigent.runner._zygote`` pool root at the FIRST dispatch's cwd.

Reproduces the reported journey against a real server + host daemon + zygote:

1. the background host daemon is started from a transient git worktree ``W1``
   (mirroring ``omnigent run`` dispatched from inside W1 spawning the daemon),
2. a session is dispatched with a DIFFERENT workspace ``W2``,
3. ``W1`` is deleted (``git worktree remove``),
4. observable failure: pool processes serving the W2 session still sit in the
   deleted ``W1`` (the report's ``lsof -a -p <pid> -d cwd`` evidence), because
   the zygote — and every harness child it forks — inherits the daemon's start
   cwd. ``_run_child`` (runner forks) chdirs to the session workspace, but
   ``_run_harness_child`` never chdirs, so any ``os.getcwd()`` fallback in a
   harness executor roots at the first dispatch's worktree.

Two tests:

- ``test_zygote_harness_children_do_not_root_at_daemon_start_cwd`` — the live
  facet. FAILS on the current build: the harness subprocess serving the W2
  session has ``cwd == W1`` and keeps it (deleted) after the worktree is
  removed. A fix may either chdir harness forks to the session workspace or
  root the whole pool at a stable directory — the assertion only requires
  that the pool does not sit in the *first dispatch's* (now deleted) cwd.
- ``test_new_dispatch_succeeds_after_daemon_start_cwd_deleted`` — the
  dead-cwd-pool facet (previously fixed): a brand-new dispatch
  after W1 is deleted must complete instead of failing with
  ``turn setup failed: [Errno 2] No such file or directory``. Kept as a
  passing regression guard.

Both run against the mock LLM server — no real credentials needed::

    pytest tests/e2e/test_host_zygote_cwd_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, runner_executable
from tests.e2e.conftest import (
    POLL_INTERVAL_S,
    configure_mock_llm,
    register_inline_agent,
    reset_mock_llm,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="reads /proc/<pid>/cwd to audit the zygote pool; Linux only",
)


@dataclass
class _WorktreeHostDaemon:
    """A host daemon spawned with its cwd inside a transient git worktree.

    :param proc: The daemon subprocess handle.
    :param host_id: The pre-seeded host id, bare 32-char hex.
    :param w1: The worktree the daemon was started from (the "first
        dispatch's cwd" in the reported journey).
    :param w2: A sibling worktree used as a different session workspace.
    """

    proc: subprocess.Popen[bytes]
    host_id: str
    w1: Path
    w2: Path


def _make_worktrees(tmp_path: Path) -> tuple[Path, Path]:
    """Create a git repo with two worktrees W1 and W2.

    :param tmp_path: Per-test temp dir.
    :returns: ``(W1, W2)`` worktree paths.
    """
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=e2e@test",
            "-c",
            "user.name=e2e",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    w1 = tmp_path / "repo-worktrees" / "W1"
    w2 = tmp_path / "repo-worktrees" / "W2"
    for path in (w1, w2):
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "-q",
                str(path),
                "-b",
                f"{path.name.lower()}-{uuid.uuid4().hex[:6]}",
            ],
            check=True,
        )
    return w1, w2


def _spawn_daemon_from_worktree(
    *,
    tmp_path: Path,
    live_server: str,
    mock_llm_server_url: str,
) -> _WorktreeHostDaemon:
    """Spawn an isolated host daemon whose process cwd is worktree W1.

    Mirrors ``_spawn_host_daemon`` in ``test_host_e2e.py`` (unique host row per
    test, mock-LLM env) but starts the daemon **from inside W1** — the exact
    state ``omnigent run`` leaves behind when the first dispatch after a stop
    happens in a transient worktree and spawns the background daemon.

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param live_server: Server URL the daemon registers with.
    :param mock_llm_server_url: Mock LLM base URL.
    :returns: The spawned daemon handle plus the two worktrees.
    """
    w1, w2 = _make_worktrees(tmp_path)
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-zygote-cwd-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            # The reported journey: the daemon inherits the FIRST dispatch's
            # cwd — a transient git worktree that will be deleted.
            cwd=str(w1),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return _WorktreeHostDaemon(proc=proc, host_id=host_id, w1=w1, w2=w2)


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float = 45.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* is online.

    :param client: HTTP client pointed at the server.
    :param host_id: Host id to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the host never appears online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


def _launch_runner_for_session(
    client: httpx.Client,
    *,
    host_id: str,
    agent_id: str,
    workspace: Path,
    timeout: float = 60.0,
) -> str:
    """Create a session and launch a host runner for it in *workspace*.

    :param client: HTTP client pointed at the server.
    :param host_id: Host to launch on.
    :param agent_id: Agent to bind the session to.
    :param workspace: Session workspace (the runner's cwd).
    :param timeout: Max seconds to wait for the runner to come online.
    :returns: The session id.
    :raises AssertionError: If the launch fails or never comes online.
    """
    resp = client.post("/v1/sessions", json={"agent_id": agent_id})
    resp.raise_for_status()
    session_id = str(resp.json()["id"])
    launch = client.post(
        f"/v1/hosts/{host_id}/runners",
        json={"session_id": session_id, "workspace": str(workspace)},
        timeout=90.0,
    )
    assert launch.status_code == 200, (
        f"Runner launch for workspace {workspace} failed: {launch.status_code} {launch.text[:500]}"
    )
    runner_id = launch.json()["runner_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/v1/runners/{runner_id}/status")
        if status.status_code == 200 and status.json().get("online") is True:
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        raise AssertionError(f"Runner {runner_id} never came online")
    client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id}).raise_for_status()
    return session_id


def _run_pwd_turn(
    client: httpx.Client,
    *,
    session_id: str,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Send one turn (the mock LLM is scripted to call ``sys_os_shell pwd``)
    and poll the session snapshot until the turn is terminal.

    :param client: HTTP client pointed at the server.
    :param session_id: Session to drive.
    :param timeout: Max seconds to wait.
    :returns: The final session snapshot body.
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "run pwd"}]},
        },
    )
    resp.raise_for_status()
    deadline = time.monotonic() + timeout
    seen_running = False
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        snap = client.get(f"/v1/sessions/{session_id}")
        snap.raise_for_status()
        body = snap.json()
        status = body.get("status")
        if status in ("running", "waiting"):
            seen_running = True
        items = body.get("items", [])
        real_output = [
            item
            for item in items
            if item.get("type") not in ("resource_event",)
            and not (
                item.get("type") == "message" and (item.get("data") or {}).get("role") == "user"
            )
        ]
        if status == "failed" or (
            status in ("idle", "completed") and (seen_running or real_output)
        ):
            return body
        time.sleep(POLL_INTERVAL_S)
    return {**body, "status": f"TIMEOUT({body.get('status')})"}


def _turn_output_text(body: dict[str, Any]) -> str:
    """Concatenate tool outputs, assistant text, and errors from a snapshot.

    :param body: Session snapshot from ``GET /v1/sessions/{id}``.
    :returns: Joined output text (tool stdout, assistant text, error JSON).
    """
    parts: list[str] = []
    for item in body.get("items", []):
        data = item.get("data") or {}
        kind = item.get("type")
        if kind == "function_call_output":
            parts.append(str(data.get("output", "")))
        elif kind == "message" and data.get("role") == "assistant":
            for block in data.get("content", []):
                if block.get("text"):
                    parts.append(block["text"])
        elif kind == "error" or item.get("status") == "failed":
            parts.append(json.dumps(data)[:600])
    error = body.get("error")
    if error:
        parts.append(f"SESSION_ERROR={error}")
    return "\n".join(parts)


def _session_process_cwds(session_id: str) -> dict[int, str]:
    """Map pids of processes serving *session_id* to their current cwd.

    Scans ``/proc`` for omnigent runner/harness processes whose cmdline or
    environment references the session id, and reads each process's cwd
    symlink (the same evidence as ``lsof -a -p <pid> -d cwd``). A deleted
    cwd reads with a ``" (deleted)"`` suffix on Linux.

    :param session_id: The session/conversation id to look for.
    :returns: ``{pid: cwd_string}`` for matching processes.
    """
    found: dict[int, str] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            cmdline = (
                Path(f"/proc/{pid}/cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")
            )
        except OSError:
            continue
        if "omnigent" not in cmdline:
            continue
        referenced = session_id in cmdline
        if not referenced:
            # Zygote-forked children keep the fork-request env; the harness
            # carries --conversation-id on argv, the runner carries the
            # session id in its env (PRIMARY_SESSION_ID).
            try:
                environ = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
                referenced = session_id in environ
            except OSError:
                referenced = False
        if not referenced:
            continue
        try:
            found[pid] = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
    return found


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM then SIGKILL a subprocess, reaping it."""
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _register_pwd_agent(
    http_client: httpx.Client,
    mock_llm_server_url: str,
    slug: str,
) -> tuple[str, str]:
    """Register a minimal shell agent and script its mock queue with one
    ``sys_os_shell pwd`` call followed by a final text.

    :param http_client: HTTP client pointed at the live server.
    :param mock_llm_server_url: Mock LLM base URL.
    :param slug: Unique-ifying fragment for the agent/model names.
    :returns: ``(agent_id, model_key)``.
    """
    model = f"mock-zygote-cwd-{slug}"
    agent_name = register_inline_agent(
        http_client,
        name=f"zygote-cwd-{slug}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a minimal shell-execution assistant for e2e tests. "
            "Call sys_os_shell with exactly the command requested."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none", "allow_network": True},
            },
        },
    )
    resp = http_client.get("/v1/sessions", params={"agent_name": agent_name, "limit": 1})
    resp.raise_for_status()
    rows = resp.json()["data"]
    assert rows, f"agent {agent_name!r} not registered"
    return str(rows[0]["agent_id"]), model


def test_zygote_harness_children_do_not_root_at_daemon_start_cwd(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A session dispatched with workspace W2 must not be served by pool
    processes rooted at the daemon's start cwd (worktree W1).

    This is the live facet: the zygote and every harness child it
    forks inherit the daemon's cwd (the FIRST dispatch's worktree). After that
    worktree is deleted, those processes sit in a deleted directory — exactly
    the report's ``lsof -a -p <zygote-pid> -d cwd`` evidence — and any
    ``os.getcwd()`` fallback in a harness executor roots the W2 session's
    environment at W1.
    """
    reset_mock_llm(mock_llm_server_url)
    daemon = _spawn_daemon_from_worktree(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id)
        agent_id, model = _register_pwd_agent(
            http_client, mock_llm_server_url, uuid.uuid4().hex[:6]
        )

        # Session A anchors the pool in W1 (the first dispatch), then session B
        # dispatches with a DIFFERENT workspace, W2.
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "a1",
                            "name": "sys_os_shell",
                            "arguments": json.dumps({"command": "pwd"}),
                        }
                    ]
                },
                {"text": "done-a"},
                {
                    "tool_calls": [
                        {
                            "call_id": "b1",
                            "name": "sys_os_shell",
                            "arguments": json.dumps({"command": "pwd"}),
                        }
                    ]
                },
                {"text": "done-b"},
            ],
            key=model,
        )
        session_a = _launch_runner_for_session(
            http_client, host_id=daemon.host_id, agent_id=agent_id, workspace=daemon.w1
        )
        body_a = _run_pwd_turn(http_client, session_id=session_a)
        assert body_a.get("status") in ("idle", "completed"), (
            f"W1 anchor turn did not complete: {body_a.get('status')} "
            f"{_turn_output_text(body_a)[:400]}"
        )

        session_b = _launch_runner_for_session(
            http_client, host_id=daemon.host_id, agent_id=agent_id, workspace=daemon.w2
        )
        body_b = _run_pwd_turn(http_client, session_id=session_b)
        assert body_b.get("status") in ("idle", "completed"), (
            f"W2 turn did not complete: {body_b.get('status')} {_turn_output_text(body_b)[:400]}"
        )

        # Delete W1 — the daemon's start cwd — exactly as the reporter's
        # `git worktree remove` does after the first run finishes.
        subprocess.run(
            ["git", "-C", str(tmp_path / "repo"), "worktree", "remove", "--force", str(daemon.w1)],
            check=False,
        )
        shutil.rmtree(daemon.w1, ignore_errors=True)
        assert not daemon.w1.exists()

        # THE observable: every live process serving the W2 session must have
        # a cwd that still exists. On the buggy build the zygote-forked
        # harness child serving session B kept the zygote's inherited cwd
        # (W1) and now reads "<W1> (deleted)".
        cwds = _session_process_cwds(session_b)
        for _pid, _cwd in cwds.items():
            try:
                _env = Path(f"/proc/{_pid}/environ").read_bytes().decode("utf-8", "replace")
            except OSError:
                _env = ""
            _zf = "OMNIGENT_ZYGOTE_HARNESS_FORKED" in _env or "ZYGOTE_HARNESS_FORKED" in _env
            _cmd = (
                Path(f"/proc/{_pid}/cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", "replace")[:160]
            )
            print(f"AUDIT pid={_pid} zygote_forked={_zf} cwd={_cwd} cmd={_cmd}")
        assert cwds, f"no live processes found for session {session_b}"
        dead = {
            pid: cwd
            for pid, cwd in cwds.items()
            if cwd.endswith(" (deleted)") or cwd.rstrip("/") == str(daemon.w1)
        }
        assert not dead, (
            "Processes serving the W2 session are rooted at the daemon's "
            f"deleted start cwd (W1={daemon.w1}):\n"
            + "\n".join(f"  pid={pid} cwd={cwd}" for pid, cwd in dead.items())
            + "\nThe zygote pool inherited the first dispatch's cwd; harness "
            "forks never chdir to the session workspace "
            "(omnigent/runner/_zygote.py _run_harness_child)."
        )
    finally:
        _terminate(daemon.proc)


def test_new_dispatch_succeeds_after_daemon_start_cwd_deleted(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A brand-new dispatch after the daemon's start worktree is deleted must
    complete — not fail with ``turn setup failed: [Errno 2]``.

    Regression guard for the dead-cwd pool (previously fixed):
    runner forks chdir to the session workspace and the direct-Popen path
    passes ``cwd=workspace``, so a deleted daemon-start cwd no longer bricks
    later dispatches.
    """
    reset_mock_llm(mock_llm_server_url)
    daemon = _spawn_daemon_from_worktree(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id)
        agent_id, model = _register_pwd_agent(
            http_client, mock_llm_server_url, uuid.uuid4().hex[:6]
        )

        # Anchor the zygote pool in W1 with a first dispatch, then delete W1.
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "a1",
                            "name": "sys_os_shell",
                            "arguments": json.dumps({"command": "pwd"}),
                        }
                    ]
                },
                {"text": "done-a"},
                {
                    "tool_calls": [
                        {
                            "call_id": "c1",
                            "name": "sys_os_shell",
                            "arguments": json.dumps({"command": "pwd"}),
                        }
                    ]
                },
                {"text": "done-c"},
            ],
            key=model,
        )
        session_a = _launch_runner_for_session(
            http_client, host_id=daemon.host_id, agent_id=agent_id, workspace=daemon.w1
        )
        body_a = _run_pwd_turn(http_client, session_id=session_a)
        assert body_a.get("status") in ("idle", "completed"), (
            f"W1 anchor turn did not complete: {body_a.get('status')}"
        )

        subprocess.run(
            ["git", "-C", str(tmp_path / "repo"), "worktree", "remove", "--force", str(daemon.w1)],
            check=False,
        )
        shutil.rmtree(daemon.w1, ignore_errors=True)
        assert not daemon.w1.exists()

        # New dispatch with an EXISTING workspace (W2) must launch and run.
        session_c = _launch_runner_for_session(
            http_client, host_id=daemon.host_id, agent_id=agent_id, workspace=daemon.w2
        )
        body_c = _run_pwd_turn(http_client, session_id=session_c)
        output = _turn_output_text(body_c)
        assert body_c.get("status") in ("idle", "completed"), (
            f"Post-deletion dispatch failed: {body_c.get('status')}; output: {output[:600]}"
        )
        assert "Errno 2" not in output and "turn setup failed" not in output, (
            f"Post-deletion dispatch hit the dead-cwd failure: {output[:600]}"
        )
        assert str(daemon.w2) in output, (
            f"Post-deletion dispatch did not run in its own workspace W2: {output[:600]}"
        )
    finally:
        _terminate(daemon.proc)
