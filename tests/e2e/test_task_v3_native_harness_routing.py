"""Harness-name round trips through the production client and a loopback router.

The stand-in requires canonical harness names for task_v3 and echoes task_v1
tags unchanged. These tests cover HTTP serialization and local selection
resolution; they do not start a harness or call the live Databricks gateway.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from omnigent.server.smart_routing import ExternalRoutingClient

# The two canonical harness names the gateway's task_v3 router accepts.
# Anything else is rejected.
_TASK_V3_HARNESSES = frozenset({"codex", "claude"})

# The 400 the real gateway returns, verbatim.
_TASK_V3_REJECT_MESSAGE = (
    "task_v3 customer policy 'all_common' has no eligible model+harness option"
)

_PROMPT = "rename a variable in one file"


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
            if not options:
                self._reply(
                    400, {"error_code": "BAD_REQUEST", "message": "route_options is empty"}
                )
                return
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


@pytest.mark.parametrize("router_name", ["task_v3", "task_v1"])
@pytest.mark.parametrize(
    ("harness", "model", "wire_harness"),
    [
        ("codex-native", "glm-5-3", "codex"),
        ("codex", "kimi-k3", "codex"),
        ("claude-native", "claude-sonnet-5", "claude"),
        ("claude-sdk", "claude-opus-4-8", "claude"),
        ("claude_sdk", "claude-sonnet-5", "claude"),
        ("openai-agents", "gpt-5-4", "codex"),
        ("openai-agents-sdk", "gpt-5-4", "codex"),
        ("agents_sdk", "gpt-5-4", "codex"),
    ],
)
async def test_routes_back_to_the_offered_harness(
    task_v3_router: tuple[str, list[dict[str, Any]]],
    router_name: str,
    harness: str,
    model: str,
    wire_harness: str,
) -> None:
    """Canonical wire tags resolve back to native, SDK, and aliased harness ids."""
    base_url, recorded = task_v3_router
    client = ExternalRoutingClient(base_url=base_url, router_name=router_name)
    catalog_model = f"system.ai.{model}"

    result = await client.route(_PROMPT, {harness: [catalog_model]})

    assert result is not None, client.last_error
    sent_harnesses = {o["harness"] for o in recorded[-1]["route_options"]}
    assert sent_harnesses == {wire_harness}
    assert (result.harness, result.model, result.raw_model) == (harness, catalog_model, model)


@pytest.mark.parametrize("router_name", ["task_v3", "task_v1"])
@pytest.mark.parametrize("first_harness", ["claude-native", "codex-native"])
async def test_mixed_native_catalog_resolves_both_families(
    task_v3_router: tuple[str, list[dict[str, Any]]],
    router_name: str,
    first_harness: str,
) -> None:
    """Both canonical tags may occur in one request without leaking into the result."""
    base_url, recorded = task_v3_router
    client = ExternalRoutingClient(base_url=base_url, router_name=router_name)
    models = {"claude-native": "claude-sonnet-5", "codex-native": "kimi-k3"}
    # The stand-in picks the first option; exercise a selection from each family.
    harnesses = [first_harness, *(h for h in models if h != first_harness)]
    catalog = {h: [f"system.ai.{models[h]}"] for h in harnesses}

    result = await client.route(_PROMPT, catalog)

    assert result is not None, client.last_error
    wire_options = {o["model"]: o["harness"] for o in recorded[-1]["route_options"]}
    assert wire_options["claude-sonnet-5"] == "claude"
    assert wire_options["kimi-k3"] == "codex"
    assert result.harness == first_harness
    assert result.model == catalog[first_harness][0]
    assert result.raw_model == models[first_harness]


@pytest.mark.parametrize("router_name", ["task_v3", "task_v1"])
@pytest.mark.parametrize("first_model", ["claude-sonnet-5", "gpt-5-4", "glm-5-3", "kimi-k3"])
async def test_pi_mixed_catalog_keeps_pi_after_canonicalization(
    task_v3_router: tuple[str, list[dict[str, Any]]],
    router_name: str,
    first_model: str,
) -> None:
    """Pi's wire tag follows each model's family while its local harness stays pi."""
    base_url, recorded = task_v3_router
    client = ExternalRoutingClient(base_url=base_url, router_name=router_name)
    expected_tags = {
        "claude-sonnet-5": "claude",
        "gpt-5-4": "codex",
        "glm-5-3": "codex",
        "kimi-k3": "codex",
    }
    models = [first_model, *(m for m in expected_tags if m != first_model)]
    catalog = {"pi": [f"system.ai.{m}" for m in models]}

    result = await client.route(_PROMPT, catalog)

    assert result is not None, client.last_error
    wire_options = {o["model"]: o["harness"] for o in recorded[-1]["route_options"]}
    assert {model: wire_options[model] for model in expected_tags} == expected_tags
    assert result.harness == "pi"
    assert result.model == f"system.ai.{first_model}"
    assert result.raw_model == first_model
