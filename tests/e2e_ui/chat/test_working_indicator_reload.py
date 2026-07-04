"""Reload behavior for the main chat Working indicator.

The regression covered here is specific to an active main session whose
snapshot hydrates as ``running`` before any committed or pending chat
bubble exists locally. The UI must keep showing Working across a full
reload instead of falling back to the empty-session start screen.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect


def _publish_status(
    base_url: str, session_id: str, status: str, response_id: str | None = None
) -> None:
    """Publish a session status through the same Omnigent route native harnesses use.

    :param base_url: Base URL of the local e2e server, e.g.
        ``"http://127.0.0.1:51234"``.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param status: Session status to publish, e.g. ``"running"``.
    :param response_id: Optional in-flight turn id. When set on a
        ``running``/``waiting`` edge, the server tracks it and projects it onto
        the session snapshot as ``active_response_id`` — the signal native
        Claude's forwarder now sends so a mid-turn (re)connect reopens the
        streaming lifecycle and renders forwarded tool cards LIVE.
    :returns: None.
    """
    data: dict[str, str] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def _seed_function_call(
    base_url: str,
    session_id: str,
    *,
    response_id: str,
    call_id: str,
    name: str,
    arguments: str,
) -> None:
    """Mirror one in-flight native tool call (no output yet) onto the session.

    Posts the same ``external_conversation_item`` / ``function_call`` a native
    forwarder emits, tagged with ``response_id`` so it belongs to the in-flight
    turn. With no ``function_call_output`` following, the call is still running.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param response_id: Turn id the call belongs to (matches the ``running`` edge).
    :param call_id: Tool-call id, e.g. ``"call_live_1"``.
    :param name: Tool name, e.g. ``"shell"``.
    :param arguments: JSON-encoded arguments string, e.g. ``'{"command": "..."}'``.
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": "claude-native-ui",
                    "name": name,
                    "arguments": arguments,
                    "call_id": call_id,
                },
                "response_id": response_id,
            },
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _snapshot_active_response_id(base_url: str, session_id: str) -> str | None:
    """Return ``active_response_id`` from the session snapshot.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :returns: The in-flight turn id the server is tracking, or ``None`` when idle.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("active_response_id")


def test_midturn_connect_renders_live_tool_card(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A mid-turn connect renders a forwarded tool call as a LIVE card.

    Reproduces native Claude's live-tool-card path without a real LLM turn: a
    tool call is mirrored mid-turn (no output yet) and the turn-start ``running``
    edge carries its ``response_id``. The server tracks that id and projects it
    as ``active_response_id`` on the snapshot; a browser connecting fresh
    (no prior local streaming state) reopens the streaming ``activeResponse``
    from that snapshot, so the tool card renders in its running state — a
    spinner (``Loader2`` ``animate-spin``, emitted only for ``input-available``).
    Before this change the reconnect left the bubble non-streaming, so the same
    call rendered as a static, spinner-less card.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    response_id = "resp_live_tool_1"
    _publish_status(base_url, session_id, "running", response_id=response_id)
    _seed_function_call(
        base_url,
        session_id,
        response_id=response_id,
        call_id="call_live_1",
        name="shell",
        arguments='{"command": "sleep 30"}',
    )

    try:
        # Server half: the snapshot exposes the in-flight turn id.
        assert _snapshot_active_response_id(base_url, session_id) == response_id

        # UI half: a fresh connect reopens streaming from the snapshot, so the
        # tool card shows the running spinner. The Working indicator uses a
        # different mark (OttoIcon/Shimmer, not animate-spin), so a spinning
        # loader in the transcript is unambiguously the live tool card.
        page.goto(f"{base_url}/c/{session_id}")
        spinner = page.locator(".animate-spin")
        expect(spinner.first).to_be_visible(timeout=20_000)

        # A full reload re-hydrates from the same snapshot and stays live —
        # this is the reconnect path, not a fluke of the live SSE tail.
        page.reload()
        expect(spinner.first).to_be_visible(timeout=20_000)
    finally:
        _publish_status(base_url, session_id, "idle", response_id=response_id)


def test_running_empty_session_reload_keeps_working_indicator(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Keep Working visible after reload when the main session is running.

    This reproduces the Nessie/custom-agent reload shape without a slow
    LLM turn: the local server owns the durable ``session.status`` cache,
    the session has no persisted chat bubbles, and the browser hydrates
    from ``GET /v1/sessions/{id}`` after a fresh page load.

    :param page: Playwright page fixture.
    :param seeded_session_pair: ``(base_url, session_a_id, session_b_id)``
        from the local server fixture. This fixture respawns the shared
        runner when a prior UI test killed it.
    :returns: None.
    """
    base_url, session_id, _other_session_id = seeded_session_pair
    _publish_status(base_url, session_id, "running")

    try:
        page.goto(f"{base_url}/c/{session_id}")
        working = page.locator('[data-testid="working-indicator"]')
        expect(working).to_be_visible(timeout=15_000)
        # Old behavior rendered the empty-state headline instead of Working.
        expect(page.get_by_text("What should we work on?")).to_have_count(0)

        page.reload()
        expect(working).to_be_visible(timeout=15_000)
        # Reload used to lose Working and fall back to the new-chat headline.
        expect(page.get_by_text("What should we work on?")).to_have_count(0)
    finally:
        _publish_status(base_url, session_id, "idle")
