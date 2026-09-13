"""Tests for the ``nimble_research`` built-in tool (Nimble Agent API v2 backend).

HTTP is mocked at the transport layer with ``respx`` so the start → poll →
result lifecycle exercises real URLs, status codes, and headers. Time is
controlled by monkeypatching the module's ``_monotonic``/``_sleep`` seams —
never the process-global ``time`` module.

Explicit 2-second polling values are test-only overrides used to exercise
pacing and deadlines without lengthening the suite. The runtime default is
10 seconds.
"""

from __future__ import annotations

import json
import sys

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
# The generated-agent create route, used when no agent_id is configured.
_GENERIC_RUNS_URL = f"{_BASE}/v2/agents/runs"
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


def test_schema_requires_task_and_exposes_selectable_effort_tiers() -> None:
    """C04: only generally available tiers are selectable."""
    schema = NimbleResearchTool().get_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "nimble_research"
    params = schema["function"]["parameters"]
    assert params["required"] == ["task"]
    assert "query" not in params["properties"]
    assert params["properties"]["effort"]["enum"] == [
        "high",
        "low",
        "medium",
        "x-high",
    ]


def test_schema_exposes_published_per_run_controls() -> None:
    """C07/C08/C09: the published per-run controls are reachable from a tool call."""
    params = NimbleResearchTool().get_schema()["function"]["parameters"]
    assert set(params["properties"]) == {
        "task",
        "effort",
        "agent_name",
        "input_data",
        "output_schema",
        "skill",
        "sources",
        "use_case",
    }


def test_schema_exposes_sdk_12_typed_fields() -> None:
    """SDK 1.2 typed fields are offered without an escape hatch."""
    params = NimbleResearchTool().get_schema()["function"]["parameters"]
    for typed in ("skill", "use_case", "agent_name"):
        assert typed in params["properties"]


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
def test_missing_agent_id_uses_generic_create_route(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """C01: without agent_id the run is created on ``POST /v2/agents/runs``."""
    create = respx.post(_GENERIC_RUNS_URL).mock(
        return_value=httpx.Response(202, json=_run("completed"))
    )
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))

    out = _invoke({"api_key": "test-key"}, tool_ctx)

    assert create.call_count == 1, "exactly one create POST"
    assert json.loads(out)["status"] == "completed"


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
    """An unsupported spec-level effort is rejected before any request."""
    out = _invoke(_config(effort="turbo"), tool_ctx)
    assert out == ("Error: unsupported effort 'turbo'. Choose low, medium, high, or x-high.")
    assert respx.calls.call_count == 0


@respx.mock
def test_invalid_effort_llm_arg_returns_error(tool_ctx: ToolContext) -> None:
    """An unsupported tool-call effort is rejected before any request."""
    out = _invoke(_config(), tool_ctx, args={"task": "x", "effort": "hyper"})
    assert out.startswith("Error: unsupported effort 'hyper'.")
    assert respx.calls.call_count == 0


@respx.mock
def test_max_effort_is_rejected_unbilled(tool_ctx: ToolContext) -> None:
    """C04: default max treatment stops with positive guidance before create."""
    out = _invoke(_config(), tool_ctx, args={"task": "x", "effort": "max"})
    assert "coming-soon custom-budget capability" in out
    assert "not generally selectable" in out
    assert "nothing was sent and no run was created" in out
    assert "explicitly choose x-high" in out
    assert "https://www.nimbleway.com/contact" in out
    assert respx.calls.call_count == 0


@respx.mock
def test_non_string_use_case_is_rejected_unbilled(tool_ctx: ToolContext) -> None:
    """A non-string use_case is a clear tool error, not a TypeError from the
    frozenset membership test (an unhashable list), and issues no request."""
    out = _invoke(_config(), tool_ctx, args={"task": "x", "use_case": []})
    assert out == "Error: 'use_case' must be research, enrichment, or dataset_building."
    assert respx.calls.call_count == 0


