"""Visual-regression snapshot of a multi-turn chat with the left-edge TurnRail.

A committed baseline of the chat surface for a fixed, fully-mocked transcript of
*several* user turns and assistant replies -- enough that the desktop TurnRail
(the left-edge tick minimap) renders. That rail only mounts for a conversation
with two or more turns (``web/src/pages/TurnRail.tsx`` returns null below that),
so the single-turn ``test_chat_snapshot.py`` baseline never captures it. This
baseline guards the spacing between the transcript column and the rail: a
regression that drops the left inset crowds the prose against the ticks. It
renders at a narrower viewport than the rest of the suite so the conversation
area lands in the crowded regime where that inset actually engages (see the
test body).

The determinism strategy is identical to ``test_chat_snapshot.py`` (see its
module docstring): every bind call is ``page.route``-stubbed, the stream is
answered with the ``[DONE]`` sentinel, and the sidebar list is empty. This
transcript uses plain prose only (no fenced code) so the capture needs no Shiki
token-color settle -- the rail spacing, not code rendering, is what it guards.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

_SESSION_ID = "conv_rail_e2e"
_AGENT_ID = "ag_claude_e2e"
_AGENT_NAME = "claude-native-ui"
_HOST_ID = "host_e2e"

# Anchored exactly as in test_chat_snapshot.py so per-session sub-paths never
# fall through to the bare-list route.
_SESSIONS_LIST_RE = re.compile(r"/v1/sessions(\?.*)?$")
_FILESYSTEM_RE = re.compile(r"/v1/hosts/[^/]+/filesystem")
_SESSION_DETAIL_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}(\?.*)?$")
_ITEMS_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/items")
_STREAM_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/stream")
_AGENT_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/agent")
_SUBRESOURCE_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/(child_sessions|resources)")
_HEALTH_RE = re.compile(r"/health(\?.*)?$")

_AGENTS_BODY = {
    "data": [
        {
            "id": _AGENT_ID,
            "name": _AGENT_NAME,
            "display_name": "Claude Code",
            "description": "Anthropic's coding agent",
            "harness": "claude",
            "skills": [],
        }
    ]
}
_HOSTS_BODY = {
    "hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]
}
_EMPTY_LIST_BODY = {"object": "list", "data": [], "has_more": False}

# Four short turns -- enough that the rail mounts (>= 2 turns) and shows a run of
# ticks. Prose only (no code) so no Shiki settle is needed; lines stay short to
# avoid wrap-boundary flake, mirroring test_chat_snapshot.py.
_TURNS = [
    ("How do I read a file in Python?", "Open it in a `with` block so it closes itself."),
    ("What about writing to one?", "Pass mode `'w'` to `open` and call `f.write(...)`."),
    ("How do I append instead?", "Use mode `'a'`; each write lands at the end."),
    ("And reading it line by line?", "Loop over the file object -- it yields one line at a time."),
]


def _items_body() -> dict:
    """Build the items page: newest-first, as the server returns it.

    The client reverses to chronological, so we emit the turns in reverse (last
    assistant reply first) and stable ids keep the capture deterministic.
    """
    data = []
    for i in reversed(range(len(_TURNS))):
        user_text, assistant_text = _TURNS[i]
        data.append(
            {
                "id": f"msg_assistant_{i}",
                "response_id": f"resp_{i}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": assistant_text}],
            }
        )
        data.append(
            {
                "id": f"msg_user_{i}",
                "response_id": f"resp_{i}",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": [{"type": "input_text", "text": user_text}],
            }
        )
    return {
        "object": "list",
        "data": data,
        "first_id": data[0]["id"],
        "last_id": data[-1]["id"],
        "has_more": False,
    }


_ITEMS_BODY = _items_body()
_MODEL_OPTIONS = [
    {"id": "opus", "model": "system.ai.claude-opus-4-10", "displayName": "Opus 4.10"},
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": True,
    },
    {"id": "haiku", "model": "system.ai.claude-haiku-4-5", "displayName": "Haiku 4.5"},
]
_SESSION_BODY = {
    "id": _SESSION_ID,
    "agent_id": _AGENT_ID,
    "agent_name": _AGENT_NAME,
    "status": "idle",
    "created_at": 1704067200,
    "updated_at": 1704067200,
    "labels": {"omnigent.wrapper": "claude-code-native-ui"},
    "harness": "claude",
    "llm_model": "system.ai.claude-sonnet-5",
    "model_options": _MODEL_OPTIONS,
}
_AGENT_BODY = {
    "id": _AGENT_ID,
    "object": "agent",
    "name": _AGENT_NAME,
    "description": "Anthropic's coding agent",
    "harness": "claude",
    "mcp_servers": [],
    "policies": [],
    "terminals": [],
}
_HEALTH_BODY = {"sessions": {_SESSION_ID: {"runner_online": True, "host_online": True}}}
_DONE_SSE = "data: [DONE]\n\n"

_BUBBLE = '[data-testid="message-bubble"]'
_TURN_TICK = "[data-turn-tick]"


@pytest.mark.visual
def test_chat_turn_rail_matches_baseline(
    snapshot_page: Page,
    live_server: str,
    fulfill_json,
    settle_for_snapshot,
    assert_snapshot,
) -> None:
    """A multi-turn transcript renders the left-edge TurnRail, spacing intact.

    Same fixtures and gate as the one-turn chat baseline; this one adds enough
    turns that the desktop tick rail mounts, so the baseline captures the gap
    between the transcript column and the rail.

    Rendered at a narrower-than-default viewport on purpose: the transcript's
    left inset ramps up (a width-driven clamp in ChatPage) only once the
    conversation area is narrow enough that the centered column would reach the
    rail. At the suite's default 1280px the area stays wide and the auto-margins
    alone clear the rail, so the guard would be a no-op; 1024px puts the area in
    the capped regime this baseline is meant to hold — the inset at its maximum,
    column edge parked clear of the ticks (still past the ``md`` breakpoint that
    gates the rail's visibility).
    """
    page = snapshot_page
    page.set_viewport_size({"width": 1024, "height": 800})

    page.route("**/v1/agents", lambda r: fulfill_json(r, _AGENTS_BODY))
    page.route("**/v1/hosts", lambda r: fulfill_json(r, _HOSTS_BODY))
    page.route(_FILESYSTEM_RE, lambda r: fulfill_json(r, _EMPTY_LIST_BODY))
    page.route(_SESSIONS_LIST_RE, lambda r: fulfill_json(r, _EMPTY_LIST_BODY))
    page.route(_ITEMS_RE, lambda r: fulfill_json(r, _ITEMS_BODY))
    page.route(_AGENT_RE, lambda r: fulfill_json(r, _AGENT_BODY))
    page.route(_SUBRESOURCE_RE, lambda r: fulfill_json(r, _EMPTY_LIST_BODY))
    page.route(_SESSION_DETAIL_RE, lambda r: fulfill_json(r, _SESSION_BODY))
    page.route(_HEALTH_RE, lambda r: fulfill_json(r, _HEALTH_BODY))
    page.route(
        _STREAM_RE,
        lambda r: r.fulfill(status=200, content_type="text/event-stream", body=_DONE_SSE),
    )

    page.goto(f"{live_server}/c/{_SESSION_ID}")

    # Every user + assistant bubble must paint before the capture.
    expect(page.locator(f'{_BUBBLE}[data-role="user"]')).to_have_count(len(_TURNS), timeout=30_000)
    expect(page.locator(f'{_BUBBLE}[data-role="assistant"]')).to_have_count(len(_TURNS))
    expect(page.locator('[data-testid="working-indicator"]')).to_have_count(0)
    # The rail mounts one tick per turn once two or more turns exist. Waiting on
    # the ticks is what proves this baseline actually captures the rail.
    expect(page.locator(_TURN_TICK)).to_have_count(len(_TURNS), timeout=30_000)
    expect(page.locator('[data-testid="composer-config-gear"]')).to_be_visible(timeout=30_000)
    expect(page.locator('[data-testid="composer-model-effort-label"]')).to_be_visible()

    # Hide the "Jump to top" pill for the capture: the initial layout settle
    # fires a scroll that reveals it for a ~2s window, so whether it's on screen
    # is a race the baseline shouldn't encode. Force it out like settle does the
    # caret. The left rail's tick preview needs no such handling — it's hover-only
    # and Playwright fires no hover at rest.
    page.add_style_tag(
        content='[aria-label="Jump to the first message"] { display: none !important; }'
    )

    settle_for_snapshot(page)

    assert_snapshot(page)
