"""Regression test for issue #1350: native bridge dirs leak on session delete.

Each native session's bridge dir holds a per-conversation bridge token + MCP
config (secret material). ``DELETE /v1/sessions/{id}`` closes the pane but
historically never removed this SEPARATE dir, so token-bearing
``/tmp/omnigent-*`` (and ``~/.omnigent``) dirs accumulated even on a clean
delete. The delete path must now ``rmtree`` it — for ALL 11 native families,
not just the original 5.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent.antigravity_native_bridge import (
    bridge_dir_for_bridge_id as antigravity_bridge_dir,
)
from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.claude_native_bridge import (
    bridge_dir_for_bridge_id as claude_bridge_dir,
)
from omnigent.codex_native_bridge import (
    bridge_dir_for_bridge_id as codex_bridge_dir,
)
from omnigent.cursor_native_bridge import (
    bridge_dir_for_session_id as cursor_bridge_dir,
)
from omnigent.goose_native_bridge import (
    bridge_dir_for_session_id as goose_bridge_dir,
)
from omnigent.hermes_native_bridge import (
    bridge_dir_for_session_id as hermes_bridge_dir,
)
from omnigent.kimi_native_bridge import (
    bridge_dir_for_session_id as kimi_bridge_dir,
)
from omnigent.kiro_native_bridge import (
    bridge_dir_for_session_id as kiro_bridge_dir,
)
from omnigent.opencode_native_bridge import (
    bridge_dir_for_bridge_id as opencode_bridge_dir,
)
from omnigent.pi_native_bridge import (
    bridge_dir_for_session_id as pi_bridge_dir,
)
from omnigent.qwen_native_bridge import (
    bridge_dir_for_session_id as qwen_bridge_dir,
)
from omnigent.runner import create_runner_app
from tests.runner.helpers import NullServerClient

# One resolver per native family — the session_id-keyed dir each harness leaves
# behind. _delete_native_bridge_dirs falls back to session_id for every family
# (label-rotated ids resolve to the same dir under the NullServerClient stub),
# so keying purely on session_id exercises the cleanup for all 11.
BRIDGE_DIR_RESOLVERS: dict[str, Callable[[str], Path]] = {
    "antigravity": antigravity_bridge_dir,
    "claude": claude_bridge_dir,
    "codex": codex_bridge_dir,
    "cursor": cursor_bridge_dir,
    "goose": goose_bridge_dir,
    "hermes": hermes_bridge_dir,
    "kimi": kimi_bridge_dir,
    "kiro": kiro_bridge_dir,
    "opencode": opencode_bridge_dir,
    "pi": pi_bridge_dir,
    "qwen": qwen_bridge_dir,
}


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


@pytest.mark.parametrize("family", sorted(BRIDGE_DIR_RESOLVERS))
async def test_cleanup_resources_removes_native_bridge_dir(
    client: httpx.AsyncClient,
    family: str,
) -> None:
    """The PRODUCTION delete path must remove every family's bridge dir.

    Server-side session delete drives ``DELETE /v1/sessions/{id}/resources``
    (``cleanup_session_resources``), NOT the bare ``DELETE /v1/sessions/{id}``
    route — so the token-bearing bridge dir must be removed there too, else the
    #1350 leak persists in real deletes. All 11 native families create such a
    dir, so all 11 must be cleaned up.
    """
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = BRIDGE_DIR_RESOLVERS[family](session_id)
    # Materialize the token-bearing dir the harness would have left behind.
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "bridge.json").write_text("{}")
    assert bridge_dir.exists()

    resp = await client.delete(f"/v1/sessions/{session_id}/resources")
    assert resp.status_code == 200

    assert not bridge_dir.exists(), (
        f"{family} bridge dir must be deleted on the real /resources path"
    )
