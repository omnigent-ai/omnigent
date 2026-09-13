"""UI journey: the full text of a long sub-agent handoff is reachable in chat.

The user asks an orchestrator to delegate a report to its writer sub-agent;
the writer returns a report longer than the inbox delivery cap. Delivery
into the parent's wake stays bounded — the "Read inbox" tool card shows the
report cut at the runtime's ``...[truncated N chars — read the full text
with sys_session_get_history ...]`` marker — but the marker now names the
retrieval path, and following it works: the parent reads the child session
with ``sys_session_get_history`` and the report's tail (its end marker)
becomes visible in the parent transcript's "Get session history" card.

This driver asserts the fixed rendering and records the after-fix footage;
the durable regression guard is
``tests/e2e/test_subagent_long_handoff_reachable_e2e.py``.

Excluded from default ``pytest`` runs. Invoke with::

    pytest tests/e2e_ui/agents/test_subagent_long_handoff_retrieval.py -v --timeout=600

Record the journey by setting ``OMNIGENT_E2E_RECORD_DIR`` (the test drives
Playwright manually and injects ``record_video_dir`` itself).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, sync_playwright

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_DISPATCH_ACK = "Writer dispatched, waiting for its report"
_RELAY_DONE = "WRITER_REPORT_RELAYED"
_RETRIEVAL_DONE = "FULL_REPORT_RETRIEVED"
_TRUNCATION_HINT = "read the full text with sys_session_get_history"

# Longer than the 12000-char inbox delivery cap, so the wake delivery
# arrives truncated and only the retrieval path can show the tail.
_HANDOFF_LEN = 20000

pytestmark = [pytest.mark.timeout(600, method="signal")]


def _build_handoff(uid: str) -> tuple[str, str]:
    """Return ``(full_report_text, end_marker)`` of exactly ``_HANDOFF_LEN`` chars."""
    begin = f"HANDOFF_BEGIN|{uid}|"
    end = f"|HANDOFF_END|{uid}"
    body_len = _HANDOFF_LEN - len(begin) - len(end)
    text = begin + ("0123456789" * (body_len // 10 + 1))[:body_len] + end
    assert len(text) == _HANDOFF_LEN
    return text, end


def _register_parent_with_writer(
    base_url: str,
    parent_model: str,
    child_model: str,
    mock_base: str,
    runner_id: str,
) -> str:
    """Register the orchestrator + writer sub-agent and bind a session to the runner."""
    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
    from tests.e2e.conftest import lookup_agent_id, register_inline_agent

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        parent_name = register_inline_agent(
            client,
            name=f"handoff-ui-parent-{parent_model[-6:]}",
            harness="openai-agents",
            model=parent_model,
            profile="",
            prompt=(
                "You are an orchestrator. Dispatch the writer sub-agent via "
                "sys_session_send when asked, and read your inbox when woken."
            ),
            mock_llm_base_url=mock_base,
            extra_config={
                "tools": {
                    "writer": {
                        "type": "agent",
                        "description": "Writer sub-agent. Produces the requested report.",
                        "executor": {
                            "harness": "openai-agents",
                            "model": child_model,
                            "auth": {
                                "type": "api_key",
                                "api_key": "mock-key",
                                "base_url": mock_base,
                            },
                        },
                        "prompt": "You are the writer. Return the report.",
                    },
                },
            },
        )
        agent_id = lookup_agent_id(client, parent_name)
        create = client.post(
            "/v1/sessions",
            json={"agent_id": agent_id},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        create.raise_for_status()
        session_id = str(create.json()["id"])
        bind = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})
        bind.raise_for_status()
    return session_id


def _configure_delegation_turns(
    mock_llm_server_url: str,
    parent_model: str,
    child_model: str,
    long_report: str,
) -> None:
    """Load the scripted mock-LLM turns for the delegation + wake phase."""
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "writer",
                                "title": "long-report",
                                "args": "Write the complete report.",
                            }
                        ),
                    }
                ],
            },
            {"text": f"{_DISPATCH_ACK}."},
            {
                "tool_calls": [
                    {"call_id": "call_drain", "name": "sys_read_inbox", "arguments": "{}"}
                ],
            },
            {
                "text": (
                    f"{_RELAY_DONE} — the writer's report arrived in my inbox; "
                    "see the drained result above for everything I received."
                )
            },
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": long_report}],
        key=child_model,
    )


def _configure_retrieval_turn(
    mock_llm_server_url: str,
    parent_model: str,
    child_session_id: str,
) -> None:
    """Load the scripted turn that reads the child's full report back."""
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_full_history",
                        "name": "sys_session_get_history",
                        "arguments": json.dumps(
                            {
                                "conversation_id": child_session_id,
                                "tail_items": 1,
                                "content_max_chars": _HANDOFF_LEN,
                            }
                        ),
                    }
                ],
            },
            {
                "text": (
                    f"{_RETRIEVAL_DONE} — the writer's complete report is in the "
                    "session history result above, end marker included."
                )
            },
        ],
        key=parent_model,
    )


def _child_session_id(base_url: str, parent_id: str) -> str:
    """Return the writer child session spawned under *parent_id*."""
    response = httpx.get(f"{base_url}/v1/sessions/{parent_id}/child_sessions", timeout=30.0)
    response.raise_for_status()
    children = response.json()["data"]
    assert children, f"child session did not appear for parent {parent_id}"
    return str(children[0]["id"])


