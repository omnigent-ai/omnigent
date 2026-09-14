"""E2E: a claude-sdk session's context ring and ``/compact`` command.

A model-unpinned in-process ``claude-sdk`` agent (the Claude Pro/Max or
gateway login shape) should:

- show a context-window usage ring near the composer once the session
  reports usage, persisting across a reload (the same indicator a native
  session gets); and
- offer the ``/compact`` slash command in the composer menu — claude-sdk is
  not a native terminal wrapper, but its runner sends ``/compact`` to the
  live SDK client to trigger native compaction, so the command is offered.

The ring is harness-agnostic: ``ContextRing`` renders when
``context_window > 0 && last_total_tokens != null`` on the session snapshot.
The server fixture seeds a normal ``hello_world`` session so the page boots
against the real app/server; a route patch reshapes only
``GET /v1/sessions/{id}`` into a claude-sdk snapshot (carrying usage for the
ring) — no real Claude CLI turn is needed (the backend label-persistence path
is covered by the Python relay suite, and the runner ``/compact`` dispatch by
``tests/runner/test_app_sessions_native_events_options.py``). Mirrors
``_patch_session_as_claude_native`` in ``test_claude_model_picker``.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry


def _patch_session_as_claude_sdk(
    page: Page,
    session_id: str,
    *,
    context_window: int | None = None,
    last_total_tokens: int | None = None,
) -> None:
    """Reshape the browser's ``GET /v1/sessions/{id}`` into a claude-sdk snapshot.

    Patches only the exact snapshot path (sub-paths — events, items, stream —
    pass through) so the SPA boots against the real server but sees an
    in-process claude-sdk session. No ``omnigent.wrapper`` label is set:
    claude-sdk is not a native terminal wrapper, so the composer's
    ``isNativeWrapper`` stays false and only the claude-sdk gate drives
    ``/compact``.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param context_window: When set, the snapshot's ``context_window`` (the
        ring denominator); ``None`` leaves the real value.
    :param last_total_tokens: When set, the snapshot's ``last_total_tokens``
        (the ring numerator); ``None`` leaves the real value.
    """

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        if request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        headers = {**response.headers, "content-type": "application/json"}
        payload["harness"] = "claude-sdk"
        if context_window is not None:
            payload["context_window"] = context_window
        if last_total_tokens is not None:
            payload["last_total_tokens"] = last_total_tokens
        route.fulfill(status=200, headers=headers, body=json.dumps(payload))

    page.route("**/v1/sessions/**", _handle)


def test_context_ring_renders_for_claude_sdk_with_usage(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The context ring renders for a claude-sdk session and survives reload.

    The ring is harness-agnostic — it shows whenever the snapshot carries
    ``context_window > 0`` and a ``last_total_tokens`` — so a claude-sdk
    session that has reported usage shows the same ``% of context used``
    indicator as a native session. 45678 / 200000 ≈ 23%.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-sdk with usage.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_sdk(
        page, session_id, context_window=200_000, last_total_tokens=45_678
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # ContextRing carries aria-label "<pct>% of context used" (23% here).
    ring = page.get_by_label(re.compile(r"\d+% of context used"))
    expect(ring).to_be_visible(timeout=15_000)
    expect(ring).to_contain_text("23%")

    # Persists across reload — the snapshot patch still applies, proving the
    # ring hydrates from the persisted snapshot, not just a transient event.
    page.reload()
    expect(page.get_by_label(re.compile(r"\d+% of context used"))).to_be_visible(timeout=15_000)


def test_compact_command_offered_for_claude_sdk(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The ``/compact`` slash command is offered for a claude-sdk session.

    claude-sdk is not a native terminal wrapper (``isNativeWrapper`` stays
    false), but its runner drives native SDK compaction by sending
    ``/compact`` to the live client, so the composer offers the command. The
    snapshot is patched to a claude-sdk harness — which populates the chat
    store's ``sessionHarness`` on bind — and typing ``/compact`` surfaces the
    ``slash-menu-item-compact`` row. A regression that gated ``/compact`` to
    native wrappers only would leave the row absent here.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-sdk.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_sdk(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("/compact")
    compact_row = page.get_by_test_id("slash-menu-item-compact")
    expect(compact_row).to_be_visible(timeout=15_000)
