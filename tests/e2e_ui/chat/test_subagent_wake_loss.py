"""UI journey: a sub-agent completion must reach the parent chat after a server blip.

The user asks their orchestrator agent to dispatch a researcher sub-agent and
then waits, hands-off, in the parent session's chat. While the researcher is
working, the server restarts briefly (a routine redeploy / tunnel blip). The
researcher finishes during the outage: the runner delivers its result to the
parent's inbox locally, but the wake-notice POST — the sole signal that makes
the idle parent take a continuation turn and surface the result — fails its
bounded retries against the dead server and is never re-attempted.

What the user then sees in the SPA is this bug: the parent chat sits on
"researcher dispatched, waiting…" forever. Only when the user manually bumps
the agent ("Status update on subagents.") does the parent read its inbox and
reveal the long-finished result.

This test drives that journey in the real browser against a dedicated
restartable server+runner stack and asserts the completion auto-surfaces with
NO user bump. On a buggy build the assertion fails — after first demonstrating
the manual-bump workaround on screen, proving the result had been sitting
deliverable in the inbox the whole time.

Excluded from default ``pytest`` runs. Invoke with::

    pytest tests/e2e_ui/chat/test_subagent_wake_loss.py -v --timeout=600

Record the journey by setting ``OMNIGENT_E2E_RECORD_DIR`` (the test drives
Playwright manually and injects ``record_video_dir`` itself).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

from tests.e2e.test_subagent_wake_loss_e2e import (
    _wait_for_log_line,
    _WakeLossStack,
)
from tests.e2e_ui.conftest import reset_mock_llm

_DISPATCH_ACK = "researcher dispatched, waiting for its result"
_INBOX_REVEAL = "found the researcher's finished result stranded in my inbox"
_WAKE_FAILED_LOG = "Sub-agent wake POST failed"
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'

pytestmark = [pytest.mark.timeout(600, method="signal")]


@pytest.fixture
def wake_loss_stack(
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[_WakeLossStack]:
    """Yield a restartable server+runner stack wired to the mock LLM.

    A dedicated stack (not the shared ``live_server``) because this journey
    kills and restarts the server mid-test.

    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :param tmp_path: Per-test temp dir for DB, artifacts, and logs.
    :returns: The started stack.
    """
    stack = _WakeLossStack(mock_llm_server_url, tmp_path)
    stack.start()
    yield stack
    stack.teardown()


def _register_parent_with_researcher(
    stack: _WakeLossStack,
    parent_model: str,
    child_model: str,
    mock_base: str,
) -> str:
    """Register the orchestrator agent and create its runner-bound session.

    Uses the same omnigent-flavored single-file spec + agent-id lookup the
    ``tests/e2e`` suite uses (``register_inline_agent``), then binds a fresh
    session to the stack's runner.

    :param stack: The running stack.
    :param parent_model: Mock-LLM queue key for the parent.
    :param child_model: Mock-LLM queue key for the researcher child.
    :param mock_base: Mock LLM ``/v1`` base URL.
    :returns: The new session id.
    """
    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
    from tests.e2e.conftest import lookup_agent_id, register_inline_agent

    parent_name = register_inline_agent(
        stack.client,
        name=f"wl-ui-parent-{parent_model[-6:]}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are an orchestrator. Dispatch the researcher sub-agent via "
            "sys_session_send when asked, and read your inbox when woken."
        ),
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "researcher": {
                    "type": "agent",
                    "description": "Researcher sub-agent. Fetches the requested marker.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": "You are the researcher. Return the marker.",
                },
            },
        },
    )
    agent_id = lookup_agent_id(stack.client, parent_name)
    create = stack.client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    create.raise_for_status()
    session_id = str(create.json()["id"])
    bind = stack.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": stack.runner_id})
    bind.raise_for_status()
    return session_id


def _configure_queues(mock_url: str, parent_model: str, child_model: str) -> None:
    """Load the scripted mock-LLM turns for the journey.

    :param mock_url: Mock LLM server base URL.
    :param parent_model: Parent queue key.
    :param child_model: Child queue key.
    """
    httpx.post(
        f"{mock_url}/mock/configure",
        json={
            "key": parent_model,
            "responses": [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_dispatch",
                            "name": "sys_session_send",
                            "arguments": json.dumps(
                                {
                                    "agent": "researcher",
                                    "title": "wake-loss",
                                    "args": "Fetch the marker.",
                                }
                            ),
                        }
                    ],
                },
                {"text": f"OK — {_DISPATCH_ACK}."},
                {
                    "tool_calls": [
                        {"call_id": "call_drain", "name": "sys_read_inbox", "arguments": "{}"}
                    ],
                },
                {"text": f"I {_INBOX_REVEAL} — it completed a while ago."},
            ],
        },
        timeout=5.0,
    ).raise_for_status()
    httpx.post(
        f"{mock_url}/mock/configure",
        json={
            "key": child_model,
            "responses": [{"text": "Research complete. MARKER-OMNI-WAKE-LOSS", "block": True}],
        },
        timeout=5.0,
    ).raise_for_status()


def _gate_pending(mock_url: str) -> bool:
    """Return whether a mock-LLM request is parked on a gate.

    :param mock_url: Mock LLM server base URL.
    :returns: True when a gated request is waiting.
    """
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5, trust_env=False)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def _run_browser_journey(
    stack: _WakeLossStack,
    session_id: str,
    mock_llm_server_url: str,
) -> bool:
    """Drive the synchronous Playwright journey outside pytest's asyncio loop."""
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    auto_woken = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context_kwargs: dict[str, object] = {"viewport": {"width": 1280, "height": 720}}
        if record_dir:
            Path(record_dir).mkdir(parents=True, exist_ok=True)
            context_kwargs["record_video_dir"] = record_dir
        context = browser.new_context(**context_kwargs)
        try:
            page = context.new_page()
            page.goto(f"{stack.base_url}/c/{session_id}")

            composer = page.get_by_placeholder("Send a message…")
            composer.wait_for(state="visible", timeout=30_000)
            composer.fill("Dispatch the researcher sub-agent and report back when it finishes.")
            page.get_by_role("button", name="Send", exact=True).click()

            # Dispatch turn ends: the ack bubble renders; the researcher is
            # now parked mid-LLM-call on the mock gate.
            page.locator(_ASSISTANT_BUBBLE, has_text=_DISPATCH_ACK).first.wait_for(
                state="visible", timeout=120_000
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not _gate_pending(mock_llm_server_url):
                time.sleep(0.25)
            assert _gate_pending(mock_llm_server_url), (
                "researcher never reached its gated LLM call"
            )

            # The outage: the server dies, then the researcher completes into
            # it — the runner's wake POST fails all bounded retries.
            stack.kill_server()
            httpx.post(
                f"{mock_llm_server_url}/gate/release", timeout=5, trust_env=False
            ).raise_for_status()
            _wait_for_log_line(stack.runner_log, _WAKE_FAILED_LOG, timeout=45)

            # Recovery: server back on the same port/DB; runner reconnects.
            # The user refreshes the page and waits — sending NOTHING.
            stack.restart_server()
            page.reload()
            page.locator(_ASSISTANT_BUBBLE, has_text=_DISPATCH_ACK).first.wait_for(
                state="visible", timeout=30_000
            )
            reveal = page.locator(_ASSISTANT_BUBBLE, has_text=_INBOX_REVEAL)
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                if reveal.count() > 0:
                    auto_woken = True
                    break
                time.sleep(1.0)

            if not auto_woken:
                # Demonstrate the workaround the reporter described: a manual
                # bump makes the parent drain its inbox and reveal the result
                # that had been sitting there the whole time.
                composer = page.get_by_placeholder("Send a message…")
                composer.wait_for(state="visible", timeout=30_000)
                composer.fill("Status update on subagents.")
                page.get_by_role("button", name="Send", exact=True).click()
                reveal.first.wait_for(state="visible", timeout=120_000)
                # Hold the failure state on screen so the recording ends on it.
                time.sleep(3)
        finally:
            context.close()
            browser.close()
    return auto_woken


def test_subagent_result_reaches_parent_chat_after_server_blip(
    wake_loss_stack: _WakeLossStack,
    mock_llm_server_url: str,
) -> None:
    """The parent chat must eventually show the sub-agent's result, unprompted.

    Journey (all in the browser): send the dispatch message → see the
    dispatch ack → server restarts while the researcher finishes → wait on
    the parent chat with NO further input → the inbox wake and drain must
    surface in the transcript. On a buggy build nothing arrives; the test
    then demonstrates the manual bump revealing the stranded result, and
    fails.
    """
    stack = wake_loss_stack
    reset_mock_llm(mock_llm_server_url)

    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-wlui-parent-{uid}"
    child_model = f"mock-wlui-child-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"
    _configure_queues(mock_llm_server_url, parent_model, child_model)
    session_id = _register_parent_with_researcher(stack, parent_model, child_model, mock_base)

    with ThreadPoolExecutor(max_workers=1) as executor:
        auto_woken = executor.submit(
            _run_browser_journey,
            stack,
            session_id,
            mock_llm_server_url,
        ).result()

    assert auto_woken, (
        f"Sub-agent completion was stranded: after the server came back and the "
        f"runner reconnected, the parent chat (session {session_id}) never "
        f"auto-surfaced the researcher's result within 75s — it appeared only "
        f"after a manual 'Status update on subagents.' bump drained the inbox. "
        f"The wake POST failed its bounded retries during the outage and nothing "
        f"ever re-attempted it."
    )
