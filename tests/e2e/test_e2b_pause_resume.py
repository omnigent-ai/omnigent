"""Live E2B pause and in-place wake against an explicitly configured test server.

Set OMNIGENT_E2B_PAUSE_E2E=1 and OMNIGENT_E2B_PAUSE_SERVER_URL, plus E2B_API_KEY
(and E2B_DOMAIN outside the default region) for the account the server uses. The
server must use ``sandbox.provider: e2b`` with ``sandbox.e2b.on_timeout: pause``.
It has to be reachable from E2B, so give it accounts auth and set
OMNIGENT_E2B_PAUSE_USERNAME and OMNIGENT_E2B_PAUSE_PASSWORD; leave both unset only
for a server that accepts unauthenticated requests. The real host and claude-sdk
runner start without submitting a model prompt.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import tarfile
import time
import uuid
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2B_PAUSE_E2E") != "1",
        reason="set OMNIGENT_E2B_PAUSE_E2E=1 and the explicit test server settings",
    ),
    pytest.mark.timeout(1200),
]

# Match argv tokens so the supervisor's embedded shell command is excluded.
_HOST_PIDS = "python3 -c " + shlex.quote(
    "import os, psutil\n"
    "for p in psutil.process_iter(['pid', 'cmdline']):\n"
    "    args = p.info['cmdline'] or []\n"
    "    if any(os.path.basename(a) == 'omnigent' and b == 'host' "
    "for a, b in zip(args, args[1:])):\n"
    "        print(p.pid)\n"
)


def _bundle() -> bytes:
    spec = (
        b"spec_version: 1\nname: e2b-pause-e2e\n"
        b"executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n"
        b"prompt: Help the user inspect their sandbox workspace.\n"
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("config.yaml")
        entry.size = len(spec)
        archive.addfile(entry, io.BytesIO(spec))
    return buffer.getvalue()


def _wait_online(client: httpx.Client, session_path: str, what: str) -> dict[str, Any]:
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        response = client.get(session_path)
        response.raise_for_status()
        session = response.json()
        assert (session.get("sandbox_status") or {}).get("stage") != "failed", (
            f"Managed {what} failed for {session_path}"
        )
        if session.get("host_online") and session.get("runner_online"):
            return session
        time.sleep(1)
    pytest.fail(f"Managed host/runner did not register after {what}")


def test_pause_resumes_same_sandbox_with_workspace_and_one_host() -> None:
    """Force a pause, wake with retry_session, and keep the sandbox id and files."""
    from e2b import Sandbox, SandboxQuery, SandboxState
    from e2b.exceptions import NotFoundException

    server_url = os.environ.get("OMNIGENT_E2B_PAUSE_SERVER_URL", "").strip().rstrip("/")
    assert server_url, "OMNIGENT_E2B_PAUSE_SERVER_URL must select the test server"

    def sandbox_for(host_name: str) -> str:
        query = SandboxQuery(
            metadata={"omnigent-name": host_name},
            state=[SandboxState.RUNNING, SandboxState.PAUSED],
        )
        sandboxes = Sandbox.list(query=query).next_items()
        assert len(sandboxes) == 1, f"Expected one sandbox for {host_name}"
        return sandboxes[0].sandbox_id

    def host_pids(sandbox: Sandbox) -> set[str]:
        return set(sandbox.commands.run(_HOST_PIDS).stdout.split())

    with httpx.Client(base_url=server_url, timeout=15) as client:
        username = os.environ.get("OMNIGENT_E2B_PAUSE_USERNAME")
        if username:
            password = os.environ["OMNIGENT_E2B_PAUSE_PASSWORD"]
            client.post(
                "/auth/login", json={"username": username, "password": password}
            ).raise_for_status()
        response = client.post(
            "/v1/sessions",
            data={
                "metadata": json.dumps(
                    {
                        "title": f"E2B pause E2E {uuid.uuid4().hex[:8]}",
                        "host_type": "managed",
                        "sandbox_provider": "e2b",
                    }
                )
            },
            files={"bundle": ("agent.tar.gz", _bundle(), "application/gzip")},
            timeout=60,
        )
        response.raise_for_status()
        session_id = response.json()["session_id"]
        session_path = f"/v1/sessions/{session_id}"
        sandbox_id: str | None = None
        try:
            session = _wait_online(client, session_path, "launch")
            host_id = session["host_id"]
            host = client.get(f"/v1/hosts/{host_id}")
            host.raise_for_status()
            assert host.json()["sandbox_provider"] == "e2b"
            sandbox_id = sandbox_for(host.json()["name"])

            sandbox = Sandbox.connect(sandbox_id)
            home = sandbox.commands.run('printf %s "$HOME"').stdout
            marker = f"{home}/workspace/E2B-PAUSE-E2E-{uuid.uuid4().hex}"
            content = "e2b workspace survives pause\n"
            sandbox.files.write(marker, content)
            assert len(host_pids(sandbox)) == 1, "Exactly one managed host must be running"

            Sandbox.pause(sandbox_id)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                response = client.get(session_path)
                response.raise_for_status()
                session = response.json()
                if not session.get("host_online") and not session.get("runner_online"):
                    break
                time.sleep(2)
            else:
                pytest.fail("The server still reports the paused host or runner online")
            assert Sandbox.get_info(sandbox_id).state == SandboxState.PAUSED

            response = client.post(
                f"{session_path}/events",
                json={"type": "retry_session", "data": {}},
                timeout=420,
            )
            response.raise_for_status()
            session = _wait_online(client, session_path, "wake")
            assert session["host_id"] == host_id
            assert sandbox_for(host.json()["name"]) == sandbox_id
            assert Sandbox.get_info(sandbox_id).state == SandboxState.RUNNING

            sandbox = Sandbox.connect(sandbox_id)
            assert sandbox.files.read(marker) == content
            woken_pids = host_pids(sandbox)
            # A restored host that reconnects before re-arming may keep serving.
            assert len(woken_pids) == 1, "The wake must leave exactly one managed host"
        finally:
            response = client.delete(session_path, timeout=30)
            response.raise_for_status()
        if sandbox_id is not None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    Sandbox.get_info(sandbox_id)
                except NotFoundException:
                    break
                time.sleep(2)
            else:
                pytest.fail("Deleting the session must kill its paused-capable sandbox")
