"""UI journey: a finished sub-agent's conversation must show its
transcript when clicked into — without a page refresh.

The user asks the dispatcher (the session's top-level agent) to get a result
from its single `worker` sub-agent. While the worker's turn is still running
(its mock LLM call is held on a gate), the user clicks into the worker's
conversation to watch it — which is what makes the SPA retain a live entry
for the child — then returns to the parent. The gate is released, the worker
finishes, and the parent's auto-wake turn relays the result (carrying the
per-run nonce) into the parent chat.

The user then clicks back into the worker's conversation. Correct behavior:
the worker's reply (identified by the nonce, which only the worker's scripted
response carries) is visible in the child transcript. The reported bug: the
child conversation renders empty — no output, no thoughts — and only a full
page refresh makes the transcript appear.

The trigger is the child's live SSE tail silently delivering nothing. That is
a real state of the reported deployment (sharded Databricks Apps): per
``bindStream``'s own comment in ``web/src/store/chatStore.ts``, a session
stream that opens unkeyed routes to the wrong replica and "silently delivers
nothing", and the host-resolve mitigation is explicitly best-effort (a failed
resolve falls through to the unkeyed open). A single-replica local server
cannot produce that routing miss on its own — the clean journey passes here —
so this test injects the fault at the transport: a Playwright route blocks
the CHILD session's ``/stream`` requests (only the live tail; every snapshot
and items GET stays real). With the child's live tail dead, the SPA's
retained entry never learns about the child's committed items, yet
``isConversationStreamCurrent`` still counts the entry as live (the pump
holds its abort controller across reconnect attempts), so revisiting paints
the stale empty transcript without a refetch.

On a buggy build this test fails with a stale-revisit-specific message after
first demonstrating the reporter's workaround on screen — a full page reload
(with the child's live tail STILL dead) makes the transcript appear, proving
the items were persisted server-side the whole time and the staleness lives
in the client's retained entry.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import tarfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
)

_AGENT_NAME = "stale_transcript_dispatcher"
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'

# Dispatch turn + gated child turn + auto-wake continuation are several
# serial mock-LLM calls; give the relay assertion the same generous budget
# the sibling multi-agent journeys use.
_RELAY_TIMEOUT_MS = 240_000

# How long the revisited child transcript gets to paint the worker's reply
# before we declare it stale. A live entry paints instantly and a cold bind's
# items fetch is one round trip, so 30s is generous.
_CHILD_REPLY_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class StaleTranscriptSession:
    """Handle for the one-worker dispatcher session fixture.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound parent session id.
    :param code: Per-run nonce only the worker's scripted reply carries.
    :param routing_token: Per-run token that selects the parent's mock queue.
    :param mock_url: Mock LLM server base URL (for the gate endpoints).
    """

    base_url: str
    session_id: str
    code: str
    routing_token: str
    mock_url: str


def _dispatcher_yaml(code: str, mock_base: str) -> str:
    """Build the dispatcher spec (parent + one inline worker sub-agent).

    Mirrors the omnigent-flavored inline ``type: agent`` shape the sibling
    ``joke_subagents_session`` fixture uses, plus an explicit ``auth`` block
    per executor (same as the wake-loss journey) so the harness talks to the
    mock LLM regardless of ambient credential config. The nonce lives ONLY in
    the worker's prompt/scripted reply, so the nonce appearing in the parent's
    bubble proves a real dispatch round trip, and the nonce appearing in the
    child transcript proves the child's own items rendered.

    :param code: Per-run nonce in the worker's canned reply and nowhere else.
    :param mock_base: Mock LLM ``/v1`` base URL for the executors' auth block.
    :returns: YAML text ready for bundle upload.
    """
    return f"""\
name: {_AGENT_NAME}
prompt: |
  You are a dispatcher coordinating one worker sub-agent named `worker`.
  You must NEVER produce a result yourself — only your worker does.

  When the user asks you to get the result, you MUST call
  `sys_session_send` to ask your `worker` sub-agent for it. Then end your
  turn and wait; do not poll. When the worker's reply arrives in your
  inbox, relay it to the user VERBATIM — repeat every word and every code
  exactly as written.

  You have exactly ONE worker. If a worker sub-agent already exists, send
  any follow-up to that SAME sub-agent session — NEVER spawn a second one.

