"""Tests for the ``nimble_research`` built-in tool (Nimble Agent API v2 backend).

HTTP is mocked at the transport layer with ``respx`` so the start → poll →
result lifecycle exercises real URLs, status codes, and headers. Time is
controlled by monkeypatching the module's ``_monotonic``/``_sleep`` seams —
never the process-global ``time`` module.
"""

from __future__ import annotations

import json

# Any: fixtures mirror Nimble's JSON payloads — heterogeneous dicts with
# string keys and mixed value types.
from typing import Any

import httpx
import pytest
import respx

import omnigent.tools.builtins.nimble_research as nimble_research_mod
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins.nimble_research import (
    NimbleResearchTool,
    _clamp_seconds,
    _map_trust,
    _resolve_effort,
)

_BASE = "https://sdk.nimbleway.com"
_AGENT_ID = "wsa_0a1b2c3d-0000-4000-8000-000000000000"
_RUN_ID = "task_run_11111111-2222-4333-8444-555555555555"
_RUNS_URL = f"{_BASE}/v2/agents/{_AGENT_ID}/runs"
_RUN_URL = f"{_RUNS_URL}/{_RUN_ID}"
_RESULT_URL = f"{_RUN_URL}/result"


def _config(**over: str) -> dict[str, str]:
    """Minimal valid spec config, with overrides."""
    config = {"api_key": "test-key", "agent_id": _AGENT_ID}
    config.update(over)
    return config


def _run(status: str, **over: Any) -> dict[str, Any]:
    """A TaskRun response body in the given status."""
    run: dict[str, Any] = {
        "id": _RUN_ID,
        "interaction_id": "int_0001",
        "status": status,
        "is_active": status in ("queued", "running"),
        "effort": "medium",
        "created_at": "2026-07-22T00:00:00Z",
        "web_search_agent_id": _AGENT_ID,
    }
    run.update(over)
    return run


def _trust(n_sources: int = 1, n_claims: int = 1, n_citations: int = 1) -> dict[str, Any]:
    """A TextRunTrust-shaped block with the requested cardinalities."""
    return {
        "confidence": "high",
        "reasoning": "Multiple primary sources agree.",
        "sources": [
            {
                "url": f"https://source-{i}.example",
                "type": "primary",
                "title": f"Source {i}",
            }
            for i in range(n_sources)
        ],
        "claims": [
            {
                "callout": i + 1,
                "confidence": "high",
                "reasoning": "Directly stated by the source.",
                "citations": [
                    {
                        "url": f"https://cite-{i}-{j}.example",
                        "title": f"Citation {i}.{j}",
                        "excerpts": ["A verbatim supporting excerpt."],
                    }
                    for j in range(n_citations)
                ],
            }
            for i in range(n_claims)
        ],
    }


