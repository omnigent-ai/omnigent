"""End-to-end user-preferences API coverage against a real server process."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_healthy(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"server exited with {process.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("server did not become healthy within 30 seconds")


def test_preferences_follow_an_authenticated_user_across_clients(tmp_path: Path) -> None:
    """A real CLI server persists one user's settings across HTTP clients."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "OMNIGENT_AUTH_ENABLED": "1",
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_AUTH_HEADER": "X-Test-User",
            "OMNIGENT_CONFIG": str(config_path),
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_SKIP_WEB_UI": "true",
        }
    )
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
            f"sqlite:///{tmp_path / 'preferences-e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
            "--no-open",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_until_healthy(base_url, process)
        headers = {"X-Test-User": "alice@example.com"}
        with httpx.Client(base_url=base_url, headers=headers) as first_client:
            initialized = first_client.put(
                "/v1/me/preferences",
                json={
                    "version": 1,
                    "settings": {"keyboard_shortcuts": {"enabled": False}},
                },
            )
            assert initialized.status_code == 200

        with httpx.Client(base_url=base_url, headers=headers) as second_client:
            me = second_client.get("/v1/me")
            assert me.status_code == 200
            assert me.json()["preferences"] == initialized.json()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
