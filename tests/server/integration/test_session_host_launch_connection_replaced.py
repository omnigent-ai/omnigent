"""Regression test: inline session-create survives a host connection
replaced between workspace validation and launch-frame enqueue.

Scenario reproduced here: ``POST /v1/sessions`` with ``host_id`` +
``workspace`` first validates the workspace over the host tunnel
(``host.stat``), then atomically binds a runner and enqueues a
``host.launch_runner`` frame. If the host reconnects in that window,
:meth:`HostRegistry.send_text` correctly rejects the now-obsolete
connection with a ``ConnectionError``.

If that ``ConnectionError`` escaped the launch-result error handling
instead of following the deliberately lenient failed-launch contract used
everywhere else in this route (see
``test_inline_launch_failure_still_returns_bound_session`` in
``test_session_host_launch.py``), the create would return HTTP 500 even
though the conversation was already created and its runner already bound,
and the pending launch future would be left behind on the connection.

Required behavior: treat the connection loss like the host's own failed
launch verdict -- return 201 with the runner-bound, recoverable
conversation so the client can reconnect/relaunch it -- and remove the
pending launch entry on every completion path.

Deterministic injection: wrap the app's ``HostRegistry.send_text`` so the
moment it is asked to send the launch frame it registers a replacement
connection for the same host (newest-wins, poisoning the original), then
delegates to the *real* ``send_text``. The genuine, unmodified
replaced-connection guard then fires -- preserving the real registry
guard, route, and database, and reproducing the exact host-reconnect race.

The create-session POST goes through a client with
``raise_app_exceptions=False`` so a regression surfaces the way a browser
client sees it: ``ServerErrorMiddleware`` sends the 500 response and then
re-raises for the ASGI server to log, so a network client receives the 500
while an in-process ``raise_app_exceptions=True`` client would instead see
the re-raised ``ConnectionError``.

This reuses the host-launch harness (``app`` override, ``_connect_host``,
``_HOST_ID``, ``_WORKSPACE``) from ``test_session_host_launch.py`` rather
than standing up a second one.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from fastapi import FastAPI

from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostStatFrame,
    HostStatResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent

# Reuse the host-launch harness verbatim: the ``app`` fixture wired WITH a
# host_store (so the inline launch branch is active), the WebSocket host
# connect helper, and the shared host id / workspace constants.
from tests.server.integration.test_session_host_launch import (  # noqa: F401
    _HOST_ID,
    _WORKSPACE,
    _connect_host,
    app,
)

pytestmark = pytest.mark.asyncio


class _DeadHostWS:
    """Minimal WebSocket stand-in for the replacement host connection.

    The replacement only needs to exist in the registry so the real send
    guard sees a *different* current connection for the original one; its
    frames go nowhere and no one reads from it.
    """

    async def send_text(self, data: str) -> None:
        """Accept and drop outbound frames."""
        del data

    async def receive_text(self) -> str:
        """Block forever; nothing drives inbound frames on the stand-in."""
        return await asyncio.Future()


async def _serve_stat_only(comm) -> None:
    """Answer the host's ``host.stat`` round-trip and return.

    Unlike the shared ``_serve_one_launch`` this stops after the stat: in
    this scenario the launch frame is never delivered to the host, because
    the connection is replaced at enqueue time and ``send_text`` raises
    before the frame reaches the wire.

    :param comm: The connected host communicator.
    """
    # Bounded so a routing bug can't hang the test; stat is 1 frame, the
    # rest of the budget absorbs interleaved pings.
    for _ in range(40):
        output = await comm.receive_output(timeout=3.0)
        if output["type"] != "websocket.send":
            continue
        frame = decode_host_frame(output["text"])
        if isinstance(frame, HostStatFrame):
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostStatResultFrame(
                            request_id=frame.request_id,
                            status="ok",
                            exists=True,
                            type="directory",
                            canonical_path=frame.path,
                        )
                    ),
                }
            )
            return
    raise AssertionError("host never received a stat frame from the inline path")


async def test_inline_launch_connection_replaced_at_enqueue_returns_bound_session(
    client: httpx.AsyncClient,
    app: FastAPI,  # noqa: F811  (imported fixture)
    db_uri: str,
) -> None:
    """A host reconnect between workspace validation and launch enqueue
    must not turn ``POST /v1/sessions`` into an HTTP 500.

    The create has already persisted the conversation and atomically bound
    its runner by the time the launch frame is enqueued, so a rejected
    (replaced) connection must be handled like the host's own failed
    launch: return 201 with the runner-bound, recoverable session, and
    leave no pending launch future behind. A regression would let the
    ``ConnectionError`` from the registry guard escape and turn the
    create into a 500.
    """
    comm = await _connect_host(app)
    agent = await create_test_agent(client)

    registry = app.state.host_registry
    # The live connection the create will resolve and try to launch on.
    original_conn: HostConnection | None = registry.get(_HOST_ID)
    assert original_conn is not None

    real_send_text = registry.send_text
    replaced = {"done": False}

    def _send_text_replacing_on_launch(conn: HostConnection, data: str) -> None:
        """Replace the host connection the instant the launch is enqueued.

        The stat frame passes straight through. On the first launch frame
        we reconnect the same host (newest-wins) so the registry entry for
        this ``conn`` now points at a different connection, then delegate
        to the genuine ``send_text`` -- whose real replaced-connection
        guard raises ``ConnectionError`` exactly as a live reconnect would.
        """
        frame = decode_host_frame(data)
        if isinstance(frame, HostLaunchRunnerFrame) and not replaced["done"]:
            replaced["done"] = True
            registry.register(
                _HOST_ID,
                _DeadHostWS(),
                HostHelloFrame(
                    version="0.1.0-test",
                    frame_protocol_version=1,
                    name="laptop-reconnect",
                ),
                owner=conn.owner,
                workspace_id=conn.workspace_id,
            )
        real_send_text(conn, data)

    registry.send_text = _send_text_replacing_on_launch  # type: ignore[method-assign]
    stat_responder = asyncio.create_task(_serve_stat_only(comm))
    # A network client receives the 500 that ServerErrorMiddleware sends
    # before it re-raises for the ASGI server's logs; raise_app_exceptions
    # here mirrors that (the shared fixture client would instead surface the
    # re-raised ConnectionError). The autouse conftest fixture stamps the
    # first-party Origin on this transport too, so the CSRF guard is happy.
    create_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )
    try:
        resp = await create_client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "host_id": _HOST_ID,
                "workspace": _WORKSPACE,
            },
        )
    finally:
        await create_client.aclose()
        registry.send_text = real_send_text  # type: ignore[method-assign]
        stat_responder.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stat_responder

    # Sanity: the injection actually fired on the launch enqueue.
    assert replaced["done"], "launch frame was never enqueued; injection did not fire"

    # The connection loss must be treated like the host's own failed
    # launch: 201 with the runner-bound, recoverable session -- NOT a 500
    # that strands an already-persisted, already-bound conversation.
    assert resp.status_code == 201, (
        f"expected lenient 201 on a replaced-connection launch, got "
        f"{resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["host_id"] == _HOST_ID
    assert body["runner_id"] is not None
    # runner_id is derived from the server's binding token, so it carries
    # the token prefix -- proving the atomic bind persisted before launch.
    assert body["runner_id"].startswith("runner_token_"), (
        f"runner_id should be derived from the binding token, got {body['runner_id']!r}"
    )

    # The persisted row must carry the same binding so the client can
    # reconnect/relaunch the existing conversation.
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(body["id"])
    assert conv is not None
    assert conv.runner_id == body["runner_id"]
    assert conv.host_id == _HOST_ID
    assert conv.workspace == _WORKSPACE

    # No pending launch future may be left behind on the connection whose
    # send was rejected -- a leftover entry would strand a future no launch
    # result can ever resolve.
    assert not original_conn.pending_launches, (
        "pending launch future left behind on the replaced connection"
    )
