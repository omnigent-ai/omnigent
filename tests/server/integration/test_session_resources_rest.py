"""Integration tests for session resource REST endpoints.

Exercises the ``/v1/sessions/{id}/resources`` surface: listing
resources, environments, files (upload / download / delete), and the
"no runner bound" error paths for runner-proxied endpoints
(filesystem, shell, search).

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM, no runner) so the tests hit the real
route-to-store pipeline without subprocesses.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Helpers ───────────────────────────────────────────────


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
) -> dict[str, Any]:
    """Create a minimal session and return the JSON response.

    :param client: The test HTTP client.
    :param agent_id: Agent to bind, e.g. ``"ag_abc123"``.
    :returns: The ``POST /v1/sessions`` response body.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── 1. List resources (empty) ─────────────────────────────


async def test_list_resources_returns_paginated_shape(client: httpx.AsyncClient) -> None:
    """GET /v1/sessions/{id}/resources returns a well-formed paginated list."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.get(f"/v1/sessions/{sid}/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["data"], list)
    assert "has_more" in body
    # Every resource entry must carry the standard object type.
    for item in body["data"]:
        assert item["object"] == "session.resource"


# ── 2. List environments (no runner -> 502) ───────────────


async def test_list_environments_no_runner(client: httpx.AsyncClient) -> None:
    """GET /v1/sessions/{id}/resources/environments returns 502 without a runner."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.get(f"/v1/sessions/{sid}/resources/environments")
    assert resp.status_code == 502


# ── 3. List files (empty) ─────────────────────────────────


async def test_list_files_empty(client: httpx.AsyncClient) -> None:
    """GET /v1/sessions/{id}/resources/files returns an empty list for a fresh session."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.get(f"/v1/sessions/{sid}/resources/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


# ── 4. File upload + download ─────────────────────────────


async def test_file_upload_and_download(client: httpx.AsyncClient) -> None:
    """POST then GET content round-trips file bytes correctly."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Upload
    upload_resp = await client.post(
        f"/v1/sessions/{sid}/resources/files",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )
    assert upload_resp.status_code == 201, upload_resp.text
    file_resource = upload_resp.json()
    assert file_resource["name"] == "hello.txt"
    file_id = file_resource["id"]

    # File appears in list
    list_resp = await client.get(f"/v1/sessions/{sid}/resources/files")
    assert list_resp.status_code == 200
    ids = [f["id"] for f in list_resp.json()["data"]]
    assert file_id in ids

    # Download content
    dl_resp = await client.get(
        f"/v1/sessions/{sid}/resources/files/{file_id}/content",
    )
    assert dl_resp.status_code == 200
    assert dl_resp.content == b"hello world"

    # File also appears in unified resources list
    res_resp = await client.get(f"/v1/sessions/{sid}/resources")
    assert res_resp.status_code == 200
    res_ids = [r["id"] for r in res_resp.json()["data"]]
    assert file_id in res_ids


# ── 5. File delete ────────────────────────────────────────


async def test_file_delete(client: httpx.AsyncClient) -> None:
    """DELETE removes the file; subsequent GET returns 404."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    upload_resp = await client.post(
        f"/v1/sessions/{sid}/resources/files",
        files={"file": ("bye.txt", b"gone", "text/plain")},
    )
    assert upload_resp.status_code == 201
    file_id = upload_resp.json()["id"]

    del_resp = await client.delete(
        f"/v1/sessions/{sid}/resources/files/{file_id}",
    )
    assert del_resp.status_code == 200
    del_body = del_resp.json()
    assert del_body["deleted"] is True
    assert del_body["id"] == file_id

    # Confirm gone
    get_resp = await client.get(
        f"/v1/sessions/{sid}/resources/files/{file_id}",
    )
    assert get_resp.status_code == 404


# ── 6. 502 when no runner bound for nonexistent single resource ──


async def test_get_nonexistent_resource_returns_error(
    client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions/{id}/resources/{bogus} returns 502 (no runner)."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.get(f"/v1/sessions/{sid}/resources/does-not-exist")
    # Without a runner the proxy returns 502.
    assert resp.status_code == 502


# ── 7. Filesystem proxy: no runner -> 502 ─────────────────


async def test_filesystem_list_no_runner(client: httpx.AsyncClient) -> None:
    """GET .../environments/default/filesystem returns 502 without a runner."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.get(
        f"/v1/sessions/{sid}/resources/environments/default/filesystem",
    )
    assert resp.status_code == 502


# ── 8. Search proxy: no runner -> 502 ─────────────────────


async def test_search_no_runner(client: httpx.AsyncClient) -> None:
    """GET .../environments/default/search returns 502 without a runner."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.get(
        f"/v1/sessions/{sid}/resources/environments/default/search",
        params={"q": "foo"},
    )
    assert resp.status_code == 502


# ── 9. Shell proxy: no runner -> 502 ──────────────────────


async def test_shell_no_runner(client: httpx.AsyncClient) -> None:
    """POST .../environments/default/shell returns 502 without a runner."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    resp = await client.post(
        f"/v1/sessions/{sid}/resources/environments/default/shell",
        json={"command": "echo hi"},
    )
    assert resp.status_code == 502


# ── 10. Nonexistent session -> 404 ────────────────────────


async def test_resources_nonexistent_session(client: httpx.AsyncClient) -> None:
    """Resource endpoints return 404 for a session that does not exist."""
    resp = await client.get("/v1/sessions/conv_bogus/resources")
    assert resp.status_code == 404

    resp = await client.get("/v1/sessions/conv_bogus/resources/files")
    assert resp.status_code == 404