def test_response_validation_error_on_create_warns_do_not_resubmit(
    tool_ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2xx create whose body fails SDK validation raises outside
    APIStatusError/APIConnectionError; the run may exist and be billed, so the
    error must carry the do-not-resubmit guidance and point at run history."""
    import nimble_python

    err = nimble_python.APIResponseValidationError(
        response=httpx.Response(202, request=httpx.Request("POST", f"{_BASE}/v2/agents/runs")),
        body={},
    )

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            def _raise(*args: Any, **kw: Any) -> Any:
                raise err

            class _Agents:
                run = staticmethod(_raise)

                class runs:
                    create = staticmethod(_raise)

            self.agents = _Agents()

        def close(self) -> None:
            pass

    monkeypatch.setattr(nimble_python, "Nimble", _FakeClient)
    out = _invoke(_config(), tool_ctx, args={"task": "x"})
    assert "Do not retry or resubmit this task automatically" in out
    assert "run history" in out


def test_request_timeout_clamps_to_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single HTTP call never outlives the tool's overall deadline."""
    monkeypatch.setattr(nimble_research_mod, "_monotonic", lambda: 100.0)
    assert nimble_research_mod._request_timeout(105.0) == 5.0
    assert nimble_research_mod._request_timeout(1000.0) == 30.0
    assert nimble_research_mod._request_timeout(99.0) == 0.001


@respx.mock
def test_missing_sdk_names_the_nimble_extra_unbilled(
    tool_ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without nimble-python installed (optional `nimble` extra), the tool
    names the extra to install and issues no request, so nothing is billed."""
    monkeypatch.setitem(sys.modules, "nimble_python", None)
    out = _invoke(_config(), tool_ctx, args={"task": "x"})
    assert "nimble-python client is not installed" in out
    assert "omnigent[nimble]" in out
    assert respx.calls.call_count == 0


@respx.mock
def test_unknown_config_keys_cannot_make_max_selectable(tool_ctx: ToolContext) -> None:
    """No config key can turn the coming-soon tier into a silent x-high downgrade.

    `gate_policy` is not read anywhere in the tool — this pins that fact: an
    unrecognized key stays inert instead of re-opening a substitution path.
    """
    out = _invoke(
        _config(gate_policy="degrade"),
        tool_ctx,
        args={"task": "x", "effort": "max"},
    )
    assert "nothing was sent and no run was created" in out
    assert "explicitly choose x-high" in out
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
@pytest.mark.parametrize("tier", ["low", "medium", "high", "x-high"])
def test_explicit_released_effort_is_accepted(
    tool_ctx: ToolContext, fake_clock: _FakeClock, tier: str
) -> None:
    """An explicit generally available tier is honored."""
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    _invoke(_config(), tool_ctx, args={"task": "x", "effort": tier})
    assert json.loads(create.calls.last.request.content)["effort"] == tier


@respx.mock
def test_effort_is_omitted_when_unset(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """C04: omission preserves the selected agent/template default."""
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
    assert fake_clock.sleeps == [10.0, 10.0]


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
    assert out.startswith(
        "Nimble research error: expected a 'task_run_...' run id, got 'run_123'."
    )
    # The run was still created, so the caller is told not to resubmit.
    assert "Do not retry or resubmit this task automatically" in out
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
    assert out.startswith(
        f"Nimble research run {_RUN_ID} timed out after 10s (last status: running). "
        "The run may still complete server-side."
    )
    assert "Do not resubmit this task automatically" in out
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
    assert out.startswith(f"Nimble research run {_RUN_ID} polling failed: HTTP 403")
    assert "Do not resubmit this task automatically" in out
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
    assert fake_clock.sleeps == [10.0, 7.0, 10.0], (
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
    assert out.startswith(f"Nimble research run {_RUN_ID} polling failed: HTTP 503")
    assert "Do not resubmit this task automatically" in out
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
    assert out.startswith(
        f"Nimble research run {_RUN_ID} completed but its result was not yet available (HTTP 409)."
    )
    assert "Do not resubmit this task automatically" in out
    assert "Retry the task" not in out
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
    assert out.startswith(
        f"Nimble research run {_RUN_ID} polling failed: "
        "HTTP 500: backend hiccup (task: task_poll_500)"
    )
    assert "Do not resubmit this task automatically" in out


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
    assert out.startswith(f"Nimble research run {_RUN_ID} result was not valid JSON.")
    assert "Do not resubmit this task automatically" in out


# ---------------------------------------------------------------------------
# Malformed config / envelope bounds (hardening)
# ---------------------------------------------------------------------------


@respx.mock
def test_control_char_agent_id_returns_error_not_raises(tool_ctx: ToolContext) -> None:
    """A control char in the configured agent_id yields a clean error, never raises."""
    out = _invoke(_config(agent_id="wsa_\n0000"), tool_ctx)
    assert out.startswith("Nimble research error:")
    assert respx.calls.call_count == 0


@respx.mock
def test_oversized_trust_reasoning_is_capped(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """A huge API-supplied trust.reasoning is capped; the envelope stays valid JSON."""
    trust = _trust()
    trust["reasoning"] = "z" * 60_000
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result(trust=trust)))
    envelope = json.loads(_invoke(_config(), tool_ctx))
    reasoning = envelope["trust"]["reasoning"]
    assert len(reasoning) <= 2_010
    assert reasoning.endswith("…")


@respx.mock
@pytest.mark.parametrize("status", ["queued", "completed"])
def test_path_unsafe_run_id_is_rejected_at_create(
    tool_ctx: ToolContext, fake_clock: _FakeClock, status: str
) -> None:
    """An API-supplied run id that is unsafe in a URL never reaches a request.

    ``task_run_`` is only a prefix, so a value like ``task_run_../..`` passes a
    prefix check while resolving to a different path once httpx normalizes it.
    """
    for bad in ("task_run_\n0000", "task_run_../../admin", "task_run_a/b"):
        with respx.mock:
            create = respx.post(_RUNS_URL).mock(
                return_value=httpx.Response(202, json=_run(status, id=bad))
            )
            out = _invoke(_config(), tool_ctx)
            assert out.startswith("Nimble research error: expected a 'task_run_...' run id"), out
            assert create.call_count == 1
            assert respx.calls.call_count == 1, "no poll or result request may be issued"


@respx.mock
def test_all_trust_string_fields_are_capped(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """Every API-supplied trust string is bounded; the envelope stays small."""
    big = "z" * 100_000
    trust = {
        "confidence": big,
        "reasoning": big,
        "sources": [{"url": big, "type": big, "title": big}],
        "claims": [
            {
                "callout": 1,
                "path": big,
                "confidence": big,
                "citations": [{"url": big, "title": big}],
            }
        ],
    }
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result(trust=trust)))
    raw = _invoke(_config(), tool_ctx)
    envelope = json.loads(raw)
    t = envelope["trust"]
    for value in (t["confidence"], t["reasoning"]):
        assert len(value) <= 2_010, "trust scalar not capped"
    src = t["sources"][0]
    for key in ("url", "type", "title"):
        assert len(src[key]) <= 2_010, f"source.{key} not capped"
    claim = t["claims"][0]
    for key in ("path", "confidence"):
        assert len(claim[key]) <= 2_010, f"claim.{key} not capped"
    assert len(claim["citations"][0]["url"]) <= 2_010
    assert len(claim["citations"][0]["title"]) <= 2_010
    assert len(raw) < 50_000, f"envelope not bounded: {len(raw)} chars"


@respx.mock
def test_non_string_source_url_is_dropped(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """A non-string url is dropped rather than passed through unbounded."""
    trust = {
        "confidence": "high",
        "reasoning": "r",
        "sources": [{"url": {"nested": "x" * 50_000}}],
        "claims": [],
    }
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result(trust=trust)))
    raw = _invoke(_config(), tool_ctx)
    assert json.loads(raw)["trust"]["sources"][0]["url"] is None
    assert len(raw) < 10_000


@respx.mock
def test_oversized_server_error_message_is_capped(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """A hostile multi-KB server error message is bounded in the error string."""
    respx.post(_RUNS_URL).mock(
        return_value=httpx.Response(500, json={"message": "q" * 100_000, "task_id": "t"})
    )
    out = _invoke(_config(), tool_ctx)
    assert out.startswith("Nimble research error: HTTP 500")
    assert len(out) < 5_000, f"error string not bounded: {len(out)} chars"


@respx.mock
def test_oversized_status_from_result_body_is_bounded(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """A hostile run status in the result body can't inflate the error string.

    The status is read from the response body (not a URL), so nothing else
    bounds it; only a known terminal status is trusted.
    """
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(
        return_value=httpx.Response(
            422,
            json={"run": {"status": "s" * 200_000}, "error": {"message": "boom"}},
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} failed: boom"
    assert len(out) < 5_000, f"error string not bounded: {len(out)} chars"


@respx.mock
def test_oversized_run_id_is_rejected(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """An implausibly long run id is a protocol error, not a value we echo or route on."""
    respx.post(_RUNS_URL).mock(
        return_value=httpx.Response(202, json=_run("completed", id="task_run_" + "z" * 50_000))
    )
    out = _invoke(_config(), tool_ctx)
    assert out.startswith("Nimble research error: expected a 'task_run_...' run id")
    assert len(out) < 5_000, f"error string not bounded: {len(out)} chars"


@respx.mock
def test_trust_section_bounded_at_max_cardinality(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """Per-field caps multiply across cardinality, so the section is bounded as a whole."""
    big = "y" * 5_000
    trust = {
        "confidence": "high",
        "reasoning": big,
        "sources": [{"url": big, "type": big, "title": big} for _ in range(10)],
        "claims": [
            {
                "path": big,
                "confidence": big,
                "citations": [{"url": big, "title": big} for _ in range(3)],
            }
            for _ in range(10)
        ],
    }
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result(trust=trust)))
    raw = _invoke(_config(), tool_ctx)
    envelope = json.loads(raw)
    assert envelope["trust"]["trust_truncated"] is True
    assert len(raw) < 60_000, f"envelope not bounded at max cardinality: {len(raw)} chars"


@respx.mock
def test_non_string_citation_url_is_dropped(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """Parity with sources: a non-string citation url is dropped, not passed through."""
    trust = {
        "confidence": "high",
        "reasoning": "r",
        "sources": [],
        "claims": [{"path": "$.a", "citations": [{"url": {"nested": "x" * 50_000}}]}],
    }
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result(trust=trust)))
    raw = _invoke(_config(), tool_ctx)
    assert json.loads(raw)["trust"]["claims"][0]["citations"][0]["url"] is None
    assert len(raw) < 10_000


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


def test_resolve_effort_preserves_omission_and_validates_tiers() -> None:
    """Unset stays omitted; released tiers normalize; max always stops."""
    assert _resolve_effort({}, None) == (None, None)
    assert _resolve_effort({"effort": "  "}, None) == (None, None)
    for tier in ("low", "medium", "high", "x-high"):
        assert _resolve_effort({}, tier.upper()) == (tier, None)
    effort, error = _resolve_effort({"effort": "max"}, None)
    assert effort is None
    assert error is not None and "nothing was sent" in error
    # A resolved effort is always exactly what was asked for: there is no
    # substitution channel, so no legacy config can turn max into another tier.
    effort, error = _resolve_effort({"effort": "max", "gate_policy": "degrade"}, None)
    assert effort is None
    assert error is not None and "explicitly choose x-high" in error


def test_map_trust_non_dict_is_empty() -> None:
    """Absent/malformed trust maps to an empty dict, not a crash."""
    assert _map_trust(None) == {}
    assert _map_trust("high") == {}


# ---------------------------------------------------------------------------
# Launch contract: optional identity, create-once, structured controls (C01-C14)
# ---------------------------------------------------------------------------

_GENERATED_AGENT_ID = "wsa_9f8e7d6c-0000-4000-8000-999999999999"
_GENERATED_RUN_URL = f"{_BASE}/v2/agents/{_GENERATED_AGENT_ID}/runs/{_RUN_ID}"
_GENERATED_RESULT_URL = f"{_GENERATED_RUN_URL}/result"


@respx.mock
def test_configured_agent_id_uses_persistent_create_route(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """C02: with agent_id the run is created on ``POST /v2/agents/{agent_id}/runs``."""
    completed = httpx.Response(202, json=_run("completed"))
    generic = respx.post(_GENERIC_RUNS_URL).mock(return_value=completed)
    persistent = respx.post(_RUNS_URL).mock(return_value=completed)
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))

    _invoke(_config(), tool_ctx)

    assert persistent.call_count == 1
    assert generic.call_count == 0, "a configured agent must not hit the generic route"


@respx.mock
@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503])
def test_create_is_never_retried_on_failure(tool_ctx: ToolContext, status: int) -> None:
    """C03: a failed create is never re-issued — a run is billable and not idempotent."""
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(status))
    out = _invoke(_config(), tool_ctx)
    assert create.call_count == 1, f"HTTP {status} must not trigger a second create"
    assert out.startswith("Nimble research error:")


@respx.mock
def test_create_transport_error_is_never_retried(tool_ctx: ToolContext) -> None:
    """C03: a transport failure on create is not retried either.

    A connection error is the most tempting case to retry, and the most
    dangerous: the request may have reached the API and started a billed run.
    """
    create = respx.post(_RUNS_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    out = _invoke(_config(), tool_ctx)
    assert create.call_count == 1
    assert out.startswith("Nimble research error: ")


@respx.mock
def test_generic_create_is_never_retried(tool_ctx: ToolContext) -> None:
    """C03: the create-once rule holds on the generic route too."""
    create = respx.post(_GENERIC_RUNS_URL).mock(return_value=httpx.Response(503))
    _invoke({"api_key": "test-key"}, tool_ctx)
    assert create.call_count == 1


@respx.mock
def test_generated_agent_id_drives_status_and_result(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """C05: a generated run's returned agent id addresses the rest of the lifecycle."""
    respx.post(_GENERIC_RUNS_URL).mock(
        return_value=httpx.Response(
            202, json=_run("queued", web_search_agent_id=_GENERATED_AGENT_ID)
        )
    )
    poll = respx.get(_GENERATED_RUN_URL).mock(
        return_value=httpx.Response(
            200, json=_run("completed", web_search_agent_id=_GENERATED_AGENT_ID)
        )
    )
    result = respx.get(_GENERATED_RESULT_URL).mock(
        return_value=httpx.Response(200, json=_text_result())
    )

    envelope = json.loads(_invoke({"api_key": "test-key"}, tool_ctx))

    assert poll.call_count == 1, "polling must target the returned agent"
    assert result.call_count == 1, "result must target the returned agent"
    assert envelope["run_id"] == _RUN_ID
    assert envelope["web_search_agent_id"] == _GENERATED_AGENT_ID, (
        "the generated agent id is only knowable from this run; it must survive"
    )


@respx.mock
def test_owner_mismatch_is_rejected_not_substituted(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """C06: a create whose run belongs to another agent stops the lifecycle."""
    respx.post(_RUNS_URL).mock(
        return_value=httpx.Response(
            202, json=_run("completed", web_search_agent_id=_GENERATED_AGENT_ID)
        )
    )
    configured_result = respx.get(_RESULT_URL).mock(
        return_value=httpx.Response(200, json=_text_result())
    )
    returned_result = respx.get(_GENERATED_RESULT_URL).mock(
        return_value=httpx.Response(200, json=_text_result())
    )

    out = _invoke(_config(), tool_ctx)

    assert "mismatched agent" in out, out
    assert _GENERATED_AGENT_ID in out and _AGENT_ID in out, "both ids must be named"
    assert configured_result.call_count == 0
    assert returned_result.call_count == 0, "neither id may be silently chosen"


@respx.mock
@pytest.mark.parametrize("missing", [None, "", 42])
def test_create_without_usable_agent_id_is_protocol_error(
    tool_ctx: ToolContext, missing: object
) -> None:
    """C05: a create response with no usable agent id cannot be polled."""
    body = _run("completed")
    if missing is None:
        del body["web_search_agent_id"]
    else:
        body["web_search_agent_id"] = missing
    respx.post(_GENERIC_RUNS_URL).mock(return_value=httpx.Response(202, json=body))
    out = _invoke({"api_key": "test-key"}, tool_ctx)
    assert out.startswith("Nimble research error:")
    assert "returned agent id" in out
    assert respx.calls.call_count == 1, "no lifecycle call may follow"


@respx.mock
@pytest.mark.parametrize(
    "hostile",
    ["wsa_\n0000", "../../../v1/admin", "x/../../../etc", "a#frag", "a?evil=1", "a/b"],
)
def test_path_unsafe_returned_agent_id_is_rejected(tool_ctx: ToolContext, hostile: str) -> None:
    """C05: a server-supplied agent id cannot retarget our authenticated requests.

    On the generic route there is no configured id to compare against, so the
    returned value is the only thing addressing the poll and result URLs. httpx
    resolves dot segments and honors ``#``/``?``, so an unvalidated id would
    silently send the bearer token to a different endpoint.
    """
    respx.post(_GENERIC_RUNS_URL).mock(
        return_value=httpx.Response(202, json=_run("completed", web_search_agent_id=hostile))
    )
    out = _invoke({"api_key": "test-key"}, tool_ctx)
    assert out.startswith("Nimble research error:"), out
    assert "not a valid agent id" in out
    assert respx.calls.call_count == 1, "no request may be issued to the hostile path"


@respx.mock
@pytest.mark.parametrize("hostile", ["../../../v1/agents", "wsa_a/b", "wsa_a#f", "wsa_a?q=1"])
def test_path_unsafe_configured_agent_id_is_rejected(tool_ctx: ToolContext, hostile: str) -> None:
    """A path-unsafe agent id from the spec is rejected before the create POST."""
    out = _invoke(_config(agent_id=hostile), tool_ctx)
    assert out.startswith("Nimble research error:"), out
    assert "not a valid agent id" in out
    assert respx.calls.call_count == 0, "the credential must not reach an unintended endpoint"


@respx.mock
@pytest.mark.parametrize("route", ["persistent", "generic"])
def test_structured_controls_pass_through_unchanged(
    tool_ctx: ToolContext, fake_clock: _FakeClock, route: str
) -> None:
    """C07/C08/C09: input_data, output_schema, and sources reach both create routes intact."""
    output_schema = {
        "type": "object",
        "properties": {"revenue": {"type": "number"}, "hq": {"type": "string"}},
        "required": ["revenue"],
    }
    input_data = [{"company": "Nimbleway"}, {"company": "Acme"}]
    sources = {
        "allow": [{"title": "SEC", "domains": ["sec.gov"], "order": 1}],
        "avoid": "Do not use discussion forums.",
    }

    is_generic = route == "generic"
    create_url = _GENERIC_RUNS_URL if is_generic else _RUNS_URL
    config = {"api_key": "test-key"} if is_generic else _config()
    create = respx.post(create_url).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))

    _invoke(
        config,
        tool_ctx,
        args={
            "task": "enrich these companies",
            "input_data": input_data,
            "output_schema": output_schema,
            "sources": sources,
        },
    )

    body = json.loads(create.calls.last.request.content)
    assert body["input_data"] == input_data, "enrichment rows must not be remapped"
    assert body["output_schema"] == output_schema, "the schema is the API's contract, verbatim"
    assert body["sources"] == sources


@respx.mock
def test_input_data_accepts_a_single_object(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """C08: input_data may be one object, not only an array."""
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    _invoke(_config(), tool_ctx, args={"task": "x", "input_data": {"company": "Nimbleway"}})
    assert json.loads(create.calls.last.request.content)["input_data"] == {"company": "Nimbleway"}


@respx.mock
def test_controls_absent_when_not_requested(tool_ctx: ToolContext, fake_clock: _FakeClock) -> None:
    """An unused control is omitted rather than sent as null."""
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))
    _invoke(_config(), tool_ctx)
    body = json.loads(create.calls.last.request.content)
    assert set(body) == {"input"}


