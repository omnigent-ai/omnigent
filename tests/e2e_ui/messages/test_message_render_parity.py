r"""UI journey: messages render identically to the transcript, with no dupes.

Drives a custom ``openai-agents`` agent ("echo_probe") through five real chat
turns via the web composer and asserts two properties that historically
regressed on the native forwarder:

1. **Render parity with the TUI.** The terminal UI (``omnigent/chat.py``) and
   the web SPA both render from the SAME canonical transcript,
   ``GET /v1/sessions/{id}/items``. So "renders exactly the same as the TUI"
   is checked by treating that transcript as ground truth: every per-turn
   user marker and assistant token the SPA shows in a bubble must also be in
   the transcript, in the same order, exactly once. If the SPA bubbles and the
   transcript carry the same markers in the same order, the SPA renders what
   the TUI renders.

2. **No duplicate rendering.** Each turn embeds a unique user marker and asks
   the agent to echo a unique assistant token. After the turn settles (working
   shimmer gone) each marker must appear in EXACTLY ONE bubble — the classic
   native-forwarder bug double-rendered a reply as both a streaming live
   preview and the committed bubble, which would push a token's bubble count
   to 2.

Per-turn unique tokens (rather than fixed strings) are load-bearing: they make
both the dedup count and the order check unambiguous even when an agent is
chatty, and they survive any harness that splits a turn across multiple
assistant blocks (the count is over bubbles containing the token, not over
bubbles).

The native CLI harnesses (claude-native, codex-native) are not covered here yet
— see the note after ``test_custom_agent_message_render_parity``.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

_TURNS = 5

# A custom openai-agents turn is a single LLM call.
_CUSTOM_TURN_TIMEOUT_MS = 90_000


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send.

    :param page: The Playwright page, on the session's chat surface.
    :param text: The message body to send.
    """
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _ensure_chat_view(page: Page) -> None:
    """Switch a terminal-first (native) session to its chat bubble view.

    Native wrapper sessions default to the terminal view; the chat view
    renders the same transcript as ``message-bubble``s. The toggle only
    exists for terminal-capable sessions, so its absence (a plain SDK
    agent) is a no-op.

    :param page: The Playwright page, on the session's chat surface.
    """
    view_mode = page.get_by_role("group", name="View mode")
    if view_mode.count() == 0:
        return
    chat_button = view_mode.get_by_role("button", name="Chat")
    expect(chat_button).to_be_visible(timeout=30_000)
    chat_button.click()


def _turn_prompt(index: int, user_marker: str, assistant_token: str) -> str:
    """Build turn *index*'s prompt: carry a marker, echo a token verbatim.

    :param index: 1-based turn number, e.g. ``3``.
    :param user_marker: Unique token embedded in the user message itself.
    :param assistant_token: Unique token the agent must echo back exactly.
    :returns: The composed prompt text.
    """
    return (
        f"This is turn {index}. Context marker {user_marker}. "
        f"Reply with exactly this token and nothing else: {assistant_token}"
    )


def _ordered_message_items(base_url: str, session_id: str) -> list[dict[str, object]]:
    """Return the session's user/assistant message items in render order.

    Filters ``GET /v1/sessions/{id}/items`` to ``type == "message"`` items
    with a ``user`` / ``assistant`` role, preserving the server's position
    order — which is exactly the order the SPA renders bubbles and the TUI
    prints lines.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: Ordered message-item dicts (each with ``role`` + ``content``).
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return [
        item
        for item in resp.json().get("data", [])
        if item.get("type") == "message" and item.get("role") in ("user", "assistant")
    ]


def _item_text(item: dict[str, object]) -> str:
    """Join every string ``text`` field across a message item's content blocks.

    Works for both user (``input_text``) and assistant (``output_text`` /
    ``text``) blocks, so the same extractor serves both roles.

    :param item: One element from the items API.
    :returns: The concatenated text, or ``""``.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _ordered_token_sequence(texts: list[str], tokens: list[str]) -> list[str]:
    """Reduce *texts* to the order its members' tokens appear, one per text.

    Each text is expected to carry at most one of *tokens*. A text carrying
    none is skipped; a text carrying a token contributes that token. The
    result is the observed order of tokens — directly comparable to the
    expected per-turn order.

    :param texts: Ordered message texts (e.g. transcript assistant texts).
    :param tokens: The unique per-turn tokens to look for.
    :returns: The tokens in the order they appear across *texts*.
    """
    token_set = set(tokens)
    observed: list[str] = []
    for text in texts:
        hits = [tok for tok in token_set if tok in text]
        observed.extend(hits)
    return observed


