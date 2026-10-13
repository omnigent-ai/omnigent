"""A Claude Code sub-agent that runs on another model gets its own row in the cost breakdown.

A real Claude CLI on sonnet runs a scripted Agent/Task spawn pinned to opus; the mock
serves both models and echoes the requested model id. The session's Token usage
breakdown must list the opus model instead of folding its spend into sonnet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

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
from tests.helpers.ui_configuration import _CLAUDE_MOCK_MODEL

_log = logging.getLogger(__name__)

# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# Mock LLM responds instantly; budget covers CLI boot + the spawn round-trip.
_MOCK_TURN_TIMEOUT_MS = 90_000
# The forwarder posts Claude Code's cumulative statusLine cost a few polls after
# the turn settles in the UI.
_COST_SETTLE_TIMEOUT_S = 90.0
# Grace for a build that publishes the sub-agent model's bucket asynchronously.
_BREAKDOWN_SETTLE_TIMEOUT_S = 30.0
# Optional directory for screenshots and API snapshots kept as evidence.
_EVIDENCE_DIR_ENV = "OMNIGENT_E2E_EVIDENCE_DIR"


def _session_snapshot(base_url: str, session_id: str) -> dict:
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
    """Poll until the asynchronously forwarded total_cost_usd exceeds *above*."""
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
        "settle (harness/timing issue, not the attribution bug this test guards). "
        f"Last snapshot: total_cost_usd={last.get('total_cost_usd')} "
        f"usage_by_model={json.dumps(last.get('usage_by_model'))}"
    )


def _captured_requests(mock_llm_server_url: str) -> list[dict]:
    resp = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return [req for req in resp.json()["requests"] if isinstance(req, dict)]


def _spawn_tool_name(mock_llm_server_url: str) -> str:
    """Return the spawn tool the CLI advertises (``Task`` became ``Agent`` in 2.1.63)."""
    for req in reversed(_captured_requests(mock_llm_server_url)):
        tools = req.get("tools")
        if isinstance(tools, list):
            names = {t.get("name") for t in tools if isinstance(t, dict)}
            for candidate in ("Task", "Agent"):
                if candidate in names:
                    return candidate
    pytest.fail(
        "no captured request advertised a Task/Agent spawn tool — the CLI never "
        "made a tool-bearing request (a harness problem, not the attribution bug)"
    )


def _models_asked_with_user_marker(mock_llm_server_url: str, marker: str) -> list[str]:
    """Models of captured requests whose *user* message carries *marker*.

    Assistant tool_use blocks repeat the child prompt in the parent history and
    would otherwise count the parent as a child request.
    """
    models: list[str] = []
    for req in _captured_requests(mock_llm_server_url):
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


def _open_usage_breakdown(page: Page) -> Locator:
    """Open the agent-info popover's Token usage section via the keyboard (no hover timer)."""
    trigger = page.get_by_test_id("agent-info-trigger")
    expect(trigger).to_be_visible(timeout=60_000)
    trigger.focus()
    trigger.press("Enter")
    expect(page.get_by_test_id("agent-info-panel")).to_be_visible(timeout=15_000)
    breakdown = page.get_by_test_id("agent-info-usage-by-model")
    expect(breakdown).to_be_visible(timeout=15_000)
    breakdown.locator("summary").click()
    return breakdown