@respx.mock
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("input_data", "not-an-object"),
        ("input_data", [{"ok": 1}, "not-an-object"]),
        ("output_schema", ["not", "an", "object"]),
        ("sources", "sec.gov"),
    ],
)
def test_malformed_controls_are_rejected_unbilled(
    tool_ctx: ToolContext, key: str, value: object
) -> None:
    """A malformed control fails locally rather than as a 422 on a billable request."""
    out = _invoke(_config(), tool_ctx, args={"task": "x", key: value})
    assert out.startswith(f"Error: {key!r} must be a JSON object"), out
    assert respx.calls.call_count == 0


@respx.mock
@pytest.mark.parametrize(
    "sources",
    [
        {"allow": ["python.org"]},
        {"allow": [{"title": "Python", "domains": []}]},
        {"allow": [{"title": "", "domains": ["python.org"]}]},
        {"prioritize": ["python.org"]},
        {"unknown": "python.org"},
    ],
)
def test_malformed_sources_are_rejected_unbilled(tool_ctx: ToolContext, sources: object) -> None:
    """The SDK 1.2 sources shape is enforced before the billable create POST."""
    out = _invoke(_config(), tool_ctx, args={"task": "x", "sources": sources})
    assert out.startswith("Error: 'sources"), out
    assert respx.calls.call_count == 0


