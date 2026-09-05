"""The "Working…" spinner disappears while the agent is still running.

Report (databricks sandbox, desktop app): after prompting, the agent said
"let me look for universe repos around the file system" and then *appeared to
stop* — there was no spinner activity telling the user it was still running,
even though the agent was still executing a long, output-less filesystem
search.

Mechanism this reproduces (a native-harness false idle):

A native-terminal harness *without* a status file (cursor / kimi / goose /
qwen / hermes / pi native, and claude-native when Claude's ``sessions/<pid>.json``
is unavailable) derives its running/idle status **only** from the tmux
pane-diff idle watcher (``omnigent/inner/terminal.py`` ``_IdleDetector`` →
``omnigent/runner/resource_registry.py`` ``_on_idle`` → ``_publish_status("idle")``).
While the agent runs a long tool call that produces no incremental pane output
(a filesystem-wide ``find`` / search), the pane stays unchanged past the idle
threshold, so the watcher publishes a **bare ``idle``** session-status edge
(no ``response_id``) *mid-turn*.

The web client trusts ``session.status`` 1:1 (deliberately — see
``test_working_indicator_idle_clears``), and a bare terminal ``idle`` is
finalized as a genuine turn end: ``chatStore`` sets ``status → idle`` /
``sessionStatus → idle`` and marks the ``activeResponse`` ``completed``. Two
things then go dark at once:

* the "Working…" shimmer (``computeShowsWorking`` reads only ``sessionStatus``
  / local send state) disappears, and
* the still-in-flight tool call (a ``function_call`` with no
  ``function_call_output``) collapses from its running spinner to
  ``state: "no-output"`` — "No output was recorded for this tool call"
  (``renderItems.ts`` ``toolItem`` / ``trailingLiveToolCallIds``, which spin
  only while ``lifecycle === "streaming"`` **or** the session is live).

So the UI reads as fully stopped although the search is still running — exactly
the report.

This drives the identical edge shape as ``test_working_indicator_idle_clears``
(turn-start ``running`` + ``response_id``, then a bare ``idle``) through the
same native ``/events`` forwarder path — the only difference is that here a tool
call is genuinely **in flight** (dispatched, no result yet) when the bare idle
arrives. A plain-text turn's bare idle is a real turn end and must still clear
Working (that sibling test); a bare idle while a tool is unresolved is a *false*
idle and must **not** make the session read as stopped. The discriminator is the
unresolved trailing tool call.

The test FAILS on un-fixed code (the shimmer vanishes the moment the bare idle
lands) and must PASS once a false idle mid-tool no longer reads as a turn end.

Run (spawns its own local server + runner; build the SPA first)::

    pytest tests/e2e_ui/chat/test_spinner_disappears_during_tool_run.py -v
"""

from __future__ import annotations

import uuid

import httpx
from playwright.sync_api import Page, expect

_WORKING = '[data-testid="working-indicator"]'
_COMPOSER = "Message the agent"
_NARRATION = "let me look for universe repos around the file system"


def _post_event(client: httpx.Client, session_id: str, event_type: str, data: dict) -> None:
    """Publish one native-forwarder ``/events`` payload.

    :param client: HTTP client bound to the spawned server's base URL.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param event_type: Wire event type, e.g. ``"external_session_status"``.
    :param data: The event's ``data`` payload.
    :returns: None.
    :raises AssertionError: If the server does not accept the event (202).
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": event_type, "data": data},
    )
    assert resp.status_code == 202, resp.text


def test_working_indicator_survives_false_idle_during_tool_call(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A bare ``idle`` mid-tool must not make a running agent read as stopped.

    Journey (what the user does and sees in the SPA):

    1. The user prompts; the turn starts running and the "Working…" shimmer
       lights.
    2. The agent narrates ("let me look for universe repos around the file
       system") and dispatches a long filesystem-search tool call. The tool is
       genuinely in flight — no output has arrived — so the session is still
       working.
    3. The search produces no pane output, so the native PTY-diff idle watcher
       publishes a bare ``idle`` (no ``response_id``) mid-turn.
    4. Because a tool call is still unresolved, the agent is still working: the
       "Working…" indicator must stay lit. Before the fix it vanishes (and the
       pending tool collapses to "no output"), so the UI reads as stopped
       though the search is still running.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    response_id = f"resp_universe_{uuid.uuid4().hex[:8]}"
    call_id = f"call_{uuid.uuid4().hex[:8]}"

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name=_COMPOSER)).to_be_visible(timeout=20_000)

    working = page.locator(_WORKING)

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        # 1. The user's turn starts and is marked running (id-bearing edge
        #    opens the streaming activeResponse and lights the shimmer).
        _post_event(
            client,
            session_id,
            "external_conversation_item",
            {
                "item_type": "message",
                "response_id": response_id,
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Find the universe repos."}],
                },
            },
        )
        _post_event(
            client,
            session_id,
            "external_session_status",
            {"status": "running", "response_id": response_id},
        )
        expect(working).to_be_visible(timeout=15_000)

        # 2. The agent narrates, then dispatches a long, output-less filesystem
        #    search tool. The tool call has NO function_call_output — it is
        #    genuinely in flight.
        _post_event(
            client,
            session_id,
            "external_output_text_delta",
            {"message_id": "live_text_1", "index": 0, "delta": _NARRATION},
        )
        _post_event(
            client,
            session_id,
            "external_conversation_item",
            {
                "item_type": "function_call",
                "response_id": response_id,
                "item_data": {
                    "agent": "e2e-universe-agent",
                    "name": "shell",
                    "arguments": '{"command": "find / -type d -name \'*universe*\'"}',
                    "call_id": call_id,
                },
            },
        )
        expect(page.get_by_text(_NARRATION)).to_be_visible(timeout=15_000)
        # The turn is still working with a tool in flight.
        expect(working).to_be_visible(timeout=15_000)

        # 3. The search runs silently: the PTY-diff idle watcher misreads the
        #    quiet pane and publishes a bare `idle` (no response_id) mid-turn.
        _post_event(client, session_id, "external_session_status", {"status": "idle"})

    # Let the bare-idle edge fully settle in the client store before asserting,
    # so the check reflects the settled state rather than a pre-event frame.
    page.wait_for_timeout(2_500)

    # 4. A tool call is still unresolved, so the agent is still working — the
    #    "Working…" shimmer must remain. Before the fix, the bare idle is
    #    treated as a turn end and the shimmer disappears (the reported bug).
    expect(working).to_be_visible()
