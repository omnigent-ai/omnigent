"""The message path against a gateway whose ``routes:select`` answers the
selection-model configuration 404.

A workspace whose ``routing:`` block pins no ``selection_model`` leaves the
router's own extraction model to the gateway's frozen default. When that
default names a model the workspace does not serve, every ``task_v1``
``routes:select`` call fails with ``404 ENDPOINT_NOT_FOUND`` whose body relays
``responses self-call returned status 404``. That condition is configuration,
not an outage: like the account-level "not enabled" 404, the client must latch
it — stop re-issuing the doomed call every turn and stop advertising the
external router — instead of silently re-failing for the life of the process.

These tests drive the real user journey (create a Smart-Routing session, send
messages over ``POST /v1/sessions/{id}/events``) with the real
:class:`~omnigent.server.smart_routing.ExternalRoutingClient` wired at a
loopback service returning that 404 body verbatim.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from omnigent.runtime import _globals as runtime_globals
from omnigent.server.routing_backend import RoutingBackends
from omnigent.server.smart_routing import ExternalRoutingClient
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

# The gateway's answer when the (defaulted) selection model is not served,
# verbatim from a Databricks workspace whose routing API is enabled.
_GATEWAY_404_BODY = json.dumps(
    {
        "error_code": "ENDPOINT_NOT_FOUND",
        "message": (
            'responses self-call returned status 404: {"error_code":"ENDPOINT_NOT_FOUND",'
            '"message":"The given endpoint does not exist, please retry after checking '
            'the specified model and version deployment exists."}'
        ),
    }
)


@dataclass
class _Gateway:
    """Handle on the loopback stand-in for the AI-Gateway routing service."""

    base_url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, body: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(body)

    def count(self) -> int:
        with self._lock:
            return len(self.requests)


@pytest.fixture
def gateway_404() -> Iterator[_Gateway]:
    """Serve every ``routes:select`` with the selection-model config 404.

    :yields: The gateway handle; ``base_url`` is what the deployment's
        ``routing.base_url`` would carry.
    """
    handle = _Gateway(base_url="")

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            """Silence the stdlib access log."""

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            handle.record(body if isinstance(body, dict) else {})
            raw = _GATEWAY_404_BODY.encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    handle.base_url = f"http://127.0.0.1:{server.server_port}/ai-gateway/routing/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handle
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def external_router(
    gateway_404: _Gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> ExternalRoutingClient:
    """Install a real external client configured with no ``selection_model``.

    Mirrors how the server CLI builds the client from a ``routing:`` block
    that sets ``base_url`` but no ``selection_model``.

    :returns: The client, for latch/last_error inspection.
    """
    router = ExternalRoutingClient(
        base_url=gateway_404.base_url,
        router_name="task_v1",
        selection_model=None,
    )
    monkeypatch.setattr(
        runtime_globals._caps,
        "routing_backends",
        RoutingBackends(external=router),
        raising=False,
    )
    monkeypatch.setattr(runtime_globals._caps, "routing_client", router, raising=False)
    return router


class _NoCatalogRunner:
    """Runner stub: acks every forwarded turn, serves no model catalog."""

    def __init__(self, forwarded: list[dict[str, Any]]) -> None:
        self._forwarded = forwarded

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        self._forwarded.append(json)

        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()

    async def get(self, *_: Any, **__: Any) -> Any:
        raise httpx.ConnectError("no live runner in this test")


def _stub_runner(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch ``_get_runner_client`` to a stub; return the forwarded bodies."""
    from omnigent.server.routes import sessions as sessions_mod

    forwarded: list[dict[str, Any]] = []

    async def _stub(*_: Any, **__: Any) -> _NoCatalogRunner:
        return _NoCatalogRunner(forwarded)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)
    return forwarded


