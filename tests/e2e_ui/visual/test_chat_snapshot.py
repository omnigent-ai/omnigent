"""Visual-regression snapshot of a mocked chat conversation ("/c/{id}").

A committed baseline of the chat surface for a fixed, fully-mocked transcript:
one user turn and one assistant reply (markdown -- prose, a bullet list, inline
and fenced code) rendered as message bubbles, with the composer below. Same gate,
renderer, and update flow as the empty-landing snapshot -- see ``README.md``.

Determinism strategy -- the chat page is a pure function of the committed bundle
plus ``page.route`` stubs for every call the bind path makes (the exact load
order is: open the per-session SSE stream, then fetch the slim session + the
items page; see ``web/src/store/chatStore.ts``):

* ``GET /v1/sessions/{id}/stream`` is answered with the server's ``[DONE]``
  sentinel -- a *clean* close the store does NOT reconnect on -- so no live event
  ever mutates the view (a bare close would read as a transport drop and respawn
  the stream, churning the capture).
* ``GET /v1/sessions/{id}/items`` returns the fixed transcript. The server orders
  items newest-first and the client reverses to chronological, so the assistant
  reply precedes the user turn in the stub.
* the slim session reports ``status: "idle"`` and ``/health`` reports the runner
  online, so neither the "Working…" shimmer nor an offline/connecting indicator
  appears.
* the session *list* is stubbed empty (exactly like the landing): an empty sidebar
  means an empty ``/v1/sessions/updates`` watch-set, so the real (empty) updates
  socket pushes nothing and no relative "x min ago" row timestamp can drift the
  capture. The captured value is the conversation surface, not the sidebar list.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

_SESSION_ID = "conv_e2e"
_AGENT_ID = "ag_claude_e2e"
_AGENT_NAME = "claude-native-ui"
_HOST_ID = "host_e2e"

# Bare session list/scan (sidebar) -- stubbed empty, exactly like the landing.
# Anchored so it matches `/v1/sessions` (+ query) but none of the per-session
# `/v1/sessions/{id}/...` sub-paths below, so the routes never overlap.
_SESSIONS_LIST_RE = re.compile(r"/v1/sessions(\?.*)?$")
_FILESYSTEM_RE = re.compile(r"/v1/hosts/[^/]+/filesystem")
# Per-session bind calls. The detail regex anchors after the id so it matches the
# slim session GET but not `/items`, `/stream`, `/agent`, etc.
_SESSION_DETAIL_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}(\?.*)?$")
_ITEMS_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/items")
_STREAM_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/stream")
_AGENT_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/agent")
# Side-rail chrome (agents-rail badge, terminals, environments). Stubbed empty so
# the real server's 404 for this (server-unknown) session can't leak an error.
_SUBRESOURCE_RE = re.compile(rf"/v1/sessions/{_SESSION_ID}/(child_sessions|resources)")
_HEALTH_RE = re.compile(r"/health(\?.*)?$")

_AGENTS_BODY = {
    "data": [
        {
            "id": _AGENT_ID,
            "name": _AGENT_NAME,
            "display_name": "Claude Code",
            "description": "Anthropic's coding agent",
            "harness": None,
            "skills": [],
        }
    ]
}
_HOSTS_BODY = {
    "hosts": [{"host_id": _HOST_ID, "name": "e2e-host", "owner": "e2e", "status": "online"}]
}
_EMPTY_LIST_BODY = {"object": "list", "data": [], "has_more": False}

_USER_TEXT = "How do I read a file in Python?"
# Keep every line comfortably SHORT. The code box is narrow, and a line that
# lands near its wrap/overflow boundary renders nondeterministically: subpixel
# differences flip the SPA between "fits" (clipped, no wrap toggle) and
# "overflows" (wraps + shows a wrap toggle), which adds a row and shifts the
# whole transcript below it -- a snapshot flake with no UI change behind it. So
# no line here should approach the box width.
_ASSISTANT_TEXT = (
    "Use a `with` block so the file closes itself:\n\n"
    "```python\n"
    "with open('notes.txt') as f:\n"
    "    print(f.read())\n"
    "```\n\n"
    "A couple of notes:\n\n"
    "- `with` frees the handle even on an error.\n"
    "- Use `f.read()` for all of it, or loop to stream.\n"
)

# Server returns newest-first; the client reverses to chronological, so the
# assistant reply (newest) precedes the user turn here.
_ITEMS_BODY = {
    "object": "list",
    "data": [
        {
            "id": "msg_assistant",
            "response_id": "resp_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": _ASSISTANT_TEXT}],
        },
        {
            "id": "msg_user",
            "response_id": "resp_1",
            "type": "message",
            "role": "user",
            "status": "completed",
            "content": [{"type": "input_text", "text": _USER_TEXT}],
        },
    ],
    "first_id": "msg_assistant",
    "last_id": "msg_user",
    "has_more": False,
}
_SESSION_BODY = {
    "id": _SESSION_ID,
    "agent_id": _AGENT_ID,
    "agent_name": _AGENT_NAME,
    "status": "idle",
    "created_at": 1704067200,
    "updated_at": 1704067200,
}
_AGENT_BODY = {
    "id": _AGENT_ID,
    "object": "agent",
    "name": _AGENT_NAME,
    "description": "Anthropic's coding agent",
    "harness": None,
    "mcp_servers": [],
    "policies": [],
    "terminals": [],
}
_HEALTH_BODY = {"sessions": {_SESSION_ID: {"runner_online": True, "host_online": True}}}

# A clean server close: the [DONE] sentinel ends the stream pump without a
# reconnect (a bare close would read as a transport drop and respawn it).
_DONE_SSE = "data: [DONE]\n\n"

_BUBBLE = '[data-testid="message-bubble"]'
# Shiki loads lazily, so the fenced block paints raw first and re-renders with
# per-token spans (colored via the `--sdm-c` custom property) once the import
# resolves. Span *presence* races the paint — the spans mount a frame before
# their colors are composited — so the capture waits on the computed colors.
_TOKEN_SPANS = '[data-streamdown="code-block-body"] span[style*="--sdm-c"]'


@pytest.mark.visual
def test_chat_conversation_matches_baseline(
    snapshot_page: Page,
    live_server: str,
    fulfill_json,
    settle_for_snapshot,
    assert_snapshot,
) -> None:
    """A mocked chat transcript renders pixel-identical to the committed baseline.

    :param snapshot_page: page pinned to a fixed viewport + light palette (see
        the suite ``conftest.py``).
    :param live_server: Base URL of the spawned ``omnigent server`` serving the
        built SPA. Every data call the chat bind makes is stubbed below, so no
        real session / LLM is involved.
    :param fulfill_json: 200-JSON route helper (suite ``conftest.py``).
    :param settle_for_snapshot: fonts + caret settle, run before capture.
    :param assert_snapshot: ``pytest-playwright-visual-snapshot`` fixture; writes
        the baseline under ``--update-snapshots`` and otherwise compares against
        it, failing (and emitting actual/expected/diff PNGs) on any mismatch.
    """
    page = snapshot_page

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

    # Bubbles paint only after the bind hydrates (the "Loading conversation…"
    # placeholder is gone). Wait for both roles so the capture is complete.
    expect(page.locator(f'{_BUBBLE}[data-role="user"]')).to_have_count(1, timeout=30_000)
    expect(page.locator(f'{_BUBBLE}[data-role="assistant"]')).to_be_visible(timeout=30_000)
    # No live turn is in flight, so the working shimmer must be absent.
    expect(page.locator('[data-testid="working-indicator"]')).to_have_count(0)

    # Shiki loads lazily: the colored token spans mount a frame after the block
    # first paints raw. Wait until the tokens resolve more than one distinct
    # color (the raw fallback is uniform `inherit`), then flush two animation
    # frames so those colors are composited before the screenshot — waiting on
    # span presence alone races the paint and captured a raw frame.
    page.wait_for_function(
        """(selector) => {
            const spans = Array.from(document.querySelectorAll(selector));
            if (spans.length < 2) return false;
            const colors = new Set(spans.map((el) => getComputedStyle(el).color));
            return colors.size > 1;
        }""",
        arg=_TOKEN_SPANS,
        timeout=30_000,
    )
    page.evaluate(
        "() => new Promise((resolve) => "
        "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )

    # Settle web fonts + kill the blinking caret (both time-dependent).
    settle_for_snapshot(page)

    # Full viewport: the open sidebar + the conversation transcript + composer.
    assert_snapshot(page)
