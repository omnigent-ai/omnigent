"""Accounts revocation through two real CLI servers sharing a database."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import httpx

from tests.server.helpers import build_agent_bundle


def test_delete_revokes_cookie_and_refresh_on_another_server(tmp_path: Path) -> None:
    """Exercise CLI configuration, bootstrap, HTTP auth, and shared DB cleanup."""
    processes: list[subprocess.Popen] = []
    repo = Path(__file__).resolve().parents[3]
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_")}
    env.update(
        {
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
            "OMNIGENT_SKIP_WEB_UI": "true",
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "OMNIGENT_AUTH_PROVIDER": "accounts",
            "OMNIGENT_ACCOUNTS_COOKIE_SECRET": "ab" * 32,
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": "admin",
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": "admin-pw-12345",
            "OMNIGENT_ACCOUNTS_AUTO_OPEN": "0",
            "OMNIGENT_ADMIN_CREDENTIALS_PATH": str(tmp_path / "admin-creds"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        }
    )
    # Child-only home prevents the loopback CLI bootstrap touching the operator's tokens.
    env["HOME"] = str(tmp_path)
    with ExitStack() as stack:
        clients = []
        try:
            for index in range(2):
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                base = f"http://127.0.0.1:{port}"
                env["OMNIGENT_ACCOUNTS_BASE_URL"] = base
                log_path = tmp_path / f"server-{index}.log"
                log = stack.enter_context(log_path.open("w"))
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "omnigent",
                        "server",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--database-uri",
                        f"sqlite:///{tmp_path / 'test.db'}",
                        "--artifact-location",
                        str(tmp_path / "artifacts"),
                    ],
                    cwd=repo,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(process)
                client = stack.enter_context(httpx.Client(base_url=base, timeout=5))
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    assert process.poll() is None, log_path.read_text()
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError(log_path.read_text())
                clients.append(client)
            admin, alice = clients
            assert (
                admin.post(
                    "/auth/login", json={"username": "admin", "password": "admin-pw-12345"}
                ).status_code
                == 200
            )
            invite = admin.post("/auth/invite", json={}).json()["token"]
            assert (
                alice.post(
                    "/auth/register",
                    json={"invite": invite, "username": "alice", "password": "alice-pw-1234"},
                ).status_code
                == 200
            )
            login = alice.post(
                "/auth/login",
                json={"username": "alice", "password": "alice-pw-1234", "issue_refresh": True},
            )
            assert login.status_code == 200, login.text
            refresh = login.json()["refresh_token"]
            assert alice.get("/v1/sessions").status_code == 200
            assert admin.delete("/auth/users/alice").status_code == 204
            assert alice.get("/v1/sessions").status_code == 401
            denied = alice.post(
                "/v1/sessions",
                data={"metadata": "{}"},
                files={
                    "bundle": (
                        "agent.tar.gz",
                        build_agent_bundle("revocation-probe"),
                        "application/gzip",
                    )
                },
            )
            assert denied.status_code == 401, denied.text
            refreshed = alice.post(
                "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
            )
            assert refreshed.status_code == 400 and refreshed.json()["error"] == "invalid_grant"
            assert "alice" not in {row["id"] for row in admin.get("/auth/users").json()["users"]}
            invite = admin.post("/auth/invite", json={}).json()["token"]
            with httpx.Client(base_url=str(alice.base_url)) as replacement:
                registered = replacement.post(
                    "/auth/register",
                    json={
                        "invite": invite,
                        "username": "alice",
                        "password": "different-password-123",
                    },
                )
                assert registered.status_code == 200, registered.text
                assert replacement.get("/auth/me").status_code == 200
            assert alice.get("/auth/me").status_code == 401
        finally:
            for process in reversed(processes):
                process.terminate()
            for process in reversed(processes):
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