@respx.mock
@pytest.mark.parametrize("route", ["persistent", "generic"])
def test_sdk_12_typed_fields_reach_both_create_routes(
    tool_ctx: ToolContext, fake_clock: _FakeClock, route: str
) -> None:
    """SDK 1.2 fields are forwarded directly on both routes."""
    is_generic = route == "generic"
    create_url = _GENERIC_RUNS_URL if is_generic else _RUNS_URL
    config = {"api_key": "test-key"} if is_generic else _config()
    create = respx.post(create_url).mock(return_value=httpx.Response(202, json=_run("completed")))
    respx.get(_RESULT_URL).mock(return_value=httpx.Response(200, json=_text_result()))

    _invoke(
        config,
        tool_ctx,
        args={
            "task": "x",
            "skill": "company-profile",
            "use_case": "research",
            "agent_name": "my-agent",
        },
    )

    body = json.loads(create.calls.last.request.content)
    assert body["skill"] == "company-profile"
    assert body["use_case"] == "research"
    assert body["agent_name"] == "my-agent"
    assert "extra_body" not in body


@respx.mock
def test_client_source_and_no_credential_reflection_on_every_hop(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """C14: attribution is preserved on all three hops and the key stays in the header."""
    secret = "sk-ATTRIBUTION-SECRET"
    create = respx.post(_GENERIC_RUNS_URL).mock(
        return_value=httpx.Response(
            202, json=_run("queued", web_search_agent_id=_GENERATED_AGENT_ID)
        )
    )
    poll = respx.get(_GENERATED_RUN_URL).mock(
        return_value=httpx.Response(
            200, json=_run("completed", web_search_agent_id=_GENERATED_AGENT_ID)
        )
    )
    result = respx.get(_GENERATED_RESULT_URL).mock(
        return_value=httpx.Response(200, json=_text_result())
    )

    out = _invoke({"api_key": secret}, tool_ctx)

    for route in (create, poll, result):
        headers = route.calls.last.request.headers
        assert headers["X-Client-Source"] == "omnigent"
        assert headers["Authorization"] == f"Bearer {secret}"
    assert secret not in json.loads(create.calls.last.request.content.decode()).get("input", "")
    assert secret not in out, "the credential must never reach the model-visible envelope"


@respx.mock
def test_generated_run_terminal_failure_keeps_run_id(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """C11: a failed generated run reports the run id and skips /result."""
    respx.post(_GENERIC_RUNS_URL).mock(
        return_value=httpx.Response(
            202,
            json=_run(
                "failed",
                web_search_agent_id=_GENERATED_AGENT_ID,
                error={"ref_id": _RUN_ID, "message": "source unreachable"},
            ),
        )
    )
    result = respx.get(_GENERATED_RESULT_URL).mock(return_value=httpx.Response(200, json={}))
    out = _invoke({"api_key": "test-key"}, tool_ctx)
    assert out == f"Nimble research run {_RUN_ID} failed: source unreachable"
    assert result.call_count == 0, "C12: never fetch a result for a non-completed run"


# ---------------------------------------------------------------------------
# Ambiguous create outcomes: do-not-resubmit guidance
# ---------------------------------------------------------------------------

_NO_RETRY = "Do not retry or resubmit this task automatically"


@respx.mock
@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_ambiguous_create_status_warns_against_resubmitting(
    tool_ctx: ToolContext, status: int
) -> None:
    """A create that reached Nimble and then failed may already be billed.

    408 and 5xx mean the request arrived, so the run can be live even though
    this call reports an error. Resubmitting would pay for the task twice.
    """
    create = respx.post(_RUNS_URL).mock(return_value=httpx.Response(status))
    out = _invoke(_config(), tool_ctx)
    assert create.call_count == 1, "the create itself must still never be retried"
    assert _NO_RETRY in out, out
    assert "may already have been created and billed" in out
    assert "run history" in out, "with no run id, reconciliation goes through account history"


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.WriteError("broken pipe"),
    ],
)
def test_transport_failure_on_create_warns_against_resubmitting(
    tool_ctx: ToolContext, failure: Exception
) -> None:
    """A transport failure or timeout is the ambiguous case: no response, unknown billing."""
    create = respx.post(_RUNS_URL).mock(side_effect=failure)
    out = _invoke(_config(), tool_ctx)
    assert create.call_count == 1
    assert _NO_RETRY in out, out
    assert "run history" in out


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 404, 422, 429])
def test_clear_rejection_does_not_warn_against_resubmitting(
    tool_ctx: ToolContext, status: int
) -> None:
    """A rejected create billed nothing, so it must not carry the warning.

    Attaching it to every failure would train the reader to ignore it.
    """
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(status))
    out = _invoke(_config(), tool_ctx)
    assert _NO_RETRY not in out, (
        f"HTTP {status} created no run; the warning is noise here: {out!r}"
    )