def _text_result(
    content: str = "The cited answer.", trust: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A completed-run result body with a text output."""
    return {
        "run": _run("completed"),
        "output": {"type": "text", "content": content, "trust": trust or _trust()},
    }


def _json_result(content: Any, trust: dict[str, Any] | None = None) -> dict[str, Any]:
    """A completed-run result body with a structured (JSON) output."""
    return {
        "run": _run("completed"),
        "output": {"type": "json", "content": content, "trust": trust or _trust()},
    }


class _FakeClock:
    """Deterministic stand-in for the module's ``_monotonic``/``_sleep`` seams."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Route the module's clock seams through a fake clock."""
    clock = _FakeClock()
    monkeypatch.setattr(nimble_research_mod, "_monotonic", clock.monotonic)
    monkeypatch.setattr(nimble_research_mod, "_sleep", clock.sleep)
    return clock


def _invoke(config: dict[str, str], ctx: ToolContext, args: dict[str, Any] | None = None) -> str:
    """Invoke the tool with JSON-encoded arguments, like the runner does."""
    tool = NimbleResearchTool(config=config)
    payload = args if args is not None else {"task": "Research Nimble's Agent API."}
    return tool.invoke(json.dumps(payload), ctx)


# ---------------------------------------------------------------------------
# Registry, schema, and sync-ness
# ---------------------------------------------------------------------------


def test_get_builtin_tool_returns_nimble_research() -> None:
    """``get_builtin_tool("nimble_research")`` returns a NimbleResearchTool."""
    tool = get_builtin_tool("nimble_research")
    assert isinstance(tool, NimbleResearchTool), (
        f"Expected NimbleResearchTool, got {type(tool).__name__}."
    )


def test_tool_name() -> None:
    """Tool name is 'nimble_research'."""
    assert NimbleResearchTool.name() == "nimble_research"


def test_schema_requires_task_and_offers_effort_enum() -> None:
    """The schema requires ``task`` and offers ``effort`` as a closed enum."""
    schema = NimbleResearchTool().get_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "nimble_research"
    params = schema["function"]["parameters"]
    assert params["required"] == ["task"]
    assert "query" not in params["properties"]
    assert params["properties"]["effort"]["enum"] == ["high", "low", "max", "medium", "x-high"]


def test_is_sync() -> None:
    """nimble_research runs synchronously (the runner bridges it off-loop)."""
    assert NimbleResearchTool().is_async() is False


# ---------------------------------------------------------------------------
# Config / argument validation (no HTTP may happen)
# ---------------------------------------------------------------------------


@respx.mock
def test_missing_api_key_returns_error(tool_ctx: ToolContext) -> None:
    """Without api_key the tool returns a clear error and makes no request."""
    out = _invoke({"agent_id": _AGENT_ID}, tool_ctx)
    assert out == "Error: api_key must be provided in the nimble_research config in config.yaml."
    assert respx.calls.call_count == 0


@respx.mock
def test_missing_agent_id_returns_error(tool_ctx: ToolContext) -> None:
    """Without agent_id the tool returns a bootstrap-pointing error, no request."""
    out = _invoke({"api_key": "test-key"}, tool_ctx)
    assert out.startswith("Error: agent_id must be provided")
    assert "/v2/agents" in out
    assert respx.calls.call_count == 0


@respx.mock
@pytest.mark.parametrize("raw", ["null", "[]", "42", '"scalar"'])
def test_non_object_arguments_return_error(tool_ctx: ToolContext, raw: str) -> None:
    """Valid-JSON non-object arguments return an error string, never raise."""
    out = NimbleResearchTool(config=_config()).invoke(raw, tool_ctx)
    assert out == "Error: arguments must be a JSON object"
    assert respx.calls.call_count == 0


@respx.mock
def test_missing_task_returns_error(tool_ctx: ToolContext) -> None:
    """Without a task the tool returns a clear error and makes no request."""
    out = _invoke(_config(), tool_ctx, args={})
    assert out == "Error: 'task' parameter is required."
    assert respx.calls.call_count == 0


@respx.mock
def test_blank_task_returns_error(tool_ctx: ToolContext) -> None:
    """A whitespace-only task is rejected before any request."""
    out = _invoke(_config(), tool_ctx, args={"task": "   "})
    assert out == "Error: 'task' parameter is required."
    assert respx.calls.call_count == 0


@respx.mock
def test_invalid_effort_config_returns_error(tool_ctx: ToolContext) -> None:
    """An unsupported spec-level effort gets the allowlist error, no request."""
    out = _invoke(_config(effort="turbo"), tool_ctx)
    assert out == "Error: unsupported effort 'turbo'. Use one of: high, low, max, medium, x-high."
    assert respx.calls.call_count == 0


@respx.mock
def test_invalid_effort_llm_arg_returns_error(tool_ctx: ToolContext) -> None:
    """An unsupported tool-call effort gets the allowlist error, no request."""
    out = _invoke(_config(), tool_ctx, args={"task": "x", "effort": "hyper"})
    assert out.startswith("Error: unsupported effort 'hyper'.")
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


@respx.mock
def test_create_request_shape_and_headers(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """The start request carries auth headers and ``{input, effort}``."""
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    result = respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    out = _invoke(_config(effort="low"), tool_ctx, args={"task": "profile Nimbleway"})

    assert create.called
    request = create.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["X-Client-Source"] == "omnigent"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert body == {"input": "profile Nimbleway", "effort": "low"}
    assert result.calls.last.request.headers["X-Client-Source"] == "omnigent"
    assert json.loads(out)["status"] == "completed"


@respx.mock
def test_llm_effort_overrides_config(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """The tool-call effort wins over the spec default."""
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    _invoke(_config(effort="low"), tool_ctx, args={"task": "x", "effort": "max"})
    assert json.loads(create.calls.last.request.content)["effort"] == "max"


@respx.mock
def test_effort_omitted_when_unset(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """With no effort anywhere, the field is omitted so the agent default applies."""
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    _invoke(_config(), tool_ctx)
    assert "effort" not in json.loads(create.calls.last.request.content)


@respx.mock
def test_base_url_env_override(
    tool_ctx: ToolContext, fake_clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OMNIGENT_NIMBLE_RESEARCH_BASE_URL`` reroutes every request (test/e2e seam)."""
    monkeypatch.setenv("OMNIGENT_NIMBLE_RESEARCH_BASE_URL", "http://127.0.0.1:9999/")
    stub_base = "http://127.0.0.1:9999"
    create = respx.post(f"{stub_base}/v2/agents/{_AGENT_ID}/runs").mock(
        return_value=httpx.Response(202, json=_run("completed"))
    )
    respx.get(f"{stub_base}/v2/agents/{_AGENT_ID}/runs/{_RUN_ID}/result").mock(
        return_value=httpx.Response(200, json=_text_result())
    )
    out = _invoke(_config(), tool_ctx)
    assert create.called
    assert json.loads(out)["run_id"] == _RUN_ID


# ---------------------------------------------------------------------------
# Lifecycle happy paths
# ---------------------------------------------------------------------------


@respx.mock
def test_lifecycle_queued_running_completed_text(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """queued → running → completed → result maps into the JSON envelope."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    poll = respx.get(_RUN_URL).mock(
        side_effect=[
            httpx.Response(200, json=_run("running")),
            httpx.Response(200, json=_run("completed")),
        ]
    )
    result = respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))

    out = _invoke(_config(), tool_ctx)

    assert poll.call_count == 2
    assert result.call_count == 1
    for route_calls in (poll.calls, result.calls):
        headers = route_calls.last.request.headers
        assert headers["X-Client-Source"] == "omnigent"
        assert headers["Authorization"] == "Bearer test-key"
    envelope = json.loads(out)
    assert envelope["run_id"] == _RUN_ID
    assert envelope["status"] == "completed"
    assert envelope["output"]["type"] == "text"
    assert envelope["output"]["content"] == "The cited answer."
    assert envelope["trust"]["confidence"] == "high"
    assert envelope["trust"]["sources"][0]["url"] == "https://source-0.example"
    assert envelope["trust"]["claims"][0]["citations"][0]["url"] == "https://cite-0-0.example"
    assert fake_clock.sleeps == [2.0, 2.0]


@respx.mock
def test_immediate_completion_zero_polls(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A run that is already terminal in the 202 body needs no poll requests."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    out = _invoke(_config(), tool_ctx)
    assert json.loads(out)["status"] == "completed"
    assert fake_clock.sleeps == []


@respx.mock
def test_json_output_envelope(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """Structured output passes through as-is under ``type: json``."""
    content = {"company": "Nimbleway", "founded": 2021, "products": ["Search", "Agents"]}
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_json_result(content)))
    envelope = json.loads(_invoke(_config(), tool_ctx))
    assert envelope["output"]["type"] == "json"
    assert envelope["output"]["content"] == content


# ---------------------------------------------------------------------------
# Envelope bounds
# ---------------------------------------------------------------------------


@respx.mock
def test_trust_sources_and_claims_capped(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """Oversized trust lists are sliced with omitted counts; excerpts dropped."""
    trust = _trust(n_sources=15, n_claims=12, n_citations=5)
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result(trust=trust)))
    out = _invoke(_config(), tool_ctx)
    envelope = json.loads(out)
    assert len(envelope["trust"]["sources"]) == 10
    assert envelope["trust"]["sources_omitted"] == 5
    assert len(envelope["trust"]["claims"]) == 10
    assert envelope["trust"]["claims_omitted"] == 2
    assert all(len(claim["citations"]) <= 3 for claim in envelope["trust"]["claims"])
    assert "excerpts" not in out


@respx.mock
def test_text_content_truncated_envelope_valid_json(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """Over-limit text is capped with a marker and the envelope still parses."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(
        return_value=httpx.Response(200, json=_text_result(content="x" * 60_000))
    )
    envelope = json.loads(_invoke(_config(), tool_ctx))
    assert envelope["output"]["content"].endswith("[truncated]")
    assert len(envelope["output"]["content"]) <= 50_100
    assert envelope["output"]["content_truncated"] is True


# ---------------------------------------------------------------------------
# Run id validation and preservation
# ---------------------------------------------------------------------------


@respx.mock
def test_create_id_without_prefix_is_protocol_error(tool_ctx: ToolContext) -> None:
    """A 202 whose id is not ``task_run_...`` is a protocol error; no polling."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued", id="run_123")))
    out = _invoke(_config(), tool_ctx)
    assert out == "Nimble research error: expected a 'task_run_...' run id, got 'run_123'."
    assert respx.calls.call_count == 1


@respx.mock
def test_create_missing_id_is_protocol_error(tool_ctx: ToolContext) -> None:
    """A 202 without an id is a protocol error; no polling."""
    body = _run("queued")
    del body["id"]
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=body))
    out = _invoke(_config(), tool_ctx)
    assert "expected a 'task_run_...' run id, got None" in out
    assert respx.calls.call_count == 1


# ---------------------------------------------------------------------------
# Terminal failures, unknown status, timeout
# ---------------------------------------------------------------------------


@respx.mock
def test_failed_run_error_no_result_call(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A ``failed`` run surfaces its message with the run id; /result untouched."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    respx.get(_RUN_URL).mock(
        return_value=httpx.Response(
            200,
            json=_run("failed", error={"ref_id": _RUN_ID, "message": "source unreachable"}),
        )
    )
    result = respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json={}))
    out = _invoke(_config(), tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} failed: source unreachable"
    assert result.call_count == 0


@respx.mock
def test_cancelled_run_error(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A ``cancelled`` run surfaces clearly with the run id preserved."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    respx.get(_RUN_URL).mock(return_value=httpx.Response(200, json=_run("cancelled")))
    out = _invoke(_config(), tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} cancelled without an error message."


@respx.mock
def test_unknown_status_protocol_error(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """An undocumented status stops polling with a protocol error + run id."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    poll = respx.get(_RUN_URL).mock(return_value=httpx.Response(200, json=_run("exploded")))
    out = _invoke(_config(), tool_ctx)
    assert f"Nimble research run {_RUN_ID} returned unknown status 'exploded'" in out
    assert poll.call_count == 1


@respx.mock
def test_timeout_uses_monotonic_deadline(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """The deadline is elapsed-time based; the message keeps run id + status."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    respx.get(_RUN_URL).mock(return_value=httpx.Response(200, json=_run("running")))
    out = _invoke(
        _config(timeout_seconds="10", poll_interval_seconds="2"),
        tool_ctx,
    )
    assert out == (
        f"Nimble research run {_RUN_ID} timed out after 10s (last status: running). "
        "The run may still complete server-side."
    )
    assert fake_clock.sleeps == [2.0] * 5, "polling must pace by the configured interval"


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


@respx.mock
def test_auth_401_no_retry(tool_ctx: ToolContext) -> None:
    """A 401 on create fails fast with an actionable message; no retries."""
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(401))
    out = _invoke(_config(), tool_ctx)
    assert out == (
        "Nimble research error: HTTP 401 (authentication failed — check the api_key "
        "in the nimble_research config)."
    )
    assert create.call_count == 1


@respx.mock
def test_create_404_names_agent_id(tool_ctx: ToolContext) -> None:
    """A 404 on create points at the configured agent_id."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(404))
    out = _invoke(_config(), tool_ctx)
    assert "HTTP 404" in out
    assert "agent_id" in out


@respx.mock
def test_poll_403_no_blind_retry(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A permission error while polling fails immediately with the run id."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    poll = respx.get(_RUN_URL).mock(return_value=httpx.Response(403))
    out = _invoke(_config(), tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} polling failed: HTTP 403"
    assert poll.call_count == 1


@respx.mock
def test_poll_429_honors_retry_after(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A 429's Retry-After adds a full extra wait on top of the poll pacing."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    respx.get(_RUN_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=_run("completed")),
        ]
    )
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    out = _invoke(_config(), tool_ctx)
    assert json.loads(out)["status"] == "completed"
    assert fake_clock.sleeps == [2.0, 7.0, 2.0], (
        "pacing sleep, then the honored Retry-After, then the next pacing sleep"
    )


@respx.mock
def test_poll_5xx_transient_then_success(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A couple of 5xx responses are retried; the lifecycle then completes."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    respx.get(_RUN_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json=_run("running")),
            httpx.Response(200, json=_run("completed")),
        ]
    )
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    out = _invoke(_config(), tool_ctx)
    assert json.loads(out)["status"] == "completed"


@respx.mock
def test_poll_transient_budget_exhausted(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """Persistent 5xx exhausts the bounded retry budget with the run id kept."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    poll = respx.get(_RUN_URL).mock(return_value=httpx.Response(503))
    out = _invoke(_config(), tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} polling failed: HTTP 503"
    assert poll.call_count == 4, "three retries after the first failure, then give up"


@respx.mock
def test_transport_error_on_create(tool_ctx: ToolContext) -> None:
    """A connect failure on create returns an error string, never raises."""
    respx.post(_RUNS_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    out = _invoke(_config(), tool_ctx)
    assert out.startswith("Nimble research error: ")


# ---------------------------------------------------------------------------
# /result handling
# ---------------------------------------------------------------------------


@respx.mock
def test_result_409_race_resumes(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A 409 right after observing ``completed`` is re-checked, then succeeds."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    result = respx.get(_RESULT_URL).mock(
        side_effect=[
            httpx.Response(409),
            httpx.Response(200, json=_text_result()),
        ]
    )
    out = _invoke(_config(), tool_ctx)
    assert json.loads(out)["status"] == "completed"
    assert result.call_count == 2


@respx.mock
def test_result_409_budget_exhausted(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """Persistent 409s after completed exhaust the bounded re-check budget."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    result = respx.get(_RESULT_URL).mock(return_value=httpx.Response(409))
    out = _invoke(_config(), tool_ctx)
    assert out == (
        f"Nimble research run {_RUN_ID} completed but its result was not yet "
        "available (HTTP 409). Retry the task to fetch it."
    )
    assert result.call_count == 4, "three re-checks after the first 409, then give up"


@respx.mock
def test_oversized_json_content_falls_back_to_marked_string(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """Oversized structured output becomes a truncated string, flagged, type kept."""
    big = {"rows": [{"snippet": "x" * 60_000}]}
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_json_result(big)))
    envelope = json.loads(_invoke(_config(), tool_ctx))
    assert envelope["output"]["type"] == "json"
    assert isinstance(envelope["output"]["content"], str)
    assert envelope["output"]["content"].endswith("[truncated]")
    assert envelope["output"]["content_truncated"] is True


@respx.mock
def test_poll_5xx_error_body_detail_surfaced(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """Exhausted 5xx polling surfaces the server's message and task id."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    respx.get(_RUN_URL).mock(
        return_value=httpx.Response(
            500, json={"message": "backend hiccup", "task_id": "task_poll_500"}
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert out == (
        f"Nimble research run {_RUN_ID} polling failed: "
        "HTTP 500: backend hiccup (task: task_poll_500)"
    )


@respx.mock
def test_result_422_parses_run_and_error(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A 422 result body maps to the failure message with the run id."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(
        return_value=httpx.Response(
            422,
            json={
                "run": _run("failed"),
                "error": {"ref_id": _RUN_ID, "message": "extraction failed"},
            },
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} failed: extraction failed"


@respx.mock
def test_result_200_failed_body_shape(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """Defensive: an HTTP 200 whose body carries an error (no output) is a failure."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "run": _run("failed"),
                "error": {"ref_id": _RUN_ID, "message": "downstream error"},
            },
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} failed: downstream error"