def _click_closed_triggers(
    page: Page, scope: str, *, has_text: re.Pattern[str] | None = None
) -> int:
    """Click every closed collapsible trigger matching *scope*; return clicks made."""
    triggers = (
        page.locator(scope, has_text=has_text) if has_text is not None else page.locator(scope)
    )
    clicks = 0
    for index in range(triggers.count()):
        trigger = triggers.nth(index)
        if trigger.is_visible() and trigger.get_attribute("data-state") == "closed":
            trigger.click()
            clicks += 1
    return clicks


def _open_tool_card_output(page: Page, card_label: str, *, timeout_s: float = 30) -> None:
    """Open settled "Worked for" folds and *card_label*'s Output panel."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _click_closed_triggers(
            page,
            '[data-testid="turn-worked-fold"] [data-slot="collapsible-trigger"]',
        )
        _click_closed_triggers(
            page,
            '[data-slot="collapsible-trigger"]',
            has_text=re.compile(card_label),
        )
        expand_button = page.get_by_role("button", name="Expand")
        if expand_button.count() > 0:
            for index in range(expand_button.count()):
                button = expand_button.nth(index)
                if button.is_visible():
                    button.click()
            return
        time.sleep(0.4)
    raise AssertionError(f"never reached the {card_label} card's Output panel")


def _run_browser_journey(
    base_url: str,
    session_id: str,
    mock_llm_server_url: str,
    parent_model: str,
    end_marker: str,
) -> dict[str, Any]:
    """Drive the synchronous Playwright journey outside pytest's asyncio loop."""
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    result: dict[str, Any] = {}
    with sync_playwright() as playwright:
        launch_args: list[str] = []
        if os.environ.get("OMNIGENT_PW_NO_SANDBOX"):
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        browser = playwright.chromium.launch(headless=True, args=launch_args)
        context_kwargs: dict[str, object] = {"viewport": {"width": 1280, "height": 720}}
        if record_dir:
            Path(record_dir).mkdir(parents=True, exist_ok=True)
            context_kwargs["record_video_dir"] = record_dir
        context = browser.new_context(**context_kwargs)
        try:
            page = context.new_page()
            page.goto(f"{base_url}/c/{session_id}")

            composer = page.get_by_placeholder("Send a message…")
            composer.wait_for(state="visible", timeout=30_000)
            composer.fill(
                "Delegate the full report to the writer sub-agent, then relay "
                "everything it returns."
            )
            page.get_by_role("button", name="Send", exact=True).click()

            # Dispatch ack, then the auto-wake turn's wrap-up after the
            # writer completes and the parent drains its inbox.
            page.locator(_ASSISTANT, has_text=_DISPATCH_ACK).first.wait_for(
                state="visible", timeout=120_000
            )
            page.locator(_ASSISTANT, has_text=_RELAY_DONE).first.wait_for(
                state="visible", timeout=240_000
            )

            # The delivered handoff is still cut at the delivery cap, but its
            # marker now points the reader at the retrieval path.
            _open_tool_card_output(page, "Read inbox")
            hint = page.get_by_text(_TRUNCATION_HINT)
            hint.first.wait_for(state="visible", timeout=15_000)
            hint.first.scroll_into_view_if_needed()
            result["delivery_hint"] = hint.first.inner_text()
            result["end_marker_hits_after_delivery"] = page.get_by_text(end_marker).count()
            time.sleep(1)

            # Follow the marker: the next turn reads the child session with
            # sys_session_get_history at the report's full length.
            child_id = _child_session_id(base_url, session_id)
            _configure_retrieval_turn(mock_llm_server_url, parent_model, child_id)
            composer.fill("Now read the writer's complete report from its session.")
            page.get_by_role("button", name="Send", exact=True).click()
            page.locator(_ASSISTANT, has_text=_RETRIEVAL_DONE).first.wait_for(
                state="visible", timeout=120_000
            )

            # The report's tail is now on the page: the "Get session history"
            # card's output ends with the end marker. Hold it on screen so
            # the recording ends on the outcome.
            _open_tool_card_output(page, "Get session history")
            marker = page.get_by_text(end_marker)
            marker.first.wait_for(state="visible", timeout=15_000)
            marker.first.scroll_into_view_if_needed()
            result["end_marker_hits_after_retrieval"] = marker.count()
            time.sleep(3)
        finally:
            context.close()
            browser.close()
    return result


def test_long_subagent_handoff_tail_reachable_in_parent_chat(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The parent transcript can show the full >12k handoff via retrieval.

    Journey (all in the browser): ask the orchestrator to delegate the
    report → dispatch ack → the writer finishes with a 20000-char report →
    the parent auto-wakes and drains its inbox → the drained result is cut
    at the delivery cap but names the retrieval path → the next turn reads
    the child session with ``sys_session_get_history`` → the report's end
    marker is visible in the parent transcript.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-hoff-parent-{uid}"
    child_model = f"mock-hoff-child-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"
    long_report, end_marker = _build_handoff(uid)

    _configure_delegation_turns(mock_llm_server_url, parent_model, child_model, long_report)
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _register_parent_with_writer(
        live_server, parent_model, child_model, mock_base, runner_id
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(
                _run_browser_journey,
                live_server,
                session_id,
                mock_llm_server_url,
                parent_model,
                end_marker,
            ).result()
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)

    # Delivery stays bounded: the tail is not in the drained inbox text,
    # and the marker names the retrieval path instead of dead-ending.
    assert _TRUNCATION_HINT in result["delivery_hint"], result
    assert result["end_marker_hits_after_delivery"] == 0, (
        f"the >12k report unexpectedly arrived whole in the inbox drain: {result!r}"
    )
    # The user-visible fix: following the marker surfaces the complete
    # report — its end marker is on the parent session page.
    assert result["end_marker_hits_after_retrieval"] > 0, result
