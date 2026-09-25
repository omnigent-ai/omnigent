"""task_v3 routing over catalogs keyed by native harness ids.

The Databricks AI Gateway ``task_v3`` router validates
``route_options[].harness`` against its canonical harness names (``codex`` /
``claude``) and rejects a request offering anything else with a 400, so a
client that sends Omnigent harness ids (``codex-native`` / ``claude-native``)
never routes. :class:`~omnigent.server.smart_routing.ExternalRoutingClient`
must translate the ids into that vocabulary.

The real :meth:`~omnigent.server.smart_routing.ExternalRoutingClient.route`
call is driven over a real socket against an in-process ``routes:select``
service that mirrors the documented ``task_v3`` harness validation and the
``task_v1`` passthrough (harness echoed, never read). The live Databricks AI
Gateway is a Databricks-network service this CI cannot reach, so that service
stands in for it; the harness ids Omnigent puts on the wire are real product
output.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

# The two canonical harness names the gateway's task_v3 router accepts.
# Anything else is rejected.
_TASK_V3_HARNESSES = frozenset({"codex", "claude"})

# The 400 the real gateway returns, verbatim.
_TASK_V3_REJECT_MESSAGE = (
    "task_v3 customer policy 'all_common' has no eligible model+harness option"
)

_PROMPT = "rename a variable in one file"
_CODEX_CATALOG = {"codex-native": ["system.ai.glm-5-3", "system.ai.kimi-k3"]}


def _handler_class(recorded: list[dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            pass

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode())
            recorded.append(body)
            options = body.get("route_options") or []
            router_name = (body.get("route_selector") or {}).get("router_name")
            harnesses = {str(o.get("harness") or "") for o in options}
            # task_v3 validates the harness tag; task_v1 (and older) treat it as
            # passthrough and never read it.
            if router_name == "task_v3" and not harnesses <= _TASK_V3_HARNESSES:
                self._reply(
                    400,
                    {"error_code": "BAD_REQUEST", "message": _TASK_V3_REJECT_MESSAGE},
                )
                return
            first = options[0]
            self._reply(
                200,
                {
                    "route_selection": [
                        {"route_option": {"model": first["model"], "harness": first["harness"]}}
                    ],
                    "rationale": f"{router_name} selected an eligible option.",
                },
            )

    return _Handler


@pytest.fixture
def task_v3_router() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """A loopback ``routes:select`` service mirroring the gateway's task_v3 rules.

    :yields: ``(base_url, recorded_request_bodies)``.
    """
    recorded: list[dict[str, Any]] = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(recorded))
    httpd.daemon_threads = True
    host, port = httpd.server_address
    base_url = f"http://{host}:{port}/ai-gateway/routing/v1"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield base_url, recorded
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


async def test_task_v3_routes_a_native_harness_catalog(
    task_v3_router: tuple[str, list[dict[str, Any]]],
) -> None:
    """A codex-native catalog goes out as harness ``codex`` and routes."""
    from omnigent.server.smart_routing import ExternalRoutingClient

    base_url, recorded = task_v3_router
    client = ExternalRoutingClient(base_url=base_url, router_name="task_v3")

    result = await client.route(_PROMPT, _CODEX_CATALOG)

    sent_harnesses = {o["harness"] for o in recorded[-1]["route_options"]}
    assert sent_harnesses == {"codex"}, (
        f"route_options carried {sent_harnesses}; task_v3 only accepts {set(_TASK_V3_HARNESSES)}"
    )
    assert result is not None, f"task_v3 route() returned None: {client.last_error}"
    assert result.raw_model in {"glm-5-3", "kimi-k3"}


async def test_task_v3_accepts_canonical_harness(
    task_v3_router: tuple[str, list[dict[str, Any]]],
) -> None:
    """A catalog already keyed by the canonical name routes unchanged."""
    from omnigent.server.smart_routing import ExternalRoutingClient

    base_url, _ = task_v3_router
    client = ExternalRoutingClient(base_url=base_url, router_name="task_v3")

    result = await client.route(_PROMPT, {"codex": ["system.ai.glm-5-3", "system.ai.kimi-k3"]})

    assert result is not None, client.last_error
    assert result.model == "system.ai.glm-5-3"


async def test_task_v1_routes_the_same_native_catalog(
    task_v3_router: tuple[str, list[dict[str, Any]]],
) -> None:
    """task_v1 never reads the tag, so the canonical name routes there too."""
    from omnigent.server.smart_routing import ExternalRoutingClient

    base_url, recorded = task_v3_router
    client = ExternalRoutingClient(base_url=base_url, router_name="task_v1")

    result = await client.route(_PROMPT, _CODEX_CATALOG)

    sent_harnesses = {o["harness"] for o in recorded[-1]["route_options"]}
    assert sent_harnesses == {"codex"}
    assert result is not None, client.last_error
