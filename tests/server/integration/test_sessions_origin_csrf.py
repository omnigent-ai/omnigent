"""
Integration tests for the Origin CSRF guard on the multipart session POSTs.

Two POST routes accept ``multipart/form-data``: ``POST /v1/sessions`` (the
bundled-create path) and ``POST /v1/sessions/{id}/resources/files`` (file
upload). ``multipart/form-data`` is CORS-safelisted, so the JSON
Content-Type guard cannot stop a cross-site upload — a malicious page can
``fetch`` a ``FormData`` body to either route with no preflight. The
``require_trusted_origin`` dependency closes that gap: a present ``Origin``
must be trusted. (An absent ``Origin`` currently fails open — a temporary
backward-compat posture, to be closed — so these tests assert the cases
that hold regardless: a present cross-site Origin is rejected, a present
trusted Origin passes.)

These tests drive the real routes through the shared ``client`` fixture
(real stores + mock LLM, permissions disabled). The suite runs in local
single-user mode (``OMNIGENT_LOCAL_SINGLE_USER=1`` from
``tests/conftest.py``), so the guard's local-mode branch is active, and an
autouse fixture stamps the first-party sentinel ``Origin`` on in-process
requests that don't set their own (emulating the SDK / runner). The tests
set explicit Origins to assert:

- a cross-site ``Origin`` returns 403 on both routes (the attack),
- a loopback ``Origin`` and the first-party sentinel still reach the
  handler and succeed (the legitimate local UI / first-party client).

The absent-``Origin`` (fail-open) behavior is covered directly and
deterministically by the unit test
``tests/server/routes/test_origin.py::test_absent_origin_is_allowed`` —
the suite-wide ASGI Origin injection makes a header-less request
unrepresentable here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio

# A concrete cross-site origin standing in for the attacker's page.
_EVIL_ORIGIN = "https://evil.example.com"
# A loopback origin — what the user's own local web UI sends.
_LOOPBACK_ORIGIN = "http://localhost:5173"


async def _create_session(client: httpx.AsyncClient) -> str:
    """
    Create a session over JSON and return its id.

    Relies on the suite-wide autouse fixture injecting the sentinel Origin
    on this in-process client, so the create itself passes the guard. Gives
    the file-upload tests a real session to target.

    :param client: The test HTTP client (sends the sentinel Origin).
    :returns: The new session/conversation id, e.g. ``"conv_abc123"``.
    """
    agent = await create_test_agent(client)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── create_session (multipart bundled-create) ──


async def test_create_session_multipart_rejects_cross_origin(
    client: httpx.AsyncClient,
) -> None:
    """
    A cross-site ``Origin`` on the multipart create returns 403.

    This is the core CSRF attack: a ``multipart/form-data`` bundle upload
    from a hostile page. Before ``require_trusted_origin`` the multipart
    Content-Type was CORS-safelisted and the bundle would be created. A
    non-403 here means the guard is not wired onto the route.
    """
    bundle = build_agent_bundle(name="csrf-origin-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": _EVIL_ORIGIN},
    )
    assert resp.status_code == 403, (
        f"multipart create accepted a cross-site Origin (status {resp.status_code}); "
        "the require_trusted_origin guard did not fire."
    )
    # Confirm it's the origin guard's 403, not an unrelated auth/permission 403.
    assert "origin" in resp.text.lower()


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param(_LOOPBACK_ORIGIN, id="loopback"),
        pytest.param(OMNIGENT_INTERNAL_WS_ORIGIN, id="sentinel"),
    ],
)
async def test_create_session_multipart_allows_trusted_origin(
    client: httpx.AsyncClient,
    origin: str,
) -> None:
    """
    A loopback or sentinel ``Origin`` still bundled-creates (201).

    The legitimate paths must keep working: the local web UI sends a
    loopback Origin, and first-party non-browser clients send the sentinel.
    A 403 here would mean the guard over-blocks and breaks the local UI or
    the SDK / runner bundle-create path.
    """
    bundle = build_agent_bundle(name="csrf-ok-agent")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": origin},
    )
    assert resp.status_code == 201, resp.text
    # The bundled-create response carries the new session id → the request
    # passed the guard and reached the bundled-create branch.
    assert "session_id" in resp.json()


# ── upload_session_file (multipart file upload) ──


async def test_upload_file_rejects_cross_origin(client: httpx.AsyncClient) -> None:
    """
    A cross-site ``Origin`` on the file upload returns 403.

    The file-upload route only accepts multipart, so the Content-Type guard
    can't protect it; require_trusted_origin must. A non-403 means a hostile
    page could write files into another user's session.
    """
    session_id = await _create_session(client)
    resp = await client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("evil.txt", b"pwned", "text/plain")},
        headers={"Origin": _EVIL_ORIGIN},
    )
    assert resp.status_code == 403, (
        f"file upload accepted a cross-site Origin (status {resp.status_code}); "
        "the require_trusted_origin guard did not fire."
    )
    # Confirm it's the origin guard's 403, not an unrelated auth/permission 403.
    assert "origin" in resp.text.lower()


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param(_LOOPBACK_ORIGIN, id="loopback"),
        pytest.param(OMNIGENT_INTERNAL_WS_ORIGIN, id="sentinel"),
    ],
)
async def test_upload_file_allows_trusted_origin(
    client: httpx.AsyncClient,
    origin: str,
) -> None:
    """
    A loopback or sentinel ``Origin`` still uploads the file (201).

    Proves the guard is transparent to the legitimate local UI (loopback)
    and first-party clients (sentinel): the upload reaches the handler and
    the stored file resource is returned. A 403 would mean the guard breaks
    real uploads.
    """
    session_id = await _create_session(client)
    resp = await client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
        headers={"Origin": origin},
    )
    assert resp.status_code == 201, resp.text
    # The created file resource echoes the filename → the upload traversed
    # the full handler, not just the gate.
    body = resp.json()
    assert body["name"] == "hello.txt"
    assert body["metadata"]["filename"] == "hello.txt"
