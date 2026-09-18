"""UI journey regression for a stranded failed sub-agent.

A failed sub-agent's result is delivered to the parent inbox and the
framework posts the auto-wake notice, but when the parent's auto-wake turn
completes EMPTY (an empty ``response.completed`` — an acknowledged
intermittent model behavior), no recovery wake fires and no durable
failed-child state surfaces. The orchestrator sits idle forever with the
failed-child payload stranded in its inbox; the user sees nothing (the SPA
deliberately hides ``subagent_wake`` system markers), and the workflow only
advances when a human sends another message.

Journey (all through the real SPA against a live server + runner + mock LLM):

1. The user asks the orchestrator to dispatch its ``researcher`` sub-agent.
2. The researcher's model calls all fail (mock provider 429s), so the child
   session terminates ``failed`` and the runner posts the
   ``[System: sub-agent … finished (failed) — 1 result waiting in inbox …]``
   wake notice to the parent.
3. The parent's auto-wake turn is scripted to complete with EMPTY text —
   mirroring the reported Codex-native ``{"response": {"output": []}}``.
4. The user sends nothing further.

Contract under test (the fix target): the stranded failed-child result must
remain actionable — the framework fires a bounded recovery wake, whose turn
(scripted here as the parent's 4th model response) surfaces the failure in
the transcript with NO further user input. While the bug is live the final
assertion times out: the parent's wake-pending flag was discarded at turn
start (``_run_turn_bg``), so ``_rewake_parent_if_inbox_stranded`` returns
early and the 4th parent model request never happens.

Invoke with::

    pytest tests/e2e_ui/agents/test_failed_subagent_empty_wake_strand.py \
        -v --ui-skip-build
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

# Private helpers from the parent conftest — same import pattern the
# sibling agents-rail fixtures use.
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
)

_ORCHESTRATOR_NAME = "strand_director"
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_SUBAGENT_STATUS_DOT = '[data-testid="subagent-status-dot"]'

# The auto-wake notice is emitted ONLY by _format_subagent_wake_notice;
# "finished (failed)" pins it to a FAILED child terminal delivery.
_FAILED_WAKE_FRAGMENT = "finished (failed)"
_WAKE_NOTICE_SIGNATURE = "waiting in inbox"

# Child failure + wake + empty turn are all mock-fast, but each hop is a
# real subprocess round trip; budgets mirror the sibling agents tests.
pytestmark = [pytest.mark.timeout(600)]


@dataclass(frozen=True)
class StrandSession:
    """Handle for the stranded-failed-sub-agent session fixture.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound parent session id.
    :param mock_url: Mock LLM server base URL (for the request ledger).
    :param routing_token: Per-run token routing the PARENT's mock queue;
        it appears only in the user's chat message.
    :param child_token: Per-run token routing the CHILD's mock queue; it
        appears only in the dispatch args.
    :param surface_code: Per-run nonce in the parent's 4th scripted response.
        It can reach the transcript ONLY via a post-empty-wake recovery
        turn, so its presence is the fixed behavior and its absence the bug.
    """

    base_url: str
    session_id: str
    mock_url: str
    routing_token: str
    child_token: str
    surface_code: str


def _strand_director_yaml() -> str:
    """Build the orchestrator spec (parent + one researcher sub-agent).

    Mirrors the omnigent-flavored inline ``type: agent`` shape parsed by
    ``omnigent/inner/loader.py:_parse_tool`` (same as the joke-director
    fixture in this directory's conftest). The child carries no ``auth``
    block: the e2e-ui server/runner export ``OPENAI_BASE_URL`` pointing at
    the mock server, and per-run content-routing tokens select each queue.
    """
    return f"""\
name: {_ORCHESTRATOR_NAME}
prompt: |
  You are an orchestrator with one `researcher` sub-agent. When the user
  asks you to dispatch research, call `sys_session_send` to hand the task
  to the `researcher` sub-agent, then end your turn and wait. When results
  arrive in your inbox, report them to the user.

executor:
  model: gpt-4o-mini
  harness: openai-agents

tools:
  researcher:
    type: agent
    description: Research sub-agent. Investigates one topic when asked.
    executor:
      model: gpt-4o-mini
      harness: openai-agents
    prompt: |
      You are a researcher. Investigate the topic you are given and reply
      with your findings.
"""


@pytest.fixture
def strand_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[StrandSession]:
    """Create a runner-bound orchestrator session wired for the strand repro.

    Mock queues (content-routed so concurrent turns cannot race):

    Parent (selected by ``routing_token`` in the user's message):
      1. dispatch: ``sys_session_send`` tool call to ``researcher``.
      2. after tool result: text acknowledging the dispatch.
      3. the auto-wake turn after the child FAILS: EMPTY text — the
         reported empty ``response.completed``.
      4. the recovery-wake turn: text carrying ``surface_code``. Reached
         only if the framework re-wakes the parent after the empty turn.

    Child (selected by ``child_token`` in the dispatch args): a run of
    provider errors deep enough to outlast SDK retries, so the child's
    turn terminates with an executor error and the child goes ``failed``.
    """
    suffix = uuid.uuid4().hex[:10]
    routing_token = f"strand-parent-{suffix}"
    child_token = f"strand-child-{suffix}"
    surface_code = f"stranded-result-surfaced-{suffix}"

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_researcher",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "researcher",
                                "title": "quota-research",
                                "args": (f"Investigate the topic. Routing marker: {child_token}"),
                            }
                        ),
                    },
                ]
            },
            {"text": "Dispatched the researcher; waiting for its result."},
            # The auto-wake turn for the FAILED child completes empty —
            # the reported {"response":{"output":[]}} shape.
            {"text": ""},
            # Served only by a post-empty-turn recovery wake (the fix).
            {
                "text": (
                    f"The researcher sub-agent FAILED — surfacing the stranded "
                    f"result now. {surface_code}"
                )
            },
        ],
        key=routing_token,
        match=routing_token,
    )
    # Deep enough to exhaust openai-client default retries (2 retries =
    # 3 attempts) several times over, so the child turn errors terminally
    # instead of succeeding off the queue-exhausted default response.
    configure_mock_llm(
        mock_llm_server_url,
        [{"error": "rate limit exceeded (mock provider 429)", "status_code": 429}] * 8,
        key=child_token,
        match=child_token,
    )

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    yaml_bytes = _strand_director_yaml().encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Non-config.yaml arcname routes the bundle through the omnigent
        # compat adapter, whose loader parses the inline `type: agent`
        # tools. The spec_version:1 parser does not accept this shorthand.
        info = tarfile.TarInfo(name=f"{_ORCHESTRATOR_NAME}.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield StrandSession(
            base_url=live_server,
            session_id=session_id,
            mock_url=mock_llm_server_url,
            routing_token=routing_token,
            child_token=child_token,
            surface_code=surface_code,
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _items_blob(base_url: str, session_id: str) -> str:
    """Return all items in a session snapshot as one JSON string."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return json.dumps(resp.json().get("items", []))


def _parent_llm_request_count(mock_url: str, routing_token: str) -> int:
    """Count mock-LLM requests that belong to the PARENT's queue.

    The routing token appears only in the user's chat message, which is
    carried (as user input) in every parent-turn request and in none of
    the child's, so token containment identifies parent turns exactly.
    """
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return sum(1 for r in resp.json().get("requests", []) if routing_token in json.dumps(r))


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    what: str,
    interval_s: float = 2.0,
) -> None:
    """Poll *predicate* until true or fail with *what* after *timeout_s*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    pytest.fail(f"Timed out after {timeout_s:.0f}s waiting for {what}")


def test_failed_subagent_result_survives_empty_wake_turn(
    page: Page,
    strand_session: StrandSession,
) -> None:
    """A failed child's stranded result must surface after an empty wake turn.

    With the bug live, everything up
    to and including the empty auto-wake turn happens (asserted as
    preconditions), and then NOTHING — the final expectation times out
    because no recovery wake ever fires and the failed-child payload stays
    silently stranded in the parent inbox.
    """
    chat = strand_session
    page.goto(f"{chat.base_url}/c/{chat.session_id}")

    # 1. The user asks for a dispatch; the scripted parent dispatches the
    #    researcher and acknowledges. This is the LAST user input the
    #    session ever receives.
    _send(
        page,
        "Please dispatch the researcher sub-agent to investigate. "
        f"Routing marker: {chat.routing_token}",
    )
    expect(page.locator(_ASSISTANT, has_text="Dispatched the researcher").first).to_be_visible(
        timeout=120_000
    )

    # 2. The child's provider calls all 429 → its turn errors terminally →
    #    the runner marks the child failed and posts the failed-child wake
    #    notice. The SPA hides subagent_wake markers, so assert the notice
    #    via the session snapshot — this is the reported state: failed
    #    result delivered to the inbox + wake posted.
    _wait_until(
        lambda: (
            _FAILED_WAKE_FRAGMENT in _items_blob(chat.base_url, chat.session_id)
            and _WAKE_NOTICE_SIGNATURE in _items_blob(chat.base_url, chat.session_id)
        ),
        timeout_s=180,
        what=(
            "the failed-child auto-wake notice "
            f"('... {_FAILED_WAKE_FRAGMENT} ... {_WAKE_NOTICE_SIGNATURE} ...') "
            "in the parent session items"
        ),
    )

    # The child's failure IS user-visible in the Agents rail: the
    # researcher row shows the destructive "failed" status.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    row = rail.locator(_SUBAGENT_ROW).first
    expect(row).to_be_visible(timeout=30_000)
    expect(row.locator(_SUBAGENT_STATUS_DOT)).to_contain_text(
        re.compile("failed", re.IGNORECASE), timeout=60_000
    )

    # 3. The auto-wake turn runs and completes EMPTY: the parent's third
    #    scripted model response ("") is served. Parent requests so far:
    #    dispatch tool-call round + post-tool text (turn 1) + wake turn.
    _wait_until(
        lambda: _parent_llm_request_count(chat.mock_url, chat.routing_token) >= 3,
        timeout_s=120,
        what="the parent's empty auto-wake turn to consume its model response",
    )

    # 4. THE BUG. No further user input is sent. A failed child result
    #    already delivered to the inbox must not go silent merely because
    #    the wake turn returned an empty completion: the framework must
    #    re-wake the parent (bounded), whose next scripted turn surfaces
    #    the failure in the transcript. While the bug is live, the
    #    wake-pending flag was discarded at turn start, the recovery check
    #    no-ops, the parent's 4th model request never happens, and this
    #    times out with the orchestrator idle and the user none the wiser.
    expect(page.locator(_ASSISTANT, has_text=chat.surface_code).first).to_be_visible(
        timeout=120_000
    )