async def _routing_session(client: httpx.AsyncClient) -> str:
    """Create a session with Smart Routing on for a gateway-backed harness.

    :returns: The session id.
    """
    agent = await create_test_agent(
        client,
        name="routing-selection-404",
        executor={
            "type": "omnigent",
            "config": {"harness": "claude-sdk"},
            "model": "databricks-claude-sonnet-4-6",
        },
        include_llm=False,
    )
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _send_message(client: httpx.AsyncClient, session_id: str, text: str) -> None:
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    assert resp.status_code == 202, resp.text


def _observed_state(
    db_uri: str,
    session_id: str,
    router: ExternalRoutingClient,
    gateway: _Gateway,
) -> str:
    """One-line snapshot of the routing outcome, for failure output."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(session_id)
    cards = [
        item
        for item in conv_store.list_items(session_id).data
        if getattr(item, "type", None) == "routing_decision"
    ]
    return (
        f"routes:select calls={gateway.count()} "
        f"permanently_unavailable={router.permanently_unavailable} "
        f"last_error={router.last_error!r} "
        f"model_override={getattr(conv, 'model_override', None)!r} "
        f"routing_decision_items={len(cards)}"
    )


async def test_selection_model_404_latches_instead_of_refailing_every_turn(
    client: httpx.AsyncClient,
    db_uri: str,
    gateway_404: _Gateway,
    external_router: ExternalRoutingClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One selection-model 404 must latch: turn 2 must not re-issue the call.

    The 404 body is configuration (the extraction model is not served), so
    every later call fails identically. Re-issuing it wastes a round trip per
    turn for the life of the process.
    """
    forwarded = _stub_runner(monkeypatch)
    session_id = await _routing_session(client)

    await _send_message(client, session_id, "rename a local variable in one file")

    assert gateway_404.count() == 1, "the first turn must reach the router"
    selector = gateway_404.requests[0].get("route_selector") or {}
    assert selector.get("router_name") == "task_v1"
    assert "config" not in selector, (
        "with routing.selection_model unset the request must pin no extraction "
        f"model (the gateway default governs); sent {selector!r}"
    )
    assert forwarded, "the turn must still be forwarded to the runner (fail-open)"
    assert "model_override" not in forwarded[0], (
        "a failed routing call must not route the turn; runner body carried "
        f"{forwarded[0].get('model_override')!r}"
    )
    assert external_router.last_error is not None, "the 404 must be recorded on the client"

    print("after turn 1:", _observed_state(db_uri, session_id, external_router, gateway_404))

    await _send_message(client, session_id, "now rename the other local variable")

    print("after turn 2:", _observed_state(db_uri, session_id, external_router, gateway_404))

    assert gateway_404.count() == 1, (
        "the selection-model 404 is a permanent configuration failure, not an "
        "outage: after the first failure no further routes:select calls may go "
        f"out, but the gateway served {gateway_404.count()} calls"
    )
    assert external_router.permanently_unavailable, (
        "the client must latch the selection-model 404 the way it latches the "
        "account-level 'not enabled' 404"
    )


async def test_selection_model_404_stops_advertising_external_router(
    client: httpx.AsyncClient,
    db_uri: str,
    gateway_404: _Gateway,
    external_router: ExternalRoutingClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /v1/info`` must stop reporting the external source once it latches.

    The deployment reporting ``external: true`` while every ``routes:select``
    404s is what makes the misconfiguration undiagnosable for an operator.
    """
    _stub_runner(monkeypatch)
    session_id = await _routing_session(client)

    await _send_message(client, session_id, "rename a local variable in one file")
    assert gateway_404.count() == 1, "the first turn must reach the router"

    print("after turn 1:", _observed_state(db_uri, session_id, external_router, gateway_404))

    info = await client.get("/v1/info")
    assert info.status_code == 200, info.text
    sources = info.json().get("smart_routing_sources") or {}
    assert sources.get("external") is False, (
        "after the router's permanent configuration failure the deployment "
        f"must stop advertising it; /v1/info reported {sources!r}"
    )
