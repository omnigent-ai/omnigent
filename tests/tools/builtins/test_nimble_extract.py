"""Tests for the ``nimble_extract`` built-in tool (Nimble Extract Templates).

HTTP is mocked at the transport layer with ``respx`` so the single
``POST /v2/extract/templates/run`` call exercises the real URL, status codes,
and headers — including a captured-request assertion that every request
carries ``X-Client-Source: omnigent``.
"""

from __future__ import annotations

import json

# Any: fixtures mirror Nimble's JSON payloads — heterogeneous dicts with
# string keys and mixed value types.
from typing import Any

import httpx
import pytest
import respx

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins.nimble_extract import (
    NimbleExtractTool,
    _format_extract,
    _resolve_timeout,
)

_BASE = "https://sdk.nimbleway.com"
_RUN_URL = f"{_BASE}/v2/extract/templates/run"


_TEMPLATE = "acme_serp_snapshot"


def _config(**over: str) -> dict[str, str]:
    """Minimal valid spec config, with overrides."""
    config = {"api_key": "test-key", "template": _TEMPLATE}
    config.update(over)
    return config


def _entities() -> dict[str, Any]:
    """A ParsingSuccessResult-style entities payload."""
    return {
        "OrganicResult": [
            {"title": "Nimble", "url": "https://nimbleway.com", "position": 1},
            {"title": "Docs", "url": "https://docs.nimbleway.com", "position": 2},
        ],
        "RelatedSearch": [{"query": "nimble web data"}],
    }


def _payload(
    parsing: dict[str, Any] | None = None,
    status: str = "success",
    **data_over: Any,
) -> dict[str, Any]:
    """A ``/v2/extract/templates/run`` response body."""
    data: dict[str, Any] = {}
    if parsing is not None:
        data["parsing"] = parsing
    data.update(data_over)
    return {
        "task_id": "task_e2e0000-extract",
        "url": "https://www.google.com/search",
        "status": status,
        "data": data,
        "metadata": {"agent": "google_search"},
    }


def _invoke(config: dict[str, str], ctx: ToolContext, args: dict[str, Any] | None = None) -> str:
    """Invoke the tool with JSON-encoded arguments, like the runner does."""
    tool = NimbleExtractTool(config=config)
    payload = args if args is not None else {"params": {"query": "nimbleway"}}
    return tool.invoke(json.dumps(payload), ctx)


# ---------------------------------------------------------------------------
# Registry, schema, and sync-ness
# ---------------------------------------------------------------------------


def test_get_builtin_tool_returns_nimble_extract() -> None:
    """``get_builtin_tool("nimble_extract")`` returns a NimbleExtractTool."""
    tool = get_builtin_tool("nimble_extract")
    assert isinstance(tool, NimbleExtractTool), (
        f"Expected NimbleExtractTool, got {type(tool).__name__}."
    )


def test_retired_nimble_agent_name_is_not_registered() -> None:
    """The predecessor name is retired — no alias resolves, nothing reserves it."""
    from omnigent.tools.builtins import BUILTIN_NAMES

    assert get_builtin_tool("nimble_agent") is None
    assert "nimble_agent" not in BUILTIN_NAMES


def test_tool_name() -> None:
    """Tool name is 'nimble_extract'."""
    assert NimbleExtractTool.name() == "nimble_extract"


def test_schema_requires_params_object() -> None:
    """The function schema requires a ``params`` object; no ``query`` param."""
    schema = NimbleExtractTool().get_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "nimble_extract"
    parameters = schema["function"]["parameters"]
    assert parameters["required"] == ["params"]
    assert parameters["properties"]["params"]["type"] == "object"
    assert "query" not in parameters["properties"]


def test_is_sync() -> None:
    """nimble_extract runs synchronously (the runner bridges it off-loop)."""
    assert NimbleExtractTool().is_async() is False


# ---------------------------------------------------------------------------
# Config / argument validation (no HTTP may happen)
# ---------------------------------------------------------------------------


@respx.mock
def test_missing_api_key_returns_error(tool_ctx: ToolContext) -> None:
    """Without api_key the tool returns a clear error and makes no request."""
    out = _invoke({}, tool_ctx)
    assert out == "Error: api_key must be provided in the nimble_extract config in config.yaml."
    assert respx.calls.call_count == 0


@respx.mock
def test_missing_template_returns_error(tool_ctx: ToolContext) -> None:
    """Without a template the tool points at the template list, no HTTP call."""
    out = _invoke({"api_key": "test-key"}, tool_ctx)
    assert out.startswith("Error: template must be provided")
    assert "/v2/extract/templates" in out
    assert respx.calls.call_count == 0


@respx.mock
@pytest.mark.parametrize("raw", ["null", "[]", "42", '"scalar"'])
def test_non_object_arguments_return_error(tool_ctx: ToolContext, raw: str) -> None:
    """Valid-JSON non-object arguments return an error string, never raise."""
    out = NimbleExtractTool(config=_config()).invoke(raw, tool_ctx)
    assert out == "Error: arguments must be a JSON object"
    assert respx.calls.call_count == 0


