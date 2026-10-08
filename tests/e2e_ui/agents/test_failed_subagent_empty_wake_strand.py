"""A failed sub-agent must still be surfaced when the parent's auto-wake turn ends empty.

The orchestrator dispatches one ``researcher`` sub-agent whose model calls all
fail (HTTP 429), so the child finishes ``failed`` and the runner delivers the
result to the parent inbox and posts the auto-wake notice. The parent's wake
turn is scripted to complete with an empty assistant completion without
draining the inbox, mirroring Codex's intermittent ``response.completed`` with
``output: []``. With no further user input the framework is expected to surface
the failure anyway (a bounded recovery wake or a durable failed-child state).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
    reset_mock_llm,
)

_ORCHESTRATOR_NAME = "strand_orchestrator"
_RESEARCHER_TITLE = "quota-research"
_DISPATCH_PROMPT = "Please dispatch the researcher sub-agent to investigate."
_DISPATCH_ACK = "Dispatched the researcher; waiting for its result."
_FAILURE_REPORT = "The researcher sub-agent failed: its model calls were rejected with HTTP 429."
_WORKAROUND_PROMPT = "Call sys_read_inbox and handle the failed researcher."
# Only the auto-wake path emits this substring (``_format_subagent_wake_notice``).
_WAKE_NOTICE_SIGNATURE = "waiting in inbox"
# Must exceed the runner's bounded stranded-wake retry schedule (2+5+10+30 s);
# overridable only to fit a constrained runner's wall-clock cap.
_RECOVERY_WINDOW_S = float(os.environ.get("OMNIGENT_STRAND_RECOVERY_WINDOW_S", "120"))
_POLL_S = 2.0
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


@dataclass(frozen=True)
class StrandSession:
    """Runner-bound orchestrator session with scripted parent and researcher queues.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: Parent (orchestrator) session id.
    :param mock_url: Mock LLM server base URL.
    :param parent_model: Per-run model id that keys the orchestrator's queue.
    :param researcher_model: Per-run model id that keys the researcher's queue.
    """

    base_url: str
    session_id: str
    mock_url: str
    parent_model: str
    researcher_model: str


def _orchestrator_yaml(parent_model: str, researcher_model: str) -> str:
    """Build the orchestrator spec with one inline ``researcher`` sub-agent.

    Neither executor sets ``auth``: both reach the mock through the runner's
    ambient ``OPENAI_BASE_URL`` and are routed by model id (same shape as the
    ``joke_subagents_session`` fixture). Baking the per-``exec`` relay URL
    instead would be unreachable from the long-lived runner's namespace.

    :param parent_model: Model id for the orchestrator (mock queue key).
    :param researcher_model: Model id for the researcher (mock queue key).
    :returns: YAML text for the bundle upload.
    """
    return f"""\
name: {_ORCHESTRATOR_NAME}
prompt: |
  You coordinate one `researcher` sub-agent. When asked to investigate,
  call `sys_session_send` to dispatch the researcher, acknowledge, and end
  your turn. When a result reaches your inbox, call `sys_read_inbox` and
  report the outcome, including any failure.
executor:
  model: {parent_model}
  harness: openai-agents
tools:
  researcher:
    type: agent
    description: Investigates a topic and reports back.
    executor:
      model: {researcher_model}
      harness: openai-agents
    prompt: |
      You are a researcher. Investigate the request and report your findings.
