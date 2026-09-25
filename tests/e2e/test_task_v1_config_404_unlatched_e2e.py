"""task_v1's selection-model config 404 must latch, not silently re-fail per turn.

On a Databricks-managed deployment whose ``routing:`` block leaves
``selection_model`` unset, the router's own extraction call 404s when the
gateway's frozen default names a model the workspace does not serve. The body
is::

    {"error_code":"ENDPOINT_NOT_FOUND",
     "message":"responses self-call returned status 404: ..."}

That failure is configuration, not an outage: every later call 404s
identically, so :func:`~omnigent.server.smart_routing.router_permanently_disabled`
must recognise it and the client must latch after one request — like the
account-level "not enabled" 404 — while a genuinely transient 404 stays
retriable.

The real Databricks AI Gateway is not reachable in CI, so a loopback
``routes:select`` service stands in for it, returning the gateway's verbatim
404 body. The client, orchestration seam, and predicate under test are the
real product code; only the gateway that produces the 404 is a stand-in.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.server.routing_backend import RoutingBackends
from omnigent.server.smart_routing import (
    ExternalRoutingClient,
    route_turn_or_decline,
    router_permanently_disabled,
)

# Verbatim gateway body: the extraction self-call 404s because the unset
# selection_model resolves to a frozen default the workspace does not serve.
# Contains neither "routes:select" nor "not enabled".
CONFIG_404_BODY: dict[str, str] = {
    "error_code": "ENDPOINT_NOT_FOUND",
    "message": (
        'responses self-call returned status 404: {"error_code":"ENDPOINT_NOT_FOUND",'
        '"message":"The given endpoint does not exist, please retry after checking '
        'the specified model and version deployment exists."}'
    ),
}

# The account-level 404 the predicate already recognises and latches.
NOT_ENABLED_BODY: dict[str, str] = {
    "error_code": "NOT_FOUND",
    "message": "routing/v1/routes:select is not enabled for this account.",
}

_MODEL_PREFIXES = ["databricks-", "system.ai."]
_CLAUDE_MENU = {"claude-native": ["claude-opus-4-8", "claude-sonnet-5"]}
_SELECTION_MODEL = "system.ai.claude-sonnet-5"


class _RecordingRouter:
    """Handle on the served routes:select requests."""

    def __init__(self) -> None:
        self.selector_models: list[str | None] = []

    @property
    def count(self) -> int:
        return len(self.selector_models)


def _handler(
    router: _RecordingRouter, respond: Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]
):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            selector = body.get("route_selector") or {}
            router.selector_models.append((selector.get("config") or {}).get("model"))
            status, payload = respond(body)
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return _Handler


@contextmanager
def _serve(
    respond: Callable[[dict[str, Any]], tuple[int, dict[str, Any]]],
) -> Iterator[tuple[str, _RecordingRouter]]:
    """Run a loopback routes:select service whose replies come from *respond*.

    :yields: ``(base_url, router)`` where ``base_url`` goes straight into
        ``ExternalRoutingClient(base_url=...)``.
    """
    router = _RecordingRouter()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(router, respond))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/ai-gateway/routing/v1", router
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _always_config_404(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 404, CONFIG_404_BODY


def test_router_permanently_disabled_recognises_the_config_failure_404() -> None:
    """The ENDPOINT_NOT_FOUND / self-call 404 is a permanent configuration
    failure, so it must be latched — just like the account-level 404 — while a
    genuinely transient 404 stays retriable."""
    assert router_permanently_disabled(404, json.dumps(CONFIG_404_BODY)) is True
    assert router_permanently_disabled(404, json.dumps(NOT_ENABLED_BODY)) is True
    assert router_permanently_disabled(404, json.dumps({"message": "no route"})) is False
    assert router_permanently_disabled(200, json.dumps(CONFIG_404_BODY)) is False


@pytest.mark.asyncio
async def test_config_404_is_latched_and_asked_exactly_once() -> None:
    """Two routing turns against a gateway that answers the config-failure 404:
    the client must latch after the first and never re-request."""
    with _serve(_always_config_404) as (base_url, router):
        client = ExternalRoutingClient(
            base_url=base_url, router_name="task_v1", model_prefixes=_MODEL_PREFIXES
        )
        assert await client.route("rename a local variable in one file", _CLAUDE_MENU) is None
        assert client.permanently_unavailable is True
        assert await client.route("add a --dry-run flag", _CLAUDE_MENU) is None

    assert router.count == 1
    assert client.last_error is not None
    assert "404" in client.last_error


@pytest.mark.asyncio
async def test_turn_seam_stops_re_requesting_after_config_404() -> None:
    """Driven through ``route_turn_or_decline`` (the per-turn orchestration
    entry point), a second turn must not re-issue routes:select once the first
    turn saw the config-failure 404."""
    with _serve(_always_config_404) as (base_url, router):
        client = ExternalRoutingClient(
            base_url=base_url, router_name="task_v1", model_prefixes=_MODEL_PREFIXES
        )

        class _Caps:
            routing_client = client
            routing_backends = RoutingBackends(external=client)

        with patch("omnigent.runtime._globals._caps", new=_Caps()):
            for message in ("rename a local variable in one file", "add a --dry-run flag"):
                model, verdict, error = await route_turn_or_decline(
                    "claude-native",
                    message,
                    session_id="conv_config_404",
                    catalog=["databricks-claude-opus-4-8", "databricks-claude-sonnet-5"],
                    gateway_backed=True,
                    allow_static_fallback=True,
                )
                # Fail-open: a routing outage never drops the turn or raises.
                assert model is None
                assert verdict is None
                assert error is None

    assert router.count == 1


@pytest.mark.asyncio
async def test_task_v1_requires_selection_model_when_gateway_default_unserved() -> None:
    """task_v1 is unusable while ``selection_model`` is unset: the request goes
    out with no ``config.model`` and the gateway 404s; pinning one flips it to a
    served route selection."""

    def respond(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        selector = body.get("route_selector") or {}
        if (selector.get("config") or {}).get("model"):
            return 200, {
                "route_selection": [
                    {
                        "route_option": {"model": "claude-sonnet-5", "harness": "claude"},
                        "params": {},
                    }
                ],
                "rationale": "Routed to claude-sonnet-5 because trivial task -> cheapest arm.",
            }
        return 404, CONFIG_404_BODY

    with _serve(respond) as (base_url, router):
        unset = ExternalRoutingClient(
            base_url=base_url, router_name="task_v1", model_prefixes=_MODEL_PREFIXES
        )
        assert await unset.route("hi", _CLAUDE_MENU) is None

        pinned = ExternalRoutingClient(
            base_url=base_url,
            router_name="task_v1",
            model_prefixes=_MODEL_PREFIXES,
            selection_model=_SELECTION_MODEL,
        )
        result = await pinned.route("hi", _CLAUDE_MENU)
        assert result is not None
        assert result.model == "claude-sonnet-5"

    assert router.selector_models == [None, _SELECTION_MODEL]