executor:
  model: gpt-4o-mini
  harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_base}

tools:
  worker:
    type: agent
    description: Worker sub-agent. Produces exactly one result when asked.
    executor:
      model: gpt-4o-mini
      harness: openai-agents
      auth:
        type: api_key
        api_key: mock-key
        base_url: {mock_base}
    prompt: |
      You are a worker. When asked for the result, reply with exactly
      this and nothing else:

      Worker result ready. Result code: {code}.
"""


@pytest.fixture
def stale_transcript_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[StaleTranscriptSession]:
    """Create a runner-bound session for the one-worker dispatcher.

    Same runner-respawn + bundle-upload + bind contract as the sibling
    ``joke_subagents_session`` fixture. Separate content-routed mock queues
    drive the parent and the worker; the worker's single scripted reply is
    gated (``block: true``) so the test controls exactly when the child's
    turn completes.

    :param live_server: Spawned server fixture from the parent conftest.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: A :class:`StaleTranscriptSession` handle.
    """
    code = f"result-{uuid.uuid4().hex[:10]}"
    suffix = uuid.uuid4().hex[:10]
    routing_token = f"stale-parent-{suffix}"
    child_token = f"stale-child-{suffix}"
    yaml_text = _dispatcher_yaml(code, f"{mock_llm_server_url}/v1")

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_worker",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": "worker",
                                "args": (f"Produce the result. Routing marker: {child_token}"),
                            }
                        ),
                    }
                ]
            },
            {"text": "Dispatched the worker; waiting for its reply."},
            {"text": f"The worker replied with result code {code}."},
        ],
        key=routing_token,
        match=routing_token,
    )
    # The worker's one reply is gated: its LLM call blocks until the test
    # releases the gate, keeping the child mid-turn while the user visits it.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Worker result ready. Result code: {code}.", "block": True}],
        key=child_token,
        match=child_token,
    )

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    yaml_bytes = yaml_text.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Non-config.yaml arcname routes the bundle through the omnigent
        # compat adapter, whose loader parses the inline `type: agent` tools.
        info = tarfile.TarInfo(name=f"{_AGENT_NAME}.yaml")
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
        yield StaleTranscriptSession(
            base_url=live_server,
            session_id=session_id,
            code=code,
            routing_token=routing_token,
            mock_url=mock_llm_server_url,
        )
    finally:
        # Release any still-held gate so the child turn can't outlive the
        # test and strand the shared runner mid-turn for later tests.
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0, trust_env=False)
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send.

    :param page: Page already on a ``/c/<id>`` route.
    :param text: Message text to send.
    """
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_for_gate_pending(mock_url: str, timeout_s: float = 120.0) -> None:
    """Poll the mock LLM until a request is blocked on the gate.

    :param mock_url: Mock LLM server base URL.
    :param timeout_s: Max seconds to wait for the child's gated call.
    :raises AssertionError: If no request blocks within the budget.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
        if resp.json()["pending"]:
            return
        time.sleep(0.5)
    raise AssertionError("worker's gated LLM call never became pending")


def _release_gate(mock_url: str) -> None:
    """Release the oldest pending mock-LLM gate (the worker's reply).

    :param mock_url: Mock LLM server base URL.
    """
    resp = httpx.post(f"{mock_url}/gate/release", timeout=5.0, trust_env=False)
    resp.raise_for_status()
    assert resp.json()["released"], "gate release found nothing pending"


def _open_agents_rail(page: Page):
    """Expand the Workspace rail and select its Agents tab.

    :param page: Page on a ``/c/<id>`` route.
    :returns: The rail locator, for scoping row lookups to the desktop rail
        (the hidden mobile drawer mirrors the same testids).
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    return rail


# Nightly: full dispatch + gated child turn + auto-wake UI journey — the same
# heavy multi-session spin-up as the sibling agents-rail tests, kept off the
# PR gate. Scripted mock-LLM queues keep it deterministic.
@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_subagent_transcript_visible_after_completion_without_refresh(
    page: Page,
    stale_transcript_session: StaleTranscriptSession,
) -> None:
    chat = stale_transcript_session
    parent_url = f"{chat.base_url}/c/{chat.session_id}"
    page.goto(parent_url)

    # Ask the dispatcher to get the result from its worker sub-agent.
    _send(
        page,
        "Please get the result from the worker and relay it exactly, "
        f"including the result code. Routing marker: {chat.routing_token}",
    )

    # The dispatch turn's ack proves the parent turn ran and the
    # `sys_session_send` tool call fired before we wait on the rail.
    expect(page.locator(_ASSISTANT, has_text="Dispatched the worker").first).to_be_visible(
        timeout=120_000
    )

    # The worker session appears in the Agents rail once dispatched.
    rail = _open_agents_rail(page)
    rows = rail.locator(_SUBAGENT_ROW)
    expect(rows).to_have_count(1, timeout=120_000)
    child_id = rows.first.get_attribute("data-child-session-id")
    assert child_id, "subagent row is missing data-child-session-id"

    # The worker's LLM call is held on the mock gate, so the child is
    # provably mid-turn while the user visits it.
    _wait_for_gate_pending(chat.mock_url)

    # Inject the reported trigger: the child's live SSE tail silently
    # delivers nothing. On the sharded deployment this is the unkeyed /
    # wrong-replica stream `bindStream` documents (the host-resolve
    # mitigation is best-effort); locally we produce the same eventless
    # tail by blocking ONLY the child's `/stream` transport. Every
    # snapshot and items GET stays real, so the server-side transcript
    # remains fully reachable throughout.
    def _block_child_stream(route: Route) -> None:
        route.abort()

    page.route(f"**/v1/sessions/{child_id}/stream*", _block_child_stream)

    # Visit the running worker — the step that makes the SPA retain a live
    # entry for the child conversation (retained entries are the whole
    # point of background streams; a revisit paints them instantly).
    rows.first.click()
    page.wait_for_url(re.compile(re.escape(f"/c/{child_id}")))
    back_link = page.get_by_role("link", name="Back to parent session")
    expect(back_link).to_be_visible(timeout=30_000)

    # Return to the parent and let the worker finish.
    back_link.click()
    page.wait_for_url(re.compile(re.escape(f"/c/{chat.session_id}")))
    _release_gate(chat.mock_url)

    # The worker's reply (identified by the nonce) reaches the parent via the
    # real dispatch -> child turn -> inbox -> auto-wake pipeline. This is the
    # report's "the preview shows their responses and the parent reads their
    # output" state: the child's items are committed server-side by now.
    expect(page.locator(_ASSISTANT, has_text=chat.code).first).to_be_visible(
        timeout=_RELAY_TIMEOUT_MS
    )

    # Click back into the finished worker's conversation. Correct behavior:
    # its transcript — including the worker's reply — renders WITHOUT a page
    # refresh, even though the child's live tail never delivered an event.
    rail = _open_agents_rail(page)
    row = rail.locator(_SUBAGENT_ROW).first
    expect(row).to_be_visible(timeout=30_000)
    row.click()
    page.wait_for_url(re.compile(re.escape(f"/c/{child_id}")))

    child_reply = page.locator(_ASSISTANT, has_text=chat.code).first
    try:
        expect(child_reply).to_be_visible(timeout=_CHILD_REPLY_TIMEOUT_MS)
    except AssertionError:
        # The reported bug: the child conversation rendered without its
        # transcript. Demonstrate the reporter's workaround on screen — a
        # full page reload (the child's live tail is STILL blocked) — to
        # prove the transcript was persisted server-side all along: client
        # staleness, not data loss.
        page.reload()
        try:
            expect(page.locator(_ASSISTANT, has_text=chat.code).first).to_be_visible(
                timeout=_CHILD_REPLY_TIMEOUT_MS
            )
        except AssertionError:
            pytest.fail(
                "sub-agent transcript missing even after a full page reload — "
                "the child's items never persisted; NOT the stale-view "
                "symptom (which a reload fixes)"
            )
        pytest.fail(
            "stale revisit: the finished sub-agent's conversation rendered empty "
            "when clicked into, and its transcript appeared only after a full "
            "page reload — the retained live entry for the child went stale "
            "(its eventless stream still counted as current, so the revisit "
            "never refetched the committed items)"
        )