"""


def _script_mock(mock_url: str, parent_model: str, researcher_model: str) -> None:
    """Queue the parent and researcher responses.

    Parent: dispatch, acknowledge, an EMPTY auto-wake completion that leaves
    the inbox undrained, then a drain + failure report for whichever later
    turn runs (a recovery wake, or the human workaround). The
    ``required_tools`` guard keeps no-tools title requests from consuming a
    parent turn. Researcher: every request is rejected with HTTP 429.
    """
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "researcher",
                                "title": _RESEARCHER_TITLE,
                                "args": "Investigate the reported quota exhaustion.",
                            }
                        ),
                    }
                ]
            },
            {"text": _DISPATCH_ACK},
            {"text": ""},
            {
                "tool_calls": [
                    {"call_id": "call_drain", "name": "sys_read_inbox", "arguments": "{}"}
                ]
            },
            {"text": _FAILURE_REPORT},
        ],
        key=parent_model,
        required_tools=["sys_session_send"],
    )
    configure_mock_llm(
        mock_url,
        [{"error": "rate limit exceeded", "status_code": 429}] * 20,
        key=researcher_model,
    )


def create_strand_session(base_url: str, mock_url: str, runner_id: str) -> StrandSession:
    """Script the mock, upload the orchestrator bundle and bind its session to *runner_id*.

    :param base_url: Server base URL.
    :param mock_url: Mock LLM server base URL.
    :param runner_id: Online runner that will host the orchestrator session.
    :returns: The created session handle.
    """
    uid = uuid.uuid4().hex[:8]
    parent_model = f"mock-strand-parent-{uid}"
    researcher_model = f"mock-strand-researcher-{uid}"
    _script_mock(mock_url, parent_model, researcher_model)
    yaml_text = _orchestrator_yaml(parent_model, researcher_model)
    # Non-config.yaml arcname routes the bundle through the compat adapter,
    # whose loader parses the inline `type: agent` tools.
    bundle = bundle_files({f"{_ORCHESTRATOR_NAME}.yaml": yaml_text.encode()})
    create_resp = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return StrandSession(
        base_url=base_url,
        session_id=session_id,
        mock_url=mock_url,
        parent_model=parent_model,
        researcher_model=researcher_model,
    )


@pytest.fixture
def strand_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[StrandSession]:
    """A fresh orchestrator session on the shared runner (same contract as the joke fixture)."""
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    session = create_strand_session(
        live_server, mock_llm_server_url, str(_server_state["runner_id"])
    )
    try:
        yield session
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session.session_id}", timeout=10.0)
        finally:
            reset_mock_llm(mock_llm_server_url)
            if respawned_runner is not None:
                respawned_runner.terminate()
                try:
                    respawned_runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned_runner.kill()
                    respawned_runner.wait(timeout=5)


def _snapshot(session: StrandSession) -> dict:
    resp = httpx.get(f"{session.base_url}/v1/sessions/{session.session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _items_blob(session: StrandSession) -> str:
    return json.dumps(_snapshot(session).get("items", []))


def _wake_notice_count(session: StrandSession) -> int:
    return _items_blob(session).count(_WAKE_NOTICE_SIGNATURE)


def _child_summary(session: StrandSession) -> list[dict]:
    resp = httpx.get(
        f"{session.base_url}/v1/sessions/{session.session_id}/child_sessions", timeout=10.0
    )
    resp.raise_for_status()
    return [
        {
            "session_id": child.get("session_id"),
            "current_task_status": child.get("current_task_status"),
            "last_task_error": child.get("last_task_error"),
        }
        for child in resp.json()["data"]
    ]


def _parent_request_count(session: StrandSession) -> int:
    resp = httpx.get(
        f"{session.mock_url}/mock/requests", params={"key": session.parent_model}, timeout=10.0
    )
    resp.raise_for_status()
    return len(resp.json()["requests"])


def _wait_for(check: Callable[[], bool], *, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(_POLL_S)
    raise AssertionError(f"Timed out after {timeout_s:.0f}s waiting for {what}.")


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _expect_researcher_failed_in_rail(page: Page) -> None:
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    row = rail.locator('[data-testid="subagent-row"]')
    expect(row).to_have_count(1, timeout=120_000)
    expect(row.get_by_test_id("subagent-status-avatar")).to_have_attribute(
        "aria-label", re.compile(r"^Failed"), timeout=120_000
    )


@pytest.mark.timeout(600)
def test_failed_subagent_is_surfaced_after_empty_wake_turn(
    request: pytest.FixtureRequest,
    strand_session: StrandSession,
) -> None:
    """With no further input, a failed child whose wake turn ended empty is still reported."""
    session = strand_session
    # Create the recorded page only after the non-browser setup is complete.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{session.base_url}/c/{session.session_id}")

    _send(page, _DISPATCH_PROMPT)
    expect(page.locator(_ASSISTANT, has_text=_DISPATCH_ACK)).to_be_visible(timeout=120_000)
    _expect_researcher_failed_in_rail(page)

    _wait_for(lambda: _wake_notice_count(session) >= 1, timeout_s=90, what="the auto-wake notice")
    # Dispatch, acknowledgement, then the wake turn: its third request consumes
    # the scripted empty completion.
    _wait_for(
        lambda: _parent_request_count(session) >= 3,
        timeout_s=90,
        what="the parent's empty auto-wake turn to run",
    )
    _wait_for(
        lambda: _snapshot(session).get("status") == "idle",
        timeout_s=90,
        what="the parent to go idle after the empty wake turn",
    )

    deadline = time.monotonic() + _RECOVERY_WINDOW_S
    recovery_seen = False
    while time.monotonic() < deadline:
        if _wake_notice_count(session) >= 2 or _FAILURE_REPORT in _items_blob(session):
            recovery_seen = True
            break
        time.sleep(_POLL_S)

    page.reload()
    _expect_researcher_failed_in_rail(page)

    if recovery_seen:
        expect(page.locator(_ASSISTANT, has_text=_FAILURE_REPORT)).to_be_visible(timeout=60_000)
        return

    # The reported workaround: only another human message drains the stranded result.
    _send(page, _WORKAROUND_PROMPT)
    expect(page.locator(_ASSISTANT, has_text=_FAILURE_REPORT)).to_be_visible(timeout=120_000)
    pytest.fail(
        f"Researcher finished failed and its wake notice was posted, but after the parent's "
        f"empty auto-wake turn nothing surfaced for {_RECOVERY_WINDOW_S:.0f}s: no recovery "
        f"wake ({_wake_notice_count(session)} notice(s) total) and no failure report. The "
        f"result stayed in the inbox until the workaround message drained it. "
        f"parent={session.session_id} children={_child_summary(session)} "
        f"parent_model_requests={_parent_request_count(session)}"
    )
