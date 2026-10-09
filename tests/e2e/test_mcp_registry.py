"""Real HTTP MCP demo fixture and OAuth callback allowlist without external accounts."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def demo_mcp(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[2]
    with (tmp_path / "demo.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(root / "examples/mcp-registry/demo_service.py"),
                "--port",
                str(port),
                "--redirect-uri",
                "http://localhost/v1/connections/mcp-tracker/callback",
            ],
            stdout=log,
            stderr=log,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                try:
                    if httpx.get(url + "/health", timeout=0.2).status_code == 200:
                        break
                except httpx.HTTPError:
                    # Connection/read failures are expected while the server starts.
                    pass
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                time.sleep(0.1)
            else:
                pytest.fail("Demo MCP service did not start")
            yield url
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_demo_authorization_requires_registered_callback(demo_mcp):
    response = httpx.post(
        demo_mcp + "/authorize",
        data={
            "redirect_uri": "http://localhost/unregistered-callback",
            "state": "test-state",
            "code_challenge_method": "S256",
            "code_challenge": "test-challenge",
        },
    )
    assert response.status_code == 400
    assert "location" not in response.headers