@respx.mock
def test_unusable_run_id_warns_without_a_durable_id(tool_ctx: ToolContext) -> None:
    """A 202 with an unusable run id means the run exists but cannot be named."""
    respx.post(_GENERIC_RUNS_URL).mock(
        return_value=httpx.Response(202, json=_run("queued", id="run_bogus"))
    )
    out = _invoke({"api_key": "test-key"}, tool_ctx)
    assert _NO_RETRY in out, out
    assert "run history" in out


@respx.mock
def test_unusable_returned_agent_id_reconciles_by_run_id(tool_ctx: ToolContext) -> None:
    """When the run id survived, the guidance names it instead of the account history."""
    respx.post(_GENERIC_RUNS_URL).mock(
        return_value=httpx.Response(202, json=_run("queued", web_search_agent_id="../../admin"))
    )
    out = _invoke({"api_key": "test-key"}, tool_ctx)
    assert _NO_RETRY in out, out
    assert f"reconcile run {_RUN_ID}" in out


@respx.mock
def test_owner_mismatch_reconciles_by_run_id(
    tool_ctx: ToolContext, fake_clock: _FakeClock
) -> None:
    """A mismatched run was created and billed; the run id identifies it."""
    respx.post(_RUNS_URL).mock(
        return_value=httpx.Response(
            202, json=_run("completed", web_search_agent_id=_GENERATED_AGENT_ID)
        )
    )
    out = _invoke(_config(), tool_ctx)
    assert _NO_RETRY in out, out
    assert f"reconcile run {_RUN_ID}" in out


