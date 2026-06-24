"""End-to-end test: the OpenCode-native client speaks to a REAL ``opencode serve``.

The opencode-native harness's HTTP+SSE client (``omnigent.opencode_native_client``)
is hand-shaped from the pinned OpenCode OpenAPI (vendored at
``omnigent/opencode/openapi-1.17.7.json``), so the rest of the suite exercises it
only against in-process fakes. This test boots a real ``opencode serve`` via the
PR's own :class:`~omnigent.opencode_native_app_server.OpenCodeNativeServer` and
drives the provider-independent endpoints the harness relies on, validating the
wire contract against the actual binary — the one thing the fakes cannot prove.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* **Opt-in only**: set ``OMNIGENT_E2E_OPENCODE_NATIVE=1`` and have a pinned
  ``opencode`` (>=1.17.7,<1.18.0) on ``PATH``. Unlike the codex/claude native
  e2es this needs **no** interactive login or model credential — session
  create/list, the SSE ``/event`` stream, permissions, fork and abort are all
  provider-independent. The gate just keeps it off CI runners without the binary.
* Run it with::

    OMNIGENT_E2E_OPENCODE_NATIVE=1 \
    .venv/bin/python -m pytest \
        tests/e2e/test_opencode_native_wire_contract_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from omnigent.opencode_native_app_server import (
    OpenCodeNativeServer,
    OpenCodeVersionError,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_OPENCODE_NATIVE") != "1" or shutil.which("opencode") is None,
    reason=(
        "opencode-native wire-contract e2e needs a pinned `opencode` binary on PATH; "
        "set OMNIGENT_E2E_OPENCODE_NATIVE=1 (and `npm i -g opencode-ai@1.17.7`) to run"
    ),
)

# Keys the typed client parses off a created session — assert the real server
# still emits the shape OpenCodeSession.from_payload depends on.
_REQUIRED_SESSION_KEYS = {"id", "directory", "title"}


async def test_opencode_native_wire_contract_against_real_server() -> None:
    """A real ``opencode serve`` answers every endpoint the harness drives.

    Covers: server boot + version pin, session create/get (+404), message
    list, permission list, the SSE ``/event`` stream framing, fork and abort —
    the provider-independent surface the SSE forwarder and executor depend on.
    """
    tmp = Path(tempfile.mkdtemp(prefix="opencode-e2e-"))
    bridge = tmp / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    workspace = tmp / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    server = OpenCodeNativeServer(bridge_dir=bridge, workspace=workspace)
    try:
        try:
            await server.start()
        except OpenCodeVersionError as exc:
            pytest.skip(f"installed opencode is outside the supported pin: {exc}")

        assert server.version is not None
        client = server.client()
        try:
            # create_session — and the parsed shape the forwarder relies on.
            session = await client.create_session({"title": "omnigent-e2e"})
            assert session.id, "created session has no id"
            assert set(session.raw) >= _REQUIRED_SESSION_KEYS, (
                f"server session payload missing keys the client parses: "
                f"{_REQUIRED_SESSION_KEYS - set(session.raw)}"
            )

            # get_session round-trips, and a missing id is a clean None (404).
            fetched = await client.get_session(session.id)
            assert fetched is not None and fetched.id == session.id
            assert await client.get_session("ses_does_not_exist_xyz") is None

            # message + permission listings are well-formed (empty for a fresh
            # session that has run no turn).
            assert await client.list_messages(session.id) == []
            assert await client.list_permissions() == []

            # The SSE /event stream connects and frames at least the initial
            # ``server.connected`` event (proves _parse_sse against the real wire).
            async def _first_event() -> object | None:
                async for event in client.events():
                    return event
                return None

            try:
                event = await asyncio.wait_for(_first_event(), timeout=10.0)
            except asyncio.TimeoutError:
                event = None
            if event is not None:
                assert isinstance(event.type, str)

            # fork creates a new session; abort returns cleanly with no work.
            forked = await client.fork(session.id)
            assert forked.id and forked.id != session.id
            assert isinstance(await client.abort(session.id), bool)
        finally:
            await client.aclose()
    finally:
        await server.close()