def _save_evidence(name: str, payload: dict | list, page: Page | None = None) -> None:
    evidence_dir = os.environ.get(_EVIDENCE_DIR_ENV)
    if not evidence_dir:
        return
    target = Path(evidence_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.json").write_text(json.dumps(payload, indent=1))
    if page is not None:
        page.screenshot(path=str(target / f"{name}.png"))


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_subagent_on_other_model_has_its_own_cost_row(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A real Claude spawn pinned to opus must add an opus row to the Token usage breakdown."""
    base_url, session_id = native_claude_mock_session
    nonce = uuid.uuid4().hex[:8]
    baseline_token = f"base-reply-{nonce}"
    fanout_text = f"fan out one sub-agent {nonce}"
    # Longer than the parent's match token so longest-match routing always hands
    # the sub-agent's own request to the sub-agent queue.
    subtask_marker = f"subtask-prompt-{nonce}-unique-cross-model-routing-token"
    subagent_reply = f"subagent-done-{nonce}"
    final_token = f"fanout-final-{nonce}"

    reset_mock_llm(mock_llm_server_url)
    # Fallbacks serve Claude's background requests and the baseline turn, so the
    # queues configured below stay reserved for the scripted fan-out.
    set_fallback_mock_llm(mock_llm_server_url, "default", baseline_token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, baseline_token)

    # The recorded page is created after the session exists so the clip starts
    # on the user journey rather than on fixture setup.
    page: Page = request.getfixturevalue("page")
    try:
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
            timeout=_TERMINAL_READY_TIMEOUT_MS
        )
        _ensure_chat_view(page)

        # Turn 1: baseline on the launch model so the fan-out turn's cost delta
        # lands on a settled baseline.
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
        _save_evidence("baseline-snapshot", baseline)

        spawn_tool = _spawn_tool_name(mock_llm_server_url)
        _log.info("CLI advertises spawn tool: %s", spawn_tool)

        # Turn 2: the orchestrator's reply is a real spawn tool_use pinned to the
        # opus family; only requests advertising the spawn tool (the parent's own
        # turns, not Claude's background requests) can consume this queue.
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
                *([{"text": final_token}] * 5),
            ],
            match=fanout_text,
            required_tools=[spawn_tool],
        )
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": subagent_reply}] * 3,
            match=subtask_marker,
        )

        _send(page, fanout_text)
        expect(page.locator(_ASSISTANT, has_text=final_token).first).to_be_visible(
            timeout=_MOCK_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

        # Journey precondition, distinct from the attribution assertion: the
        # sub-agent genuinely ran on an opus model.
        subagent_models = _models_asked_with_user_marker(mock_llm_server_url, subtask_marker)
        _save_evidence("mock-requests", _captured_requests(mock_llm_server_url))
        assert subagent_models, (
            "the scripted sub-agent never called the LLM — the fan-out did not run, "
            "so the journey could not be driven (harness issue, not the bug)"
        )
        opus_models = {m for m in subagent_models if "opus" in m}
        assert opus_models, (
            f"the sub-agent ran on {subagent_models} instead of an opus model — the "
            "fan-out was not cross-model, so the journey could not be driven "
            "(harness issue, not the bug)"
        )
        _log.info("sub-agent ran on: %s", sorted(opus_models))

        settled = _wait_for_priced_cost(
            base_url, session_id, above=baseline_cost, timeout_s=_COST_SETTLE_TIMEOUT_S
        )
        _log.info(
            "fan-out cost settled: %s by_model=%s",
            settled.get("total_cost_usd"),
            settled.get("usage_by_model"),
        )
        deadline = time.monotonic() + _BREAKDOWN_SETTLE_TIMEOUT_S
        by_model: dict[str, object] = {}
        while time.monotonic() < deadline:
            by_model = _session_snapshot(base_url, session_id).get("usage_by_model") or {}
            if any("opus" in model for model in by_model):
                break
            time.sleep(0.5)
        assert any("opus" in model for model in by_model), (
            "the API never attributed the sub-agent's opus spend within "
            f"{_BREAKDOWN_SETTLE_TIMEOUT_S}s (usage_by_model: {json.dumps(by_model)})"
        )

        # The user-facing surface: reload so the popover seeds from the snapshot,
        # then open the per-model Token usage breakdown.
        page.reload()
        _open_usage_breakdown(page)
        model_rows = page.locator('[data-testid^="agent-info-model-"]')
        expect(model_rows.first).to_be_visible(timeout=15_000)

        final = _session_snapshot(base_url, session_id)
        api_by_model = final.get("usage_by_model") or {}
        _save_evidence("final-snapshot", final, page)
        # Don't pin the count: the harness may report more than one opus variant.
        opus_rows = model_rows.filter(has_text=re.compile("opus"))
        expect(
            opus_rows.first,
            (
                f"the sub-agent ran on an opus model ({sorted(opus_models)}), but the "
                "session cost panel's per-model breakdown has no opus row — its spend "
                "was folded into the orchestrator's model bucket "
                f"(API usage_by_model: {json.dumps(api_by_model)})"
            ),
        ).to_be_visible(timeout=5_000)
        per_model_cost = sum(
            float(bucket.get("total_cost_usd") or 0.0) for bucket in api_by_model.values()
        )
        assert per_model_cost == pytest.approx(float(final["total_cost_usd"]), abs=1e-6), (
            f"per-model costs {per_model_cost} do not sum to the session cost "
            f"{final['total_cost_usd']} (usage_by_model: {json.dumps(api_by_model)})"
        )
    finally:
        page.context.close()
