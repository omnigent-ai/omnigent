r"""UI journey: a composer ``!`` shell exec keeps the native chat timeline intact.

On a claude-native session, running a bang command from the web composer after
an ordinary text turn must not corrupt the abstracted chat view: the prior
turn's assistant reply stays its own turn (not regrouped under a collapsed
'Worked for' section), the bang lands as its own user bubble above the exec's
terminal-command cards, and those cards stay visible in the feed. Claude Code
persists the bang only as ``<bash-*>`` records and then starts a genuine model
turn on the output, so the mirror must echo the bang as a canonical user
message — that echo is the turn boundary everything above depends on.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import _ASSISTANT, _WORKING, _ensure_chat_view, _item_text, _send
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

_TERMINAL_CARD = '[data-testid="terminal-command-card"]'
_FEED_ENTRY = f'[data-testid="message-bubble"], {_TERMINAL_CARD}'

_BANG_PROBE = "echo bang-order-probe"
_BANG_COMMAND = f"! {_BANG_PROBE}"

# Canonical reconciliation regroups the feed a beat after the exec cards
# first render, so the settled state needs a short wait.
_RECONCILE_SETTLE_MS = 2_000


def _feed_entries(page: Page) -> list[tuple[str, str]]:
    """Snapshot the chat feed's bubbles and exec cards in document order.

    :param page: The Playwright page, on the session's chat surface.
    :returns: Ordered ``(kind, text)`` pairs, e.g. ``("message:user", "! env")``
        or ``("terminal:output", "output")``.
    """
    entries: list[tuple[str, str]] = []
    for element in page.locator(_FEED_ENTRY).all():
        if element.get_attribute("data-testid") == "message-bubble":
            kind = f"message:{element.get_attribute('data-role')}"
        else:
            kind = f"terminal:{element.get_attribute('data-terminal-kind')}"
        text = " ".join((element.inner_text() or "").split())
        entries.append((kind, text[:100]))
    return entries


def _canonical_items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Fetch the session's canonical transcript items in server order.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The raw item dicts from ``GET /v1/sessions/{id}/items``.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return list(resp.json().get("data", []))


def _summarize(items: list[dict[str, Any]]) -> str:
    """Render canonical items as one ``type[:role] text`` line each.

    :param items: Item dicts from :func:`_canonical_items`.
    :returns: The joined summary block for a failure message.
    """
    lines: list[str] = []
    for item in items:
        kind = str(item.get("type"))
        if isinstance(item.get("role"), str):
            kind += f":{item['role']}"
        text = " ".join(_item_text(item).split())[:80]
        lines.append(f"{kind} {text}".rstrip())
    return "\n".join(lines)


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_native_claude_bang_exec_keeps_chat_timeline(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A bang exec leaves the prior reply alone and the feed in send order."""
    base_url, session_id = native_claude_mock_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    reset_mock_llm(mock_llm_server_url)
    token = f"ast-{uuid.uuid4().hex[:8]}"
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)

    _send(page, f"Reply with exactly this token and nothing else: {token}")
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    _send(page, _BANG_COMMAND)
    input_card = page.locator(f'{_TERMINAL_CARD}[data-terminal-kind="input"]')
    expect(input_card.first).to_be_visible(timeout=_MOCK_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    page.wait_for_timeout(_RECONCILE_SETTLE_MS)

    problems: list[str] = []
    entries = _feed_entries(page)

    exec_index = next(
        (
            index
            for index, (kind, text) in enumerate(entries)
            if kind == "terminal:input" and _BANG_PROBE in text
        ),
        None,
    )
    if exec_index is None:
        problems.append(
            "the exec's terminal-command card is no longer in the feed "
            "(swallowed into a collapsed steps group)"
        )

    bang_indexes = [
        index
        for index, (kind, text) in enumerate(entries)
        if kind == "message:user" and _BANG_COMMAND in text
    ]
    if len(bang_indexes) != 1:
        problems.append(
            f"expected exactly one bang prompt bubble, found {len(bang_indexes)} "
            "(the optimistic bubble did not reconcile with the mirrored message)"
        )
    bang_index = bang_indexes[0] if bang_indexes else None
    if bang_index is not None and exec_index is not None and bang_index > exec_index:
        problems.append(
            "bang prompt bubble renders below its exec card "
            f"(bubble at feed index {bang_index}, exec card at {exec_index})"
        )

    prior_reply = next(
        (
            (index, text)
            for index, (kind, text) in enumerate(entries)
            if kind == "message:assistant" and token in text
        ),
        None,
    )
    if prior_reply is None:
        problems.append("the prior turn's assistant reply is gone from the feed")
    else:
        reply_index, reply_text = prior_reply
        if bang_index is not None and reply_index > bang_index:
            problems.append(
                "the prior turn's assistant reply renders below the bang bubble "
                f"(reply at feed index {reply_index}, bubble at {bang_index})"
            )
        if "Worked for" in reply_text:
            problems.append(
                "the prior turn's assistant reply was regrouped under a collapsed "
                f"'Worked for' section: {reply_text!r}"
            )

    # Canonical shape. Claude Code starts a REAL model turn on the bash
    # output (the mock's fallback answers it with the same token), so the
    # durable regression anchor is the mirrored bang user message sitting
    # between the prior reply and the exec's terminal items — not a count
    # of token replies.
    items = _canonical_items(base_url, session_id)
    bang_user_indexes = [
        index
        for index, item in enumerate(items)
        if item.get("type") == "message"
        and item.get("role") == "user"
        and _BANG_COMMAND in _item_text(item)
    ]
    terminal_indexes = [
        index for index, item in enumerate(items) if item.get("type") == "terminal_command"
    ]
    first_reply_index = next(
        (
            index
            for index, item in enumerate(items)
            if item.get("type") == "message"
            and item.get("role") == "assistant"
            and token in _item_text(item)
        ),
        None,
    )
    if len(bang_user_indexes) != 1:
        problems.append(
            f"the bang landed {len(bang_user_indexes)} times as a canonical user "
            "message (expected exactly once — it is the turn boundary that keeps "
            "the prior reply out of the exec's group)"
        )
    elif not terminal_indexes:
        problems.append("no terminal_command items reached the canonical transcript")
    else:
        if bang_user_indexes[0] > min(terminal_indexes):
            problems.append(
                "the canonical bang user message lands after the exec's "
                f"terminal items (message at {bang_user_indexes[0]}, first "
                f"terminal item at {min(terminal_indexes)})"
            )
        if first_reply_index is not None and first_reply_index > bang_user_indexes[0]:
            problems.append(
                "the prior turn's reply lands after the bang in the canonical "
                f"transcript (reply at {first_reply_index}, bang at "
                f"{bang_user_indexes[0]})"
            )

    feed = "\n".join(f"{index}: {kind} {text}" for index, (kind, text) in enumerate(entries))
    assert not problems, (
        "chat timeline corrupted after bang exec:\n- "
        + "\n- ".join(problems)
        + f"\n\nrendered feed:\n{feed}\n\ncanonical items:\n{_summarize(items)}"
    )
