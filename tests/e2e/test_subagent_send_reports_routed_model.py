"""Regression e2e: fan-out send results must expose each child's routed model.

**Bug.** An orchestrator with smart routing fans out several sub-agents in one
turn. Routing places the workers on *different* models (e.g.
Opus / Opus / Sonnet), but when the user later asks which model each worker
ran on, the orchestrator answers "all Sonnet" — it reports its *own* model for
the whole fan-out because it was never told the per-sub-agent routed model.

**Root cause (the reproducible contract gap).** The user-visible misreport is a
downstream, non-deterministic LLM symptom. The deterministic defect underneath
it is a tool-result contract gap: the ``sys_session_send`` launching handle the
runner returns to the orchestrator carries the child's ``agent`` / ``title`` /
``status`` but **not** the model the child was routed onto — even though the
runner has just persisted that exact value as the child row's
``model_override``. So the orchestrator is structurally blind to per-sub-agent
routing at fan-out time. (Contrast ``sys_session_get_info``, which already
exposes ``"model"`` from the same session row — the launching handle simply
never mirrored it.)

**What this test drives.** The real fan-out user journey, faithfully: an
``openai-agents`` orchestrator emits three ``sys_session_send`` tool calls in a
single turn, each dispatching a worker with a distinct ``args.model``
(Opus / Opus / Sonnet). The runner validates, provider-localizes, and persists
a ``model_override`` on each child row (the precondition — routing genuinely
landed the children on distinct models), then returns a launching handle to
the orchestrator for each.

**Assertions.**
1. *Precondition* — the three child sessions persist the three routed models
   (compared modulo mechanical gateway-prefix localization, which may strip or
   add a ``databricks-`` prefix depending on the child's resolved provider),
   so there really is distinct routing to report.
2. *Defect* — each ``sys_session_send`` result the orchestrator received must
   expose the child's routed ``model``, verbatim equal to that child's
   persisted ``model_override``. On the buggy build the handle omits ``model``
   entirely, so this fails: the exact blindness that lets the orchestrator
   misreport "all Sonnet".

Mock-LLM keyed queues drive the orchestrator and workers so the whole chain —
mock tool_calls -> ``sys_session_send`` args -> runner validation ->
``POST /v1/sessions`` ``model_override`` -> persisted child row -> launching
handle returned to the orchestrator — runs without real credentials or native
CLI binaries.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_subagent_send_reports_routed_model.py -v
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)

# Three workers routed onto distinct models — the Opus/Opus/Sonnet fan-out.
# ``openai-agents`` is a multi-model harness, so all three ids pass the
# per-dispatch family guard. Two workers deliberately share a model so the
# test proves the orchestrator needs a per-child signal, not a single
# session-wide model.
_WORKER_MODELS: dict[str, str] = {
    "worker_alpha": "databricks-claude-opus-4-8",
    "worker_beta": "databricks-claude-opus-4-8",
    "worker_gamma": "databricks-claude-sonnet-4-6",
}

# These tests use per-sub-agent mock-LLM routing (each child on its own
# ``auth.base_url``), which servers < 0.3.0 do not propagate; gate accordingly.
pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.min_server_version("0.3.0"),
]


def _canonical(model_id: str) -> str:
    """Return the model id modulo mechanical gateway localization.

    The runner localizes a per-dispatch id for the child's resolved
    provider before persisting it as ``model_override`` — a
    ``databricks-`` prefix is stripped for a vendor-direct child and
    added for a gateway-routed one. The routing *identity* is the bare
    canonical id either way.

    :param model_id: A requested or persisted model id.
    :returns: The id with any ``databricks-`` gateway prefix removed.
    """
    return model_id.removeprefix("databricks-")


@pytest.fixture(scope="session")
def orchestrator_agent(
    http_client: httpx.Client,
    mock_llm_server_url: str,
) -> tuple[str, str]:
    """Register an orchestrator with three inline worker sub-agents.

    Every executor (orchestrator + workers) points at the mock server via
    ``auth.base_url`` so neither the parent nor the children ever reach a real
    LLM API. The workers' spec models are placeholders — each dispatch
    overrides them through ``sys_session_send``'s ``args.model``.

    :param http_client: HTTP client pointed at the live server.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: Tuple ``(orchestrator_name, orchestrator_model)``.
    """
    uid = uuid.uuid4().hex[:6]
    orch_model = f"mock-orch-{uid}"
    # openai-agents harness expects /v1 in the base URL.
    mock_base = f"{mock_llm_server_url}/v1"

    worker_tools: dict[str, Any] = {}
    for worker_name in _WORKER_MODELS:
        worker_tools[worker_name] = {
            "type": "agent",
            "description": f"Test-fixture {worker_name}.",
            "executor": {
                "harness": "openai-agents",
                # Spec default; each dispatch overrides it via args.model.
                "model": f"{worker_name}-spec-{uid}",
                "auth": {
                    "type": "api_key",
                    "api_key": "mock-key",
                    "base_url": mock_base,
                },
            },
            "prompt": (f"You are {worker_name}. Reply with a one-line status line."),
        }

    orch_name = register_inline_agent(
        http_client,
        name=f"orch-{uid}",
        harness="openai-agents",
        model=orch_model,
        profile="",
        prompt=(
            "You are a routing orchestrator. Fan out the worker sub-agents via "
            "sys_session_send, then report which model each worker ran on."
        ),
        mock_llm_base_url=mock_base,
        extra_config={"tools": worker_tools},
    )
    return orch_name, orch_model


def _flat_session_items(
    client: httpx.Client,
    session_id: str,
) -> list[dict[str, Any]]:
    """Return session items flattened to the Responses-style shape.

    ``GET /v1/sessions/{id}`` nests type-specific fields (``name``,
    ``call_id``, ``arguments``, ``output``) under ``data``; lift them to the
    top level while keeping the item ``type`` so callers can correlate
    ``function_call`` and ``function_call_output`` items by ``call_id``.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    :returns: Flattened conversation items.
    """
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    flat: list[dict[str, Any]] = []
    for item in resp.json().get("items", []):
        data = item.get("data") or {}
        flat.append({"type": item.get("type"), **data})
    return flat


def test_send_result_exposes_routed_subagent_model(
    http_client: httpx.Client,
    live_runner_id: str,
    orchestrator_agent: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A fan-out's ``sys_session_send`` results must carry each routed model.

    Drives the misreported-fan-out journey: one orchestrator turn dispatches
    three workers on distinct models (Opus/Opus/Sonnet). Asserts the children
    really persist those distinct ``model_override``s (precondition), then
    that every ``sys_session_send`` handle returned to the orchestrator
    exposes the child's routed ``model`` matching its ``model_override``. On
    the buggy build the handle omits ``model``, so the second assertion fails
    — the contract gap that makes the orchestrator misreport "all Sonnet".
    """
    orch_name, orch_model = orchestrator_agent
    reset_mock_llm(mock_llm_server_url)

    # Orchestrator brain: turn 1 emits all three sys_session_send tool calls
    # (one per worker, each with its routed args.model); turn 2 acks once the
    # three tool results arrive.
    dispatch_calls: list[dict[str, Any]] = []
    for index, (worker_name, model) in enumerate(_WORKER_MODELS.items(), start=1):
        dispatch_calls.append(
            {
                "call_id": f"call_{worker_name}",
                "name": "sys_session_send",
                "arguments": json.dumps(
                    {
                        "agent": worker_name,
                        "title": f"task-{index}",
                        "args": {
                            "model": model,
                            "input": f"Do subtask {index} and report status.",
                        },
                    }
                ),
            }
        )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": dispatch_calls},
            {"text": "Dispatched all three workers."},
        ],
        key=orch_model,
    )
    # Fallbacks keep the journey stable regardless of async timing: any
    # orchestrator continuation (auto-wake) and any worker turn resolve to a
    # valid text response instead of erroring on an empty queue. Workers run
    # on their persisted model_override — which provider localization may have
    # rewritten from the requested id — so register both spellings.
    set_fallback_mock_llm(mock_llm_server_url, orch_model, "Workers still running.")
    worker_mock_keys = set(_WORKER_MODELS.values()) | {
        _canonical(model) for model in _WORKER_MODELS.values()
    }
    for model in worker_mock_keys:
        set_fallback_mock_llm(mock_llm_server_url, model, f"{model} worker done.")

    session_id = create_runner_bound_session(
        http_client,
        agent_name=orch_name,
        runner_id=live_runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Fan out all three workers on their routed models, then tell me "
            "which model each one ran on."
        ),
    )
    poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )

    # ── Precondition: routing landed the three children on distinct models. ──
    kids_resp = http_client.get(f"/v1/sessions/{session_id}/child_sessions")
    kids_resp.raise_for_status()
    kids = kids_resp.json().get("data", [])
    assert len(kids) == 3, (
        f"expected 3 dispatched child sessions, got {[k.get('tool') for k in kids]}"
    )
    child_models: dict[str, str] = {}
    for kid in kids:
        child_id = kid.get("session_id") or kid.get("id")
        snap = http_client.get(f"/v1/sessions/{child_id}")
        snap.raise_for_status()
        override = snap.json().get("model_override")
        assert isinstance(override, str) and override, (
            f"child {kid.get('tool')!r} persisted no model_override; "
            "routing never landed a per-child model"
        )
        child_models[str(kid.get("tool"))] = override
    # Compare modulo mechanical gateway-prefix localization: the persisted
    # override may be the vendor-direct spelling of the requested id.
    assert {name: _canonical(mid) for name, mid in child_models.items()} == {
        name: _canonical(mid) for name, mid in _WORKER_MODELS.items()
    }, (
        "precondition failed: children were not routed onto distinct models\n"
        f"  requested {_WORKER_MODELS}\n  persisted {child_models}"
    )

    # ── Defect: every sys_session_send RESULT the orchestrator received must ──
    #    expose the child's routed model (matching its persisted model_override).
    items = _flat_session_items(http_client, session_id)
    send_calls = {
        item["call_id"]: item
        for item in items
        if item.get("type") == "function_call"
        and item.get("name") == "sys_session_send"
        and item.get("call_id")
    }
    send_results = [
        item
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") in send_calls
    ]
    assert len(send_results) == 3, (
        f"expected 3 sys_session_send tool results, got {len(send_results)}: "
        f"{[r.get('call_id') for r in send_results]}"
    )

    for result in send_results:
        payload = json.loads(str(result.get("output", "")))
        agent = payload.get("agent")
        # The handle is otherwise informative — it names the sub-agent and its
        # launching status. Only the routed model is missing.
        assert agent in _WORKER_MODELS, (
            f"sys_session_send result named an unexpected sub-agent: {payload!r}"
        )
        assert payload.get("status") == "launching", (
            f"sys_session_send result had unexpected status: {payload!r}"
        )
        assert "model" in payload, (
            f"sys_session_send result for {agent!r} exposed NO routed model to "
            f"the orchestrator: {payload!r}. The orchestrator cannot report "
            f"which model each sub-agent ran on, so it defaults to its own "
            f"model and misreports the fan-out (e.g. 'all Sonnet')."
        )
        assert payload["model"] == child_models[agent], (
            f"sys_session_send result for {agent!r} reported model "
            f"{payload['model']!r}, but the child persisted model_override "
            f"{child_models[agent]!r}."
        )