@respx.mock
def test_missing_params_returns_error(tool_ctx: ToolContext) -> None:
    """Without a params object the tool returns a clear error, no HTTP call."""
    out = _invoke(_config(), tool_ctx, args={})
    assert out.startswith("Error: 'params' object is required")
    assert respx.calls.call_count == 0


@respx.mock
def test_non_object_params_returns_error(tool_ctx: ToolContext) -> None:
    """A string where the params object belongs is rejected before any request."""
    out = _invoke(_config(), tool_ctx, args={"params": "nimbleway"})
    assert out.startswith("Error: 'params' object is required")
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# Request shape (captured-request header + body assertions)
# ---------------------------------------------------------------------------


@respx.mock
def test_sends_bearer_client_source_template_and_params(tool_ctx: ToolContext) -> None:
    """api_key → Bearer, X-Client-Source set, body carries template + params."""
    route = respx.post(_RUN_URL).mock(
        return_value=httpx.Response(
            200, json=_payload({"status": "success", "entities": _entities()})
        )
    )
    _invoke(_config(), tool_ctx, args={"params": {"query": "nimbleway"}})

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["X-Client-Source"] == "omnigent", (
        f"Expected X-Client-Source 'omnigent', got {request.headers.get('X-Client-Source')!r}"
    )
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert body == {"template": _TEMPLATE, "params": {"query": "nimbleway"}}


@respx.mock
def test_config_template_selects_the_template(tool_ctx: ToolContext) -> None:
    """The spec-level ``template`` names the account template to run."""
    route = respx.post(_RUN_URL).mock(
        return_value=httpx.Response(200, json=_payload({"status": "success", "entities": {}}))
    )
    _invoke(_config(template="amazon_product"), tool_ctx, args={"params": {"asin": "B0TEST"}})
    body = json.loads(route.calls.last.request.content)
    assert body["template"] == "amazon_product"
    assert body["params"] == {"asin": "B0TEST"}


