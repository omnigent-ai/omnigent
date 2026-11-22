"""E2E: a daemon's session lookup lists only the caller's own sessions.

``omnigent host status --sessions`` finds a daemon's sessions by listing
sessions and keeping the ones bound to the daemon's host. A host only runs
sessions its owner started, but the lookup listed ``visibility=all``: every
session the caller can read, other users' shared sessions included, which on a
busy multi-user server is most of what the server scans. The lookup must list
the caller's own sessions and still report the host's archived sessions.

The test drives the real journey against a real server (header auth) and a
real host daemon: two sessions launched on the host, one then archived, plus
another user's session shared with the host's owner. The real ``host status``
must report both host sessions, ``host stop`` must drain and stop the daemon,
and the server's access log must show that every session list either command
sent asked for ``visibility=mine``.

No turn runs, so no LLM credentials are needed::

    .venv/bin/python -m pytest \
        tests/e2e/test_host_status_owner_scoped_session_lookup_e2e.py -v
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests._helpers.live_server import isolated_local_server, upload_agent
from tests.e2e.conftest import lookup_agent_id
from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online

pytestmark = pytest.mark.timeout(300)

# Mirrored from omnigent/server/auth.py so a renumbering fails loudly.
_LEVEL_READ = 1
# A uvicorn access line for a session list sent by the CLI, e.g.
# ``"GET /v1/sessions?limit=1000&... HTTP/1.1" 200 OK 6.0ms rid=... ua="omnigent-cli/..."``.
_CLI_SESSION_LIST_REQUEST = re.compile(
    r'"GET (/v1/sessions\?[^ "]+) HTTP/[^\n]* ua="omnigent-cli/'
)


def _write_agent(parent: Path, name: str) -> Path:
    """
    Write a minimal agent bundle named *name* under *parent*.

    :param parent: Directory to create the bundle in.
    :param name: Agent name, unique per uploader, e.g. ``"scope-e2e-owner"``.
    :returns: The bundle directory.
    """
    agent_dir = parent / name
    agent_dir.mkdir(parents=True)
    (agent_dir / f"{name}.yaml").write_text(
        f"name: {name}\n"
        "description: Minimal agent for the host session lookup e2e.\n"
        "executor:\n"
        "  harness: openai-agents\n"
        "  model: gpt-5.4\n"
        "prompt: You are a terse assistant.\n"
    )
    return agent_dir


def _listed_ids(client: httpx.Client, *, visibility: str) -> set[str]:
    """
    Return the ids of every session *client* lists under *visibility*.

    :param client: Client authenticated as the listing user.
    :param visibility: Session-list scope, e.g. ``"all"`` or ``"mine"``.
    :returns: Listed session ids, archived ones included.
    """
    resp = client.get(
        "/v1/sessions",
        params={"visibility": visibility, "include_archived": "true", "limit": 1000},
    )
    resp.raise_for_status()
    return {str(row["id"]) for row in resp.json()["data"]}


def _share_another_users_session(base_url: str, tmp_path: Path) -> str:
    """
    Create a session as another user and grant the host owner read access.

    :param base_url: Server base URL.
    :param tmp_path: Directory for the other user's agent bundle.
    :returns: The shared session id.
    """
    email = f"bob-{uuid.uuid4().hex[:6]}@e2e.test"
    with httpx.Client(
        base_url=base_url, headers={"X-Forwarded-Email": email}, timeout=60.0, trust_env=False
    ) as bob:
        # The bundle upload creates a session owned by the uploader.
        upload_agent(bob, _write_agent(tmp_path / "bob", "scope-e2e-bob"))
        (session_id,) = _listed_ids(bob, visibility="mine")
        grant = bob.put(
            f"/v1/sessions/{session_id}/permissions",
            json={"user_id": "local", "level": _LEVEL_READ},
        )
        assert grant.status_code == 200, grant.text
    return session_id


def _server_log_file(server_dir: Path) -> Path:
    """
    Return the log file the isolated server writes its access lines to.

    :param server_dir: Directory passed to :func:`isolated_local_server`.
    :returns: The newest ``server-*.log`` under the server's home.
    """
    logs = sorted(
        (server_dir / "server-home" / ".omnigent" / "logs" / "server").glob("server-*.log")
    )
    assert logs, f"the server wrote no log file under {server_dir}"
    return logs[-1]


def _cli_session_list_scopes(server_log: Path, offset: int) -> list[str]:
    """
    Return the ``visibility`` of each CLI session list logged after *offset*.

    :param server_log: The server's log file.
    :param offset: Byte offset to start reading from.
    :returns: One visibility value per request, in log order; a request
        without the parameter counts as the server default, ``"all"``.
    """
    with server_log.open("rb") as log:
        log.seek(offset)
        text = log.read().decode(errors="replace")
    return [
        parse_qs(urlsplit(match.group(1)).query).get("visibility", ["all"])[0]
        for match in _CLI_SESSION_LIST_REQUEST.finditer(text)
    ]


def _wait_for_cli_session_lists(server_log: Path, offset: int, *, count: int) -> list[str]:
    """
    Wait until *count* CLI session lists are logged after *offset*.

    Uvicorn writes the access line as the response goes out, so it can trail
    the CLI's exit briefly.

    :param server_log: The server's log file.
    :param offset: Byte offset to start reading from.
    :param count: Number of requests the CLI command sends, e.g. ``2``.
    :returns: The logged scopes; fewer than *count* after a 10s wait.
    """
    deadline = time.monotonic() + 10.0
    scopes = _cli_session_list_scopes(server_log, offset)
    while len(scopes) < count and time.monotonic() < deadline:
        time.sleep(0.2)
        scopes = _cli_session_list_scopes(server_log, offset)
    return scopes


def _run_cli(daemon_home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """
    Run the real CLI as the host's owner against the daemon's registry.

    :param daemon_home: ``HOME`` the daemon wrote its registry record under.
    :param args: CLI arguments, e.g. ``("host", "status", "--json")``.
    :returns: The finished process, with text output captured.
    """
    return subprocess.run(
        [sys.executable, "-m", "omnigent", *args],
        env={**os.environ, "HOME": str(daemon_home)},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_host_status_and_stop_list_only_the_owners_sessions(tmp_path: Path) -> None:
    """Host lookups find archived host sessions without listing shared ones."""
    server_dir = tmp_path / "server"
    daemon_home = tmp_path / "daemon-home"
    workspace = daemon_home / "work"
    workspace.mkdir(parents=True)
    with (
        isolated_local_server(server_dir) as base_url,
        httpx.Client(base_url=base_url, timeout=60.0, trust_env=False) as owner,
    ):
        daemon = _spawn_host_daemon(
            tmp_path=daemon_home,
            live_server=base_url,
            # No turn runs, so the runners never reach this LLM endpoint.
            mock_llm_server_url="http://127.0.0.1:9",
        )
        try:
            _wait_for_host_online(owner, daemon.host_id, timeout=60.0)
            agent_id = lookup_agent_id(
                owner, upload_agent(owner, _write_agent(tmp_path / "owner", "scope-e2e-owner"))
            )
            host_sessions = []
            for _ in range(2):
                created = owner.post(
                    "/v1/sessions",
                    json={
                        "agent_id": agent_id,
                        "host_id": daemon.host_id,
                        "workspace": str(workspace),
                    },
                )
                assert created.status_code in (200, 201), created.text
                host_sessions.append(str(created.json()["id"]))
            archived = owner.patch(f"/v1/sessions/{host_sessions[1]}", json={"archived": True})
            assert archived.status_code == 200, archived.text

            shared_id = _share_another_users_session(base_url, tmp_path)
            # The owner can read the shared session, so listing everything includes it.
            assert shared_id in _listed_ids(owner, visibility="all")
            assert shared_id not in _listed_ids(owner, visibility="mine")

            server_log = _server_log_file(server_dir)
            offset = server_log.stat().st_size
            status = _run_cli(daemon_home, "host", "status", "--sessions", "--json")
            assert status.returncode == 0, status.stdout + status.stderr
            (payload,) = [
                daemon_payload
                for daemon_payload in json.loads(status.stdout)["daemons"]
                if daemon_payload["host_id"] == daemon.host_id
            ]
            assert payload["error"] is None, payload
            reported = {str(session["id"]) for session in payload["sessions"]}
            assert set(host_sessions) <= reported, (
                f"host status dropped a host session (the second is archived): "
                f"expected {host_sessions}, got {sorted(reported)}"
            )
            assert shared_id not in reported
            status_scopes = _wait_for_cli_session_lists(server_log, offset, count=1)
            assert status_scopes, "the server logged no session list from host status"
            assert set(status_scopes) == {"mine"}, (
                f"host status listed sessions with visibility={status_scopes}; a host only "
                "runs its owner's sessions, so the lookup must not scan everyone's shared ones"
            )

            # Stopping drains the host's sessions through the same lookup, for
            # root sessions and their sub-agents.
            offset = server_log.stat().st_size
            stop = _run_cli(daemon_home, "host", "stop", "--server", base_url)
            assert stop.returncode == 0, stop.stdout + stop.stderr
            assert "sessions_stopped=" in stop.stdout, stop.stdout
            daemon.proc.wait(timeout=30)
            stop_scopes = _wait_for_cli_session_lists(server_log, offset, count=2)
            assert len(stop_scopes) >= 2, f"host stop logged session lists {stop_scopes}"
            assert set(stop_scopes) == {"mine"}, (
                f"host stop listed sessions with visibility={stop_scopes} to drain the host"
            )
        finally:
            daemon.proc.send_signal(signal.SIGTERM)
            try:
                daemon.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.proc.kill()
                daemon.proc.wait(timeout=5)
