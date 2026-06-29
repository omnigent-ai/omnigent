"""Regression test for issue #1350: native bridge dirs leak on session delete.

Each native session's ``prepare_bridge_dir`` creates a per-conversation dir
holding a bridge token + MCP config (secret material). ``DELETE
/v1/sessions/{id}`` closes the pane but historically never removed this
SEPARATE dir, so token-bearing ``/tmp/omnigent-*`` dirs accumulated even on a
clean delete. The delete path must now ``rmtree`` it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.runner import create_runner_app
from tests.runner.helpers import NullServerClient


@pytest.fixture
def app() -> FastAPI:
    """Build a runner app with a stub server client."""
    return create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client bound to the runner app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


async def test_delete_session_removes_native_bridge_dir(
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    """``DELETE`` must remove the token-bearing claude-native bridge dir."""
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    assert bridge_dir == bridge_dir_for_bridge_id(session_id)
    # prepare_bridge_dir writes a bridge.json holding the bridge token.
    assert (bridge_dir / "bridge.json").exists()

    resp = await client.delete(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200

    assert not bridge_dir.exists(), "bridge dir (with token) must be deleted"


async def test_cleanup_resources_removes_native_bridge_dir(
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    """The PRODUCTION delete path must remove the token-bearing bridge dir.

    Server-side session delete drives ``DELETE /v1/sessions/{id}/resources``
    (``cleanup_session_resources``), NOT the bare ``DELETE /v1/sessions/{id}``
    route — so the bridge dir must be removed there too, else the #1350 leak
    persists in real deletes even though the bare-route test passes.
    """
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    assert (bridge_dir / "bridge.json").exists()

    resp = await client.delete(f"/v1/sessions/{session_id}/resources")
    assert resp.status_code == 200

    assert not bridge_dir.exists(), "bridge dir must be deleted on the real /resources path"