@respx.mock
def test_base_url_env_override(tool_ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_NIMBLE_EXTRACT_BASE_URL`` reroutes the request (test/e2e seam)."""
    monkeypatch.setenv("OMNIGENT_NIMBLE_EXTRACT_BASE_URL", "http://127.0.0.1:9998/")
    route = respx.post("http://127.0.0.1:9998/v2/extract/templates/run").mock(
        return_value=httpx.Response(
            200, json=_payload({"status": "success", "entities": _entities()})
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert route.called
    assert "OrganicResult" in out


# ---------------------------------------------------------------------------
# Output mapping
# ---------------------------------------------------------------------------


@respx.mock
def test_returns_structured_entities_json(tool_ctx: ToolContext) -> None:
    """The parsed ``entities`` are returned as structured, valid JSON."""
    entities = _entities()
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(
            200, json=_payload({"status": "success", "entities": entities})
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert json.loads(out) == entities, "Result must be valid JSON of the entities."
    assert "https://nimbleway.com" in out


@respx.mock
def test_parsing_error_result_is_clear(tool_ctx: ToolContext) -> None:
    """A ParsingErrorResult surfaces the template, error text, and task id."""
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(
            200, json=_payload({"status": "error", "error": "selector drift"})
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert out == (
        f"Nimble extract error: template {_TEMPLATE!r} parsing failed: "
        "selector drift (task: task_e2e0000-extract)"
    )


@respx.mock
def test_freeform_parsing_object_is_returned(tool_ctx: ToolContext) -> None:
    """A schema-less parsing object (no status envelope) passes through as JSON."""
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(200, json=_payload({"products": [{"name": "X"}]}))
    )
    out = _invoke(_config(), tool_ctx)
    assert json.loads(out) == {"products": [{"name": "X"}]}


@respx.mock
def test_markdown_fallback(tool_ctx: ToolContext) -> None:
    """With no parsed entities, ``markdown`` content is returned."""
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(200, json=_payload(None, markdown="# Page\n\nBody text."))
    )
    out = _invoke(_config(), tool_ctx)
    assert out.startswith("# Page")


@respx.mock
def test_no_data_returns_status_message_with_task_id(tool_ctx: ToolContext) -> None:
    """No parsing/markdown/html → a clear status message keeping the task id."""
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(200, json=_payload(None, status="blocked"))
    )
    out = _invoke(_config(), tool_ctx)
    assert out == (
        "No structured data returned by the Nimble extract run "
        "(status: blocked) (task: task_e2e0000-extract)."
    )


@respx.mock
def test_large_entities_truncated_with_marker(tool_ctx: ToolContext) -> None:
    """Oversized structured output is capped with the truncation marker."""
    big = {"OrganicResult": [{"snippet": "x" * 60_000}]}
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(200, json=_payload({"status": "success", "entities": big}))
    )
    out = _invoke(_config(), tool_ctx)
    assert out.endswith("[truncated]")
    assert len(out) <= 50_100


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


@respx.mock
def test_template_404_names_the_template(tool_ctx: ToolContext) -> None:
    """A 404 maps to a template-not-found error naming the template."""
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(404, json={"status": "failed", "msg": "Template not found"})
    )
    out = _invoke(_config(template="not_a_template"), tool_ctx)
    assert out == (
        "Nimble extract error: template 'not_a_template' was not found. "
        "List templates via GET /v2/extract/templates."
    )


@respx.mock
def test_auth_401_no_retry(tool_ctx: ToolContext) -> None:
    """A 401 fails fast with an actionable message; exactly one request."""
    route = respx.post(_RUN_URL).mock(return_value=httpx.Response(401))
    out = _invoke(_config(), tool_ctx)
    assert out == (
        "Nimble extract error: HTTP 401 (authentication failed - check the "
        "api_key in the nimble_extract config)."
    )
    assert route.call_count == 1


@respx.mock
def test_422_names_params_rejection(tool_ctx: ToolContext) -> None:
    """A 422 maps to a clear params-rejected message naming the template."""
    respx.post(_RUN_URL).mock(return_value=httpx.Response(422))
    out = _invoke(_config(), tool_ctx)
    assert "HTTP 422" in out
    assert _TEMPLATE in out


@respx.mock
def test_5xx_returns_error_string(tool_ctx: ToolContext) -> None:
    """A bodyless server error surfaces as a readable string, never an exception."""
    respx.post(_RUN_URL).mock(return_value=httpx.Response(503))
    assert _invoke(_config(), tool_ctx) == "Nimble extract error: HTTP 503"


@respx.mock
def test_5xx_surfaces_server_message_and_task_id(tool_ctx: ToolContext) -> None:
    """A 5xx body with a message/task_id is surfaced for supportability."""
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(
            500,
            json={
                "success": "false",
                "task_id": "task_500",
                "message": "can't download the query response - please try again",
            },
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert out == (
        "Nimble extract error: HTTP 500: can't download the query response - "
        "please try again (task: task_500)"
    )


@respx.mock
def test_oversized_error_body_is_capped(tool_ctx: ToolContext) -> None:
    """API-supplied error strings are capped so an oversized error body
    cannot bloat the tool result the way success payloads cannot."""
    respx.post(_RUN_URL).mock(
        return_value=httpx.Response(
            500,
            json={"message": "m" * 10_000, "task_id": "t" * 10_000},
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert len(out) < 2 * 2_100
    assert out.count("…") == 2


@respx.mock
def test_timeout_returns_error_string(tool_ctx: ToolContext) -> None:
    """A network timeout surfaces as a readable error string."""
    respx.post(_RUN_URL).mock(side_effect=httpx.TimeoutException("read timed out"))
    out = _invoke(_config(), tool_ctx)
    assert out.startswith("Nimble extract error: ")


@respx.mock
def test_non_json_response_is_clear(tool_ctx: ToolContext) -> None:
    """A non-JSON 200 body becomes a clear decode error."""
    respx.post(_RUN_URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    out = _invoke(_config(), tool_ctx)
    assert out == "Nimble extract error: the run returned a non-JSON response."


@respx.mock
def test_api_key_never_in_any_output(tool_ctx: ToolContext) -> None:
    """The api_key never leaks into any returned message, on any path."""
    secret = "sk-SECRET-VALUE"
    outputs = []
    respx.post(_RUN_URL).mock(return_value=httpx.Response(401))
    outputs.append(_invoke(_config(api_key=secret), tool_ctx))
    respx.post(_RUN_URL).mock(side_effect=httpx.ConnectError("refused"))
    outputs.append(_invoke(_config(api_key=secret), tool_ctx))
    for out in outputs:
        assert secret not in out, f"api_key leaked into: {out!r}"


# ---------------------------------------------------------------------------
# Helper units
# ---------------------------------------------------------------------------


def test_resolve_timeout_clamps_and_defaults() -> None:
    """Timeout coercion: junk → default, out-of-range → clamped."""
    assert _resolve_timeout({}) == 120.0
    assert _resolve_timeout({"timeout_seconds": "abc"}) == 120.0
    assert _resolve_timeout({"timeout_seconds": "5"}) == 10.0
    assert _resolve_timeout({"timeout_seconds": "9999"}) == 600.0
    assert _resolve_timeout({"timeout_seconds": "45"}) == 45.0
    assert _resolve_timeout({"timeout_seconds": "nan"}) == 120.0


def test_empty_entities_on_success_returns_empty_json(tool_ctx: ToolContext) -> None:
    """A successful parse with zero entities returns {} — not a status message."""
    with respx.mock:
        respx.post(_RUN_URL).mock(
            return_value=httpx.Response(200, json=_payload({"status": "success", "entities": {}}))
        )
        out = _invoke(_config(), tool_ctx)
    assert out == "{}"


def test_format_extract_prefers_entities_over_markdown() -> None:
    """When both entities and markdown exist, entities win."""
    payload = {
        "task_id": "t1",
        "status": "success",
        "data": {
            "parsing": {"status": "success", "entities": {"A": 1}},
            "markdown": "# fallback",
        },
    }
    assert json.loads(_format_extract(payload, "google_search")) == {"A": 1}