@respx.mock
def test_result_malformed_json_keeps_run_id(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A non-JSON result body becomes a decode error that keeps the run id."""
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    out = _invoke(_config(), tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} result was not valid JSON."


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_api_key_never_in_any_output(
    tool_ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The api_key never leaks into any returned message, on any path."""
    secret = "sk-SECRET-VALUE"
    config = _config(api_key=secret, timeout_seconds="10", poll_interval_seconds="2")
    clock = _FakeClock()
    monkeypatch.setattr(nimble_research_mod, "_monotonic", clock.monotonic)
    monkeypatch.setattr(nimble_research_mod, "_sleep", clock.sleep)

    with respx.mock:
        respx.post(_RUNS_URL).mock(return_value=httpx.Response(401))
        outputs = [_invoke(config, tool_ctx)]
    with respx.mock:
        respx.post(_RUNS_URL).mock(side_effect=httpx.ConnectError("refused"))
        outputs.append(_invoke(config, tool_ctx))
    with respx.mock:
        respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
        respx.get(_RUN_URL).mock(return_value=httpx.Response(200, json=_run("running")))
        outputs.append(_invoke(config, tool_ctx))

    for out in outputs:
        assert secret not in out, f"api_key leaked into: {out!r}"


# ---------------------------------------------------------------------------
# Helper units
# ---------------------------------------------------------------------------


def test_clamp_seconds_junk_and_bounds() -> None:
    """Numeric config coercion: junk → default, out-of-range → clamped."""
    assert _clamp_seconds({}, "timeout_seconds", 300.0, 10.0, 3600.0) == 300.0
    assert (
        _clamp_seconds({"timeout_seconds": "abc"}, "timeout_seconds", 300.0, 10.0, 3600.0) == 300.0
    )
    assert (
        _clamp_seconds({"timeout_seconds": "-5"}, "timeout_seconds", 300.0, 10.0, 3600.0) == 10.0
    )
    assert (
        _clamp_seconds({"timeout_seconds": "99999"}, "timeout_seconds", 300.0, 10.0, 3600.0)
        == 3600.0
    )
    assert (
        _clamp_seconds({"timeout_seconds": "45"}, "timeout_seconds", 300.0, 10.0, 3600.0) == 45.0
    )
    assert (
        _clamp_seconds({"timeout_seconds": "nan"}, "timeout_seconds", 300.0, 10.0, 3600.0) == 300.0
    )


def test_resolve_effort_precedence_and_validation() -> None:
    """Tool-call effort beats config; both validate; unset means omit."""
    assert _resolve_effort({}, None) == (None, None)
    assert _resolve_effort({"effort": "low"}, None) == ("low", None)
    assert _resolve_effort({"effort": "low"}, "max") == ("max", None)
    assert _resolve_effort({}, "X-HIGH") == ("x-high", None)
    effort, error = _resolve_effort({"effort": "warp"}, None)
    assert effort is None
    assert error is not None and "warp" in error


def test_map_trust_non_dict_is_empty() -> None:
    """Absent/malformed trust maps to an empty dict, not a crash."""
    assert _map_trust(None) == {}
    assert _map_trust("high") == {}