@respx.mock
@pytest.mark.parametrize(
    "phase", ["poll_timeout", "poll_http_error", "result_http_error", "result_409_exhausted"]
)
def test_post_create_failures_warn_against_resubmitting(
    tool_ctx: ToolContext, fake_clock: _FakeClock, phase: str
) -> None:
    """Once the run exists it is billed, so no later failure may invite a resubmit.

    The create-time rule and the post-create rule have to agree: telling the
    caller to "retry the task" after a run was created and paid for is how the
    same research gets billed twice.
    """
    respx.post(_RUNS_URL).mock(return_value=httpx.Response(202, json=_run("queued")))
    if phase == "poll_timeout":
        respx.get(_RUN_URL).mock(return_value=httpx.Response(200, json=_run("running")))
    elif phase == "poll_http_error":
        respx.get(_RUN_URL).mock(return_value=httpx.Response(500))
    else:
        respx.get(_RUN_URL).mock(return_value=httpx.Response(200, json=_run("completed")))
        respx.get(_RESULT_URL).mock(
            return_value=httpx.Response(409 if phase == "result_409_exhausted" else 500)
        )

    out = _invoke(
        _config(timeout_seconds="10", poll_interval_seconds="2"),
        tool_ctx,
        args={"task": "x"},
    )

    assert _RUN_ID in out, out
    assert "Do not resubmit this task automatically" in out, out
    assert "reconcile it by run id" in out
    assert "Retry the task" not in out, "a billed run must never be re-submitted to re-read"
