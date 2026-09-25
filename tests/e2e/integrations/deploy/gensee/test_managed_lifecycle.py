"""Optional live end-to-end test for the Gensee managed-host lifecycle.

The test targets an existing Omnigent server configured with the built-in
Gensee provider. It creates one managed session without sending an LLM prompt,
waits for the sandbox host and runner to connect, deletes that exact session,
and verifies that the corresponding Gensee allocation is released.

This test is opt-in because it provisions a billable external sandbox. The
agent must use a harness available in the Gensee runtime image.
``OMNIGENT_E2E_GENSEE_SERVER_TOKEN`` is optional for auth-disabled servers and
is sent as an Omnigent server Bearer token when present.

    OMNIGENT_E2E_GENSEE=1 \
    OMNIGENT_E2E_GENSEE_SERVER_URL=https://omnigent.example.com \
    OMNIGENT_E2E_GENSEE_AGENT_ID=ag_example \
    OMNIGENT_E2E_GENSEE_SERVER_TOKEN=... \
    GENSEE_CONTROLLER_API_TOKEN=... \
    .venv/bin/python -m pytest tests/e2e/integrations/deploy/gensee/test_managed_lifecycle.py -v
"""

from __future__ import annotations

import os
import time
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import pytest

from omnigent.onboarding.sandboxes.gensee import (
    DEFAULT_WORKSPACE_ROOT,
    GenseeSandboxLauncher,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_GENSEE") != "1",
        reason="set OMNIGENT_E2E_GENSEE=1 to provision a live Gensee sandbox",
    ),
    pytest.mark.timeout(1200),
]

_READY_TIMEOUT_S = 900.0
_RELEASE_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 5.0


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} must be set when OMNIGENT_E2E_GENSEE=1")
    return value


def _server_headers() -> dict[str, str]:
    token = os.environ.get("OMNIGENT_E2E_GENSEE_SERVER_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _assert_gensee_enabled(client: httpx.Client) -> None:
    response = client.get("/v1/info")
    response.raise_for_status()
    info = response.json()
    providers = info.get("sandbox_providers") or []
    assert "gensee" in providers or info.get("sandbox_provider") == "gensee", (
        "the target Omnigent server does not advertise Gensee as a managed sandbox provider"
    )


def _assert_agent_exists(client: httpx.Client, agent_id: str) -> None:
    response = client.get("/v1/agents")
    response.raise_for_status()
    agents = response.json().get("data") or []
    assert any(agent.get("id") == agent_id for agent in agents), (
        f"agent {agent_id!r} is not registered on the target Omnigent server"
    )


def _wait_until_ready(client: httpx.Client, session_id: str) -> dict[str, object]:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_snapshot: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(
            f"/v1/sessions/{quote(session_id, safe='')}",
            params={"include_items": "false"},
        )
        response.raise_for_status()
        last_snapshot = response.json()
        sandbox_status = last_snapshot.get("sandbox_status") or {}
        if isinstance(sandbox_status, dict) and sandbox_status.get("stage") == "failed":
            pytest.fail(f"Gensee managed launch failed: {sandbox_status.get('error')}")
        if last_snapshot.get("host_online") is True and last_snapshot.get("runner_online") is True:
            return last_snapshot
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"Gensee host and runner did not become ready within {_READY_TIMEOUT_S:.0f}s; "
        f"last sandbox status was {last_snapshot.get('sandbox_status')!r}"
    )


def _sandbox_id_from_snapshot(snapshot: dict[str, object]) -> str:
    workspace = snapshot.get("workspace")
    assert isinstance(workspace, str) and workspace, "ready session did not report its workspace"
    workspace_path = PurePosixPath(workspace)
    expected_root = PurePosixPath(
        os.environ.get("OMNIGENT_E2E_GENSEE_WORKSPACE_ROOT", DEFAULT_WORKSPACE_ROOT)
    )
    assert workspace_path.parent == expected_root, (
        f"session workspace {workspace!r} is not directly beneath the expected "
        f"Gensee workspace root {str(expected_root)!r}"
    )
    allocation_id = workspace_path.name
    return f"gensee+controller:///{quote(allocation_id, safe='')}"


def _wait_until_released(launcher: GenseeSandboxLauncher, sandbox_id: str) -> None:
    deadline = time.monotonic() + _RELEASE_TIMEOUT_S
    while time.monotonic() < deadline:
        if launcher.is_running(sandbox_id) is False:
            return
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"Gensee allocation {sandbox_id!r} was still active "
        f"{_RELEASE_TIMEOUT_S:.0f}s after session deletion"
    )


def test_gensee_managed_session_lifecycle() -> None:
    """A managed session provisions, becomes ready, and releases its allocation."""
    server_url = _required_env("OMNIGENT_E2E_GENSEE_SERVER_URL").rstrip("/")
    agent_id = _required_env("OMNIGENT_E2E_GENSEE_AGENT_ID")
    _required_env("GENSEE_CONTROLLER_API_TOKEN")

    session_id: str | None = None
    deleted = False
    with httpx.Client(
        base_url=f"{server_url}/",
        headers=_server_headers(),
        timeout=30.0,
    ) as client:
        _assert_gensee_enabled(client)
        _assert_agent_exists(client, agent_id)
        try:
            response = client.post(
                "/v1/sessions",
                json={
                    "agent_id": agent_id,
                    "host_type": "managed",
                    "sandbox_provider": "gensee",
                    "title": f"Gensee managed lifecycle E2E {int(time.time())}",
                },
            )
            response.raise_for_status()
            session_id = response.json()["id"]

            snapshot = _wait_until_ready(client, session_id)
            assert isinstance(snapshot.get("host_id"), str)
            sandbox_id = _sandbox_id_from_snapshot(snapshot)

            launcher = GenseeSandboxLauncher()
            assert launcher.is_running(sandbox_id) is True

            response = client.delete(
                f"/v1/sessions/{quote(session_id, safe='')}",
                timeout=120.0,
            )
            response.raise_for_status()
            assert response.json() == {
                "id": session_id,
                "object": "conversation.deleted",
                "deleted": True,
            }
            deleted = True

            _wait_until_released(launcher, sandbox_id)
            assert client.get(f"/v1/sessions/{quote(session_id, safe='')}").status_code == 404
        finally:
            if session_id is not None and not deleted:
                cleanup = client.delete(
                    f"/v1/sessions/{quote(session_id, safe='')}",
                    timeout=120.0,
                )
                assert cleanup.status_code in {200, 404}, (
                    f"cleanup failed for session {session_id!r}: HTTP {cleanup.status_code}"
                )
