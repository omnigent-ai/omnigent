"""Integration tests for session archive lifecycle and agent contents download.

Covers:
- ``PATCH /v1/sessions/{id}`` with ``archived=True/False``
- ``GET /v1/sessions`` with ``include_archived`` filtering
- ``GET /v1/sessions/{id}/agent/contents`` returning a valid gzip tarball

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests hit the real route-to-store
pipeline without subprocesses.
"""

from __future__ import annotations

import gzip
import io
import tarfile

import httpx
import pytest

from tests.unit.server.helpers import create_test_session

pytestmark = pytest.mark.asyncio


# ── Archive / unarchive lifecycle ────────────────────────


async def test_session_not_archived_by_default(
    client: httpx.AsyncClient,
) -> None:
    """A freshly created session has ``archived=False``."""
    session = await create_test_session(client, name="archive-default")
    assert session["archived"] is False


async def test_archive_hides_session_from_default_listing(
    client: httpx.AsyncClient,
) -> None:
    """Archiving a session removes it from the default GET /v1/sessions listing."""
    session = await create_test_session(client, name="archive-hide")
    session_id = session["id"]

    # Archive it.
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["archived"] is True

    # Default listing (include_archived=False) should not contain it.
    listing = await client.get("/v1/sessions")
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id not in listed_ids


async def test_archived_session_appears_with_include_archived(
    client: httpx.AsyncClient,
) -> None:
    """An archived session is returned when ``include_archived=True``."""
    session = await create_test_session(client, name="archive-include")
    session_id = session["id"]

    await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
    )

    listing = await client.get("/v1/sessions", params={"include_archived": "true"})
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id in listed_ids


async def test_unarchive_restores_session_to_default_listing(
    client: httpx.AsyncClient,
) -> None:
    """Unarchiving a session makes it visible in the default listing again."""
    session = await create_test_session(client, name="archive-restore")
    session_id = session["id"]

    # Archive then unarchive.
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": False},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["archived"] is False

    # Back in the default listing.
    listing = await client.get("/v1/sessions")
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id in listed_ids


# ── Agent contents download ──────────────────────────────


async def test_agent_contents_returns_valid_gzip_tarball(
    client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions/{id}/agent/contents returns a valid tar.gz bundle."""
    session = await create_test_session(client, name="contents-download")
    session_id = session["id"]

    resp = await client.get(f"/v1/sessions/{session_id}/agent/contents")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"

    # Verify the bytes are valid gzip.
    decompressed = gzip.decompress(resp.content)
    assert len(decompressed) > 0

    # Verify the bytes are a valid tar archive containing config.yaml.
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tf:
        names = tf.getnames()
        assert "config.yaml" in names


async def test_agent_contents_404_for_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions/{id}/agent/contents returns 404 for a missing session."""
    resp = await client.get("/v1/sessions/conv_nonexistent/agent/contents")
    assert resp.status_code == 404
