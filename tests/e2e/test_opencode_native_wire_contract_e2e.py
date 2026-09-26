"""End-to-end test: the OpenCode-native client speaks to a REAL ``opencode serve`` 2.x.

The client (``omnigent.harnesses.opencode_native.client``) is hand-shaped from the
``@opencode/cli`` 2.0.x OpenAPI, so the rest of the suite exercises it only
against in-process fakes. This test boots a real ``opencode serve --stdio``
through :class:`~omnigent.harnesses.opencode_native.app_server.OpenCodeNativeServer`
and drives the provider-independent ``/api/*`` endpoints the harness relies on.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* Opt-in only: set ``OMNIGENT_E2E_OPENCODE_NATIVE=1`` and have ``opencode`` 2.0.x
  on ``PATH`` (``npm i -g @opencode/cli@~2.0.18``). No login or model
  credential is needed: server info, session create/get/list/context, the SSE
  stream, fork, interrupt, and the permission/form error paths are
  provider-independent.
* The heartbeat check waits up to 20 s for the server's 15 s ``: heartbeat``.
* Run it with::

    OMNIGENT_E2E_OPENCODE_NATIVE=1 .venv/bin/pytest \
        tests/e2e/test_opencode_native_wire_contract_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeNativeServer,
    OpenCodeVersionError,
)
from omnigent.harnesses.opencode_native.client import OpenCodeClientError, OpenCodeEvent

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_OPENCODE_NATIVE") != "1" or shutil.which("opencode") is None,
    reason=(
        "opencode-native wire-contract e2e needs `opencode` 2.0.x on PATH; "
        "set OMNIGENT_E2E_OPENCODE_NATIVE=1 (and `npm i -g @opencode/cli@~2.0.18`) to run"
    ),
)

# Session.Info keys OpenCodeSession.from_payload and the forwarder depend on.
_REQUIRED_SESSION_KEYS = {"id", "projectID", "location", "time"}


async def _first_event(server: OpenCodeNativeServer) -> OpenCodeEvent | None:
    client = server.client()
    try:
        async for event in client.stream_events():
            return event
        return None
    finally:
        await client.aclose()


async def _saw_heartbeat(server: OpenCodeNativeServer) -> bool:
    async with httpx.AsyncClient(
        base_url=server.base_url, headers=server.auth_headers, timeout=None
    ) as http:
        async with http.stream("GET", "/api/event") as response:
            async for line in response.aiter_lines():
                if line.startswith(": heartbeat"):
                    return True
    return False


async def test_opencode_native_wire_contract_against_real_server() -> None:
    """A real ``opencode serve --stdio`` answers every v2 endpoint the harness drives."""
    tmp = Path(tempfile.mkdtemp(prefix="opencode-e2e-"))
    bridge = tmp / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    workspace = tmp / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    server = OpenCodeNativeServer(bridge_dir=bridge, workspace=workspace)
    process = None
    try:
        try:
            await server.start()
        except OpenCodeVersionError as exc:
            pytest.skip(f"installed opencode is outside the supported pin: {exc}")
        process = server.process

        # Readiness recorded the server-reported version; OPENCODE_DB landed in the bridge.
        assert server.version is not None and server.version.startswith("2.")
        assert (bridge / "opencode.db").exists()

        client = server.client()
        try:
            info = await client.info()
            assert info["version"] == server.version

            session = await client.create_session(
                title="omnigent-e2e",
                directory=str(workspace),
                metadata={"omnigent_conversation": "conv_e2e"},
            )
            assert session.id.startswith("ses")
            assert set(session.raw) >= _REQUIRED_SESSION_KEYS, (
                f"session payload missing keys: {_REQUIRED_SESSION_KEYS - set(session.raw)}"
            )
            assert session.directory is not None
            assert Path(session.directory).resolve() == workspace.resolve()

            fetched = await client.get_session(session.id)
            assert fetched is not None and fetched.id == session.id
            assert await client.get_session("ses_does_not_exist_xyz") is None

            assert isinstance(await client.list_messages(session.id), list)
            assert isinstance(await client.get_context(session.id), list)

            # The first SSE frame is server.connected; heartbeats are comments.
            event = await asyncio.wait_for(_first_event(server), timeout=10.0)
            assert event is not None and event.type == "server.connected"
            assert await asyncio.wait_for(_saw_heartbeat(server), timeout=20.0)

            # A fresh session has no messages, and 2.0.18 refuses to fork one;
            # forking a real turn needs model credentials this e2e doesn't have.
            with pytest.raises(OpenCodeClientError) as fork_exc:
                await client.fork(session.id)
            assert fork_exc.value.status_code == 400
            assert await client.interrupt(session.id) is False

            with pytest.raises(OpenCodeClientError) as perm_exc:
                await client.reply_permission(session.id, "per_missing", "reject")
            assert perm_exc.value.status_code in (400, 404)
            with pytest.raises(OpenCodeClientError):
                await client.cancel_form(session.id, "frm_missing")
        finally:
            await client.aclose()
    finally:
        await server.close()
    # Closing stdin ends a --stdio server cleanly, without terminate().
    if process is not None:
        assert process.returncode == 0