def _assert_no_duplicate_render(
    page: Page,
    user_markers: list[str],
    assistant_tokens: list[str],
) -> None:
    """Assert each marker/token renders in exactly one bubble, in order.

    :param page: The Playwright page, on the settled chat surface.
    :param user_markers: Per-turn unique user markers, oldest first.
    :param assistant_tokens: Per-turn unique assistant tokens, oldest first.
    """
    # Exactly one bubble per token — a second bubble means duplicate rendering
    # (e.g. a native live-preview that was never reconciled away).
    for marker in user_markers:
        expect(page.locator(_USER, has_text=marker)).to_have_count(1)
    for token in assistant_tokens:
        expect(page.locator(_ASSISTANT, has_text=token)).to_have_count(1)
    # One user bubble per turn — no phantom/duplicated user echoes.
    expect(page.locator(_USER)).to_have_count(len(user_markers))

    # DOM render order matches the turn order for both roles.
    user_texts = page.locator(_USER).all_inner_texts()
    assert _ordered_token_sequence(user_texts, user_markers) == user_markers, (
        "user bubbles are out of order or a marker rendered more than once"
    )
    assistant_texts = page.locator(_ASSISTANT).all_inner_texts()
    assert _ordered_token_sequence(assistant_texts, assistant_tokens) == assistant_tokens, (
        "assistant bubbles are out of order or a token rendered more than once"
    )


def _assert_transcript_parity(
    base_url: str,
    session_id: str,
    user_markers: list[str],
    assistant_tokens: list[str],
) -> None:
    """Assert the canonical transcript carries the same markers, once, in order.

    This is the "same as the TUI" half: the SPA bubbles (already asserted to
    hold each marker exactly once, in order) match the transcript the TUI also
    renders from.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :param user_markers: Per-turn unique user markers, oldest first.
    :param assistant_tokens: Per-turn unique assistant tokens, oldest first.
    """
    items = _ordered_message_items(base_url, session_id)
    user_texts = [_item_text(it) for it in items if it.get("role") == "user"]
    assistant_texts = [_item_text(it) for it in items if it.get("role") == "assistant"]

    for marker in user_markers:
        hits = sum(marker in t for t in user_texts)
        assert hits == 1, f"user marker {marker!r} appears {hits}x in the transcript, expected 1"
    for token in assistant_tokens:
        hits = sum(token in t for t in assistant_texts)
        assert hits == 1, (
            f"assistant token {token!r} appears {hits}x in the transcript, expected 1"
        )

    assert _ordered_token_sequence(user_texts, user_markers) == user_markers, (
        "transcript user messages are out of turn order"
    )
    assert _ordered_token_sequence(assistant_texts, assistant_tokens) == assistant_tokens, (
        "transcript assistant messages are out of turn order"
    )


def _run_render_parity_journey(
    page: Page,
    base_url: str,
    session_id: str,
    *,
    per_turn_timeout_ms: int,
) -> None:
    """Drive five turns, then assert no-duplicate render + transcript parity.

    :param page: The Playwright page.
    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id to chat in.
    :param per_turn_timeout_ms: How long to wait for each turn to land.
    """
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    user_markers: list[str] = []
    assistant_tokens: list[str] = []
    for index in range(1, _TURNS + 1):
        nonce = uuid.uuid4().hex[:8]
        user_marker = f"usr-{index}-{nonce}"
        assistant_token = f"ast-{index}-{nonce}"
        user_markers.append(user_marker)
        assistant_tokens.append(assistant_token)

        _send(page, _turn_prompt(index, user_marker, assistant_token))
        # The echoed token in an assistant bubble = the turn produced its
        # reply; only producible from this turn's prompt.
        expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
            timeout=per_turn_timeout_ms
        )
        # Turn fully settled before the next send, so any transient live
        # preview has collapsed into the committed bubble (the dedup check
        # below would otherwise race a mid-stream double).
        expect(page.locator(_WORKING)).to_have_count(0, timeout=per_turn_timeout_ms)
        expect(page.locator(_USER)).to_have_count(index, timeout=15_000)

    _assert_no_duplicate_render(page, user_markers, assistant_tokens)
    _assert_transcript_parity(base_url, session_id, user_markers, assistant_tokens)


@pytest.mark.timeout(300)
def test_custom_agent_message_render_parity(
    page: Page,
    custom_agent_session: tuple[str, str],
) -> None:
    base_url, session_id = custom_agent_session
    _run_render_parity_journey(
        page, base_url, session_id, per_turn_timeout_ms=_CUSTOM_TURN_TIMEOUT_MS
    )


# NOTE: the native CLI harnesses (claude-native "Claude Code", codex-native
# "Codex") are intentionally NOT covered here yet. Driving the real vendor TUI
# in a PTY needs CI-side setup (gateway auth + Claude Code first-run state) that
# is owned by the native-harness CI enablement work; until that lands, this
# suite covers the render-parity / no-duplicate logic via the custom
# openai-agents agent above.
