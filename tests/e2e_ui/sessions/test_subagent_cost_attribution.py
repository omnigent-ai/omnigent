"""E2E: a Task sub-agent routed to a different model must appear in the
session cost panel's per-model breakdown.

The journey: a claude-native (Claude Code) orchestrator session runs on one
model (sonnet). Its turn fans out a Task/Agent sub-agent pinned to a DIFFERENT
model (opus). Claude Code's own cumulative cost (the statusLine
``total_cost_usd``, "S") includes the sub-agent's spend once it returns, and
the native forwarder posts that S tagged with the statusLine's ACTIVE model —
the orchestrator's. The server's ``_persist_native_cumulative_usage`` then
attributes the whole delta (sub-agent spend included) to that one model
bucket, so the cost panel's per-model breakdown shows 100% on the
orchestrator's model and no entry for the model the sub-agent actually ran on.

This test drives the real user path: a live ``claude`` CLI in the session
terminal against the mock LLM, a scripted fan-out turn whose API response is a
real Task/Agent ``tool_use`` with ``model: "opus"``, and the SPA's agent-info
popover as the surface the user reads the per-model breakdown on. The final
assertion — an opus row in the panel's Token-usage breakdown — is the one this
bug breaks.

Runs only in mock-LLM mode (``LLM_API_KEY`` unset): the fan-out turn must be
scripted for the journey to be deterministic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)

_log = logging.getLogger(__name__)

# Must match the model in the mock anthropic provider config written by the
# native_claude_mock_session fixture (conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# Mock LLM responds instantly; budget covers CLI boot + the Task round-trip.
_MOCK_TURN_TIMEOUT_MS = 90_000
# The forwarder polls the statusLine snapshot and posts cost best-effort, so
# the priced cost can land a few polls after the turn settles in the UI.
_COST_SETTLE_TIMEOUT_S = 90.0
# Bounded grace for a FIXED build to publish the sub-agent model's bucket
# before the failing assertion runs (buggy builds never publish it).
_BREAKDOWN_SETTLE_TIMEOUT_S = 30.0


def _session_snapshot(base_url: str, session_id: str) -> dict:
    """Fetch the session snapshot the web UI's cost panel is seeded from.

    :param base_url: Spawned server base URL.
    :param session_id: Parent session id.
    :returns: The ``SessionResponse`` JSON dict.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}",
        params={"include_items": "false"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def _wait_for_priced_cost(
    base_url: str, session_id: str, *, above: float, timeout_s: float
) -> dict:
    """Poll the snapshot until ``total_cost_usd`` exceeds *above*.

    The claude-native forwarder posts Claude Code's cumulative statusLine cost
    (S) asynchronously, so the turn settling in the chat surface does not mean
    the priced cost has landed yet.

    :param base_url: Spawned server base URL.
    :param session_id: Parent session id.
    :param above: Strictly-below threshold the priced cost must pass.
    :param timeout_s: Poll budget in seconds.
    :returns: The first snapshot whose priced cost exceeds *above*.
    """
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = _session_snapshot(base_url, session_id)
        cost = last.get("total_cost_usd")
        if isinstance(cost, (int, float)) and cost > above:
            return last
        time.sleep(2.0)
    pytest.fail(
        "Claude Code's cumulative statusLine cost never advanced past "
        f"{above} within {timeout_s:.0f}s — the cost-forwarding lane did not "
        "settle (harness/timing issue, NOT the attribution bug this test "
        f"guards). Last snapshot cost fields: total_cost_usd="
        f"{last.get('total_cost_usd')} usage_by_model="
        f"{json.dumps(last.get('usage_by_model'))}"
    )


def _spawn_tool_name(mock_llm_server_url: str) -> str:
    """Return the sub-agent spawn tool name this Claude CLI advertises.

    ``Task`` was renamed to ``Agent`` in CLI 2.1.63 (both stay callable), and
    the scripted ``tool_use`` must quote the name the CLI actually sent in its
    request's ``tools`` list, so read it from the captured baseline request.

    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: ``"Task"`` or ``"Agent"``.
    """
    resp = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    for req in reversed(resp.json()["requests"]):
        tools = req.get("tools") if isinstance(req, dict) else None
        if isinstance(tools, list):
            names = {t.get("name") for t in tools if isinstance(t, dict)}
            for candidate in ("Task", "Agent"):
                if candidate in names:
                    return candidate
    return "Task"


def _models_asked_with_user_marker(mock_llm_server_url: str, marker: str) -> list[str]:
    """Models of captured LLM requests whose USER-role content carries *marker*.

    Scoped to user-role messages so the parent's follow-up request (whose
    assistant-role ``tool_use`` block also embeds the sub-agent prompt) does
    not count — only the sub-agent's own request has the marker as its user
    message.

    :param mock_llm_server_url: Mock LLM server base URL.
    :param marker: Unique substring planted in the Task prompt.
    :returns: The ``model`` field of each matching request, in arrival order.
    """
    resp = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    models: list[str] = []
    for req in resp.json()["requests"]:
        if not isinstance(req, dict):
            continue
        messages = req.get("messages") or []
        if isinstance(messages, dict):
            messages = [messages]
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("role") == "user"
                and marker in json.dumps(message.get("content", ""))
            ):
                models.append(str(req.get("model", "")))
                break
    return models


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_task_subagent_on_other_model_appears_in_cost_breakdown(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A Task sub-agent on opus must surface as its own per-model cost bucket.

    Journey: baseline turn on the launch model (sonnet) → fan-out turn whose
    scripted response spawns a Task/Agent sub-agent with ``model: "opus"`` →
    the sub-agent runs on a real opus model against the mock → the turn
    settles and Claude's cumulative cost (sub-agent spend included) reaches
    the server → the user opens the agent-info popover's Token-usage
    breakdown. The buggy build shows ONLY the sonnet bucket: the opus
    sub-agent's spend was folded into it, so no opus row ever appears.
    """
    if os.environ.get("LLM_API_KEY"):
        pytest.skip("deterministic scripted fan-out needs the mock LLM (unset LLM_API_KEY)")

    base_url, session_id = native_claude_mock_session
    nonce = uuid.uuid4().hex[:8]
    baseline_token = f"base-reply-{nonce}"
    # Longer than the parent queue's match token (see below) so the mock's
    # longest-match content routing always hands the sub-agent's own request
    # to the sub-agent queue.
    subtask_marker = f"subtask-prompt-{nonce}-unique-cross-model-routing-token"
    subagent_reply = f"subagent-done-{nonce}"
    final_token = f"fanout-final-{nonce}"

    page.goto(f"{base_url}/c/{session_id}")
    # Claude Code must be up (terminal-first session) before composer turns run.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _ensure_chat_view(page)

    reset_mock_llm(mock_llm_server_url)
    # Fallbacks serve Claude's private background requests AND the baseline
    # turn, so queues below stay reserved for the scripted fan-out.
    set_fallback_mock_llm(mock_llm_server_url, "default", baseline_token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, baseline_token)

    # --- Turn 1: baseline on the launch model, so the fan-out turn's cost
    # delta lands on an already-settled baseline. ---
    _send(page, f"baseline turn {nonce}")
    expect(page.locator(_ASSISTANT, has_text=baseline_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    baseline = _wait_for_priced_cost(
        base_url, session_id, above=0.0, timeout_s=_COST_SETTLE_TIMEOUT_S
    )
    baseline_cost = float(baseline["total_cost_usd"])
    _log.info(
        "baseline cost settled: %s by_model=%s", baseline_cost, baseline.get("usage_by_model")
    )

    spawn_tool = _spawn_tool_name(mock_llm_server_url)
    _log.info("CLI advertises spawn tool: %s", spawn_tool)

    # --- Turn 2: scripted fan-out. The orchestrator's next response is a real
    # spawn tool_use pinned to the OTHER model family; the sub-agent's own
    # request (routed by its unique prompt marker) gets a text reply; the
    # orchestrator's post-Task continuations drain the queued final texts. ---
    fanout_text = f"fan out one sub-agent {nonce}"
    # Content-routed on the Agent-tool system reminder, which ONLY the
    # parent's real conversation-turn requests carry. Claude's background
    # requests (title generation etc.) replay the user's message text, so
    # matching on the message would let them steal the scripted tool_use;
    # they never carry this reminder. The first matching request drains the
    # tool_use; the parent's post-Task continuations drain the finals.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "name": spawn_tool,
                        "arguments": json.dumps(
                            {
                                "description": "Cost attribution probe",
                                "prompt": f"{subtask_marker} reply with one word: done",
                                "subagent_type": "general-purpose",
                                "model": "opus",
                            }
                        ),
                    }
                ],
            },
            # Post-Task continuations (extra copies absorb a stray
            # background request that also embeds the conversation text).
            {"text": final_token},
            {"text": final_token},
            {"text": final_token},
            {"text": final_token},
            {"text": final_token},
        ],
        key="fanout-parent",
        match="Available agent types for the Agent tool",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": subagent_reply}, {"text": subagent_reply}, {"text": subagent_reply}],
        key="opus-subagent",
        match=subtask_marker,
    )

    _send(page, fanout_text)
    expect(page.locator(_ASSISTANT, has_text=final_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    # The sub-agent genuinely ran on a different (opus) model — the journey's
    # precondition, distinct from the attribution assertion below.
    subagent_models = _models_asked_with_user_marker(mock_llm_server_url, subtask_marker)
    assert subagent_models, (
        "the scripted Task sub-agent never called the LLM — the fan-out did "
        "not run, so the journey could not be driven (harness issue, not the bug)"
    )
    opus_models = {m for m in subagent_models if "opus" in m}
    assert opus_models, (
        f"the Task sub-agent ran on {subagent_models} instead of an opus "
        "model — the fan-out was not cross-model, so the journey could not "
        "be driven (harness issue, not the bug)"
    )
    _log.info("sub-agent ran on: %s", sorted(opus_models))

    # Wait for the fan-out turn's cost (sub-agent spend included, once Claude
    # settles S at the turn boundary) to reach the server.
    settled = _wait_for_priced_cost(
        base_url, session_id, above=baseline_cost, timeout_s=_COST_SETTLE_TIMEOUT_S
    )
    _log.info(
        "fan-out cost settled: %s by_model=%s",
        settled.get("total_cost_usd"),
        settled.get("usage_by_model"),
    )

    # Bounded grace so a FIXED build (which attributes the sub-agent's spend
    # to its own model, directly or via the child-session rollup) publishes
    # the opus bucket before the assertion; a buggy build never will.
    deadline = time.monotonic() + _BREAKDOWN_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        by_model = _session_snapshot(base_url, session_id).get("usage_by_model") or {}
        if any("opus" in model for model in by_model):
            break
        time.sleep(2.0)

    # --- The user-facing surface: the agent-info popover's per-model
    # Token-usage breakdown. Reload so the popover seeds from the snapshot. ---
    page.reload()
    trigger = page.get_by_test_id("agent-info-trigger")
    expect(trigger).to_be_visible(timeout=60_000)
    trigger.click()
    expect(page.get_by_test_id("agent-info-panel")).to_be_visible(timeout=15_000)
    breakdown = page.get_by_test_id("agent-info-usage-by-model")
    expect(breakdown).to_be_visible(timeout=15_000)
    breakdown.locator("summary").click()

    model_rows = page.locator('[data-testid^="agent-info-model-"]')
    # Sanity: the breakdown renders at least the orchestrator's own bucket.
    expect(model_rows.first).to_be_visible(timeout=15_000)

    # THE BUG: the sub-agent ran on opus, but its spend was attributed to the
    # orchestrator's model, so the breakdown has no opus row at all.
    api_by_model = _session_snapshot(base_url, session_id).get("usage_by_model") or {}
    expect(
        model_rows.filter(has_text=re.compile("opus")),
        (
            "the Task sub-agent ran on an opus model "
            f"({sorted(opus_models)}), but the session cost panel's "
            "per-model breakdown has no opus entry — its spend was folded "
            "into the orchestrator's model bucket "
            f"(API usage_by_model: {json.dumps(api_by_model)})"
        ),
    ).to_have_count(1, timeout=5_000)
