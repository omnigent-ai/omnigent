r"""UI journey: a composer ``!`` shell exec keeps the native chat timeline intact.

On a claude-native session, running a bang command from the web composer after
an ordinary text turn must leave the abstracted chat view in send order: the
prior assistant reply stays its own visible bubble (not regrouped under a
collapsed "Worked for" row), the bang lands exactly once as a user bubble, the
exec's command and output cards render below that bubble and stay visible, and
the follow-up reply stays a visible bubble below the bang. A second bang must
keep that shape.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import _ASSISTANT, _WORKING, _ensure_chat_view, _item_text, _send
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_TERMINAL_CARD = '[data-testid="terminal-command-card"]'
_FEED_ENTRY = f'[data-testid="message-bubble"], {_TERMINAL_CARD}'

# Canonical reconciliation regroups the feed a beat after the exec cards first
# render, so feed snapshots wait until it stops changing rather than sleeping a
# fixed interval a slow runner could outlast.
_FEED_SETTLE_TIMEOUT_MS = 8_000
_FEED_SETTLE_QUIET_MS = 600


def _set_reply(mock_url: str, token: str) -> None:
    """Make the mock answer every model request with *token*."""
    set_fallback_mock_llm(mock_url, "default", token)
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, token)


def _feed_entries(page: Page) -> list[tuple[str, str]]:
    """Snapshot the visible chat feed's bubbles and exec cards in document order.

    :param page: The Playwright page, on the session's chat surface.
    :returns: Ordered ``(kind, text)`` pairs, e.g. ``("message:user", "! echo x")``
        or ``("terminal:output", "output")``. Hidden elements are skipped.
    """
    entries: list[tuple[str, str]] = []
    for element in page.locator(_FEED_ENTRY).all():
        if not element.is_visible():
            continue
        if element.get_attribute("data-testid") == "message-bubble":
            kind = f"message:{element.get_attribute('data-role')}"
        else:
            kind = f"terminal:{element.get_attribute('data-terminal-kind')}"
        text = " ".join((element.inner_text() or "").split())
        entries.append((kind, text[:120]))
    return entries


def _settled_feed(page: Page) -> list[tuple[str, str]]:
    """Return the feed once it stops changing, after canonical reconciliation.

    Polls :func:`_feed_entries` until two reads spanning ``_FEED_SETTLE_QUIET_MS``
    agree, bounded by ``_FEED_SETTLE_TIMEOUT_MS``. On timeout it returns the last
    snapshot, so a stalled reconciliation still surfaces as a timeline problem.
    """
    deadline = time.monotonic() + _FEED_SETTLE_TIMEOUT_MS / 1000
    previous = _feed_entries(page)
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        page.wait_for_timeout(100)
        current = _feed_entries(page)
        if current != previous:
            previous, stable_since = current, time.monotonic()
        elif (time.monotonic() - stable_since) * 1000 >= _FEED_SETTLE_QUIET_MS:
            return current
    return previous


def _canonical_items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Fetch the session's canonical transcript items in server order."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return list(resp.json().get("data", []))


def _summarize(items: list[dict[str, Any]]) -> str:
    """Render canonical items as one ``type[:role] text`` line each."""
    lines: list[str] = []
    for item in items:
        kind = str(item.get("type"))
        if isinstance(item.get("role"), str):
            kind += f":{item['role']}"
        if kind == "terminal_command":
            kind += f":{item.get('kind')}"
        text = " ".join(_item_text(item).split())[:80]
        lines.append(f"{kind} {text}".rstrip())
    return "\n".join(lines)


def _run_bang(page: Page, probe: str, *, reply: str) -> str:
    """Send ``! echo <probe>`` from the composer and wait for the turn to settle.

    :param reply: Token of the follow-up assistant reply the mock answers with;
        the turn has not settled until it is on screen.
    :returns: The exact composer text, which the user bubble must echo.
    """
    command = f"! echo {probe}"
    _send(page, command)
    input_card = page.locator(f'{_TERMINAL_CARD}[data-terminal-kind="input"]', has_text=probe)
    expect(input_card.first).to_be_attached(timeout=_MOCK_TURN_TIMEOUT_MS)
    expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    return command


def _timeline_problems(
    entries: list[tuple[str, str]],
    *,
    prior_reply_token: str,
    follow_up_token: str,
    command: str,
    probe: str,
    label: str,
) -> list[str]:
    """Describe every way the feed around one bang exec is out of send order.

    :param entries: Feed snapshot from :func:`_feed_entries`.
    :param prior_reply_token: Token of the assistant reply that settled before this bang.
    :param follow_up_token: Token of the assistant reply to this bang's exec output.
    :param command: The exact composer text of the bang, e.g. ``"! echo probe"``.
    :param probe: The unique echo argument identifying this exec's cards.
    :param label: Prefix for the problem descriptions, e.g. ``"first bang"``.
    :returns: Human-readable problems; empty when the timeline is intact.
    """
    problems: list[str] = []
    exec_index = next(
        (
            i
            for i, (kind, text) in enumerate(entries)
            if kind == "terminal:input" and probe in text
        ),
        None,
    )
    if exec_index is None:
        problems.append(f"{label}: the exec's command card is not visible in the feed")
    # The mock exec's output card renders a fixed stdout string, not the probe,
    # so it is matched by position: the first output card below the
    # probe-matched command card.
    output_index = (
        next(
            (
                i
                for i, (kind, _text) in enumerate(entries)
                if kind == "terminal:output" and i > exec_index
            ),
            None,
        )
        if exec_index is not None
        else None
    )
    if output_index is None:
        problems.append(f"{label}: the exec's output card is not visible below its command card")
    bang_indexes = [
        i for i, (kind, text) in enumerate(entries) if kind == "message:user" and command in text
    ]
    if len(bang_indexes) != 1:
        problems.append(
            f"{label}: expected exactly one bang user bubble, found {len(bang_indexes)}"
        )
    bang_index = bang_indexes[0] if bang_indexes else None
    if bang_index is not None and exec_index is not None and bang_index > exec_index:
        problems.append(
            f"{label}: bang bubble renders below its command card "
            f"(bubble at {bang_index}, card at {exec_index})"
        )
    if bang_index is not None and bang_index == len(entries) - 1:
        problems.append(f"{label}: bang bubble is the last entry of the feed (nothing below it)")
    prior = next(
        (
            (i, text)
            for i, (kind, text) in enumerate(entries)
            if kind == "message:assistant" and prior_reply_token in text
        ),
        None,
    )
    if prior is None:
        problems.append(
            f"{label}: the prior assistant reply {prior_reply_token!r} is gone from the feed"
        )
    else:
        prior_index, prior_text = prior
        if bang_index is not None and prior_index > bang_index:
            problems.append(
                f"{label}: the prior assistant reply renders below the bang bubble "
                f"(reply at {prior_index}, bubble at {bang_index})"
            )
        if "Worked for" in prior_text:
            problems.append(
                f"{label}: the prior assistant reply was regrouped under a collapsed "
                f"'Worked for' row: {prior_text!r}"
            )
    follow_up_index = next(
        (
            i
            for i, (kind, text) in enumerate(entries)
            if kind == "message:assistant" and follow_up_token in text
        ),
        None,
    )
    if follow_up_index is None:
        problems.append(
            f"{label}: the follow-up reply {follow_up_token!r} is not visible in the feed"
        )
    elif bang_index is not None and follow_up_index < bang_index:
        # The reply bubble wraps the exec cards, so its feed entry precedes the
        # standalone card entries; only its position relative to the bang bubble
        # is meaningful here.
        problems.append(
            f"{label}: the follow-up reply renders above the bang bubble "
            f"(reply at {follow_up_index}, bubble at {bang_index})"
        )
    return problems


def _canonical_problems(
    items: list[dict[str, Any]], *, command: str, probe: str, label: str
) -> list[str]:
    """Check that the bang is a canonical user message sitting right before its own exec.

    :param items: Canonical items from :func:`_canonical_items`.
    :param command: The exact composer text of the bang, e.g. ``"! echo probe"``.
    :param probe: The unique echo argument identifying this exec's command item.
    :param label: Prefix for the problem descriptions, e.g. ``"first bang"``.
    :returns: Human-readable problems; empty when the transcript is intact.
    """
    bang_user = [
        i
        for i, item in enumerate(items)
        if item.get("type") == "message"
        and item.get("role") == "user"
        and command in _item_text(item)
    ]
    exec_inputs = [
        i
        for i, item in enumerate(items)
        if item.get("type") == "terminal_command"
        and item.get("kind") == "input"
        and probe in str(item.get("input") or "")
    ]
    if len(bang_user) != 1:
        return [
            f"{label}: the bang landed {len(bang_user)} times as a canonical user message "
            "(expected exactly once; it is the turn boundary for the exec)"
        ]
    if len(exec_inputs) != 1:
        return [
            f"{label}: the exec's command landed {len(exec_inputs)} times as a canonical "
            "terminal_command input item (expected exactly once)"
        ]
    bang_index, exec_index = bang_user[0], exec_inputs[0]
    if bang_index > exec_index:
        return [
            f"{label}: the canonical bang user message lands after its exec's command item "
            f"(message at {bang_index}, command item at {exec_index})"
        ]
    # Resource bookkeeping is not part of the conversation order.
    between = [
        str(items[i].get("type"))
        for i in range(bang_index + 1, exec_index)
        if items[i].get("type") != "resource_event"
    ]
    if between:
        return [
            f"{label}: {between} separate the canonical bang user message (at {bang_index}) "
            f"from its exec's command item (at {exec_index})"
        ]
    return []


def _format_feed(entries: list[tuple[str, str]]) -> str:
    return "\n".join(f"{i}: {kind} {text}" for i, (kind, text) in enumerate(entries))


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_native_claude_bang_exec_keeps_chat_timeline(
    request: pytest.FixtureRequest,
    context: BrowserContext,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A bang exec leaves the prior reply alone and the feed in send order.

    ``context`` is requested up front only so the suite can stop its recording
    when the body ends; the page (and thus the footage) starts after setup.
    """
    base_url, session_id = native_claude_mock_session
    _log.info("bang-timeline journey: base_url=%s session_id=%s", base_url, session_id)
    reset_mock_llm(mock_llm_server_url)
    shots = Path(str(request.config.getoption("--output"))) / "bang-timeline"
    shots.mkdir(parents=True, exist_ok=True)

    page: Page = request.getfixturevalue("page")
    assert page.context is context
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    nonce = uuid.uuid4().hex[:8]
    first_reply = f"ast-{nonce}"
    _set_reply(mock_llm_server_url, first_reply)
    _send(page, "test")
    expect(page.locator(_ASSISTANT, has_text=first_reply).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    page.screenshot(path=str(shots / "1-after-text-turn.png"))

    problems: list[str] = []

    follow_up_1 = f"after-bang-1-{nonce}"
    _set_reply(mock_llm_server_url, follow_up_1)
    first_probe = f"bang-order-{nonce}"
    first_command = _run_bang(page, first_probe, reply=follow_up_1)
    first_feed = _settled_feed(page)
    page.screenshot(path=str(shots / "2-after-first-bang.png"))
    _log.info("feed after first bang:\n%s", _format_feed(first_feed))
    problems += _timeline_problems(
        first_feed,
        prior_reply_token=first_reply,
        follow_up_token=follow_up_1,
        command=first_command,
        probe=first_probe,
        label="first bang",
    )

    follow_up_2 = f"after-bang-2-{nonce}"
    _set_reply(mock_llm_server_url, follow_up_2)
    second_probe = f"bang-second-{nonce}"
    second_command = _run_bang(page, second_probe, reply=follow_up_2)
    second_feed = _settled_feed(page)
    page.screenshot(path=str(shots / "3-after-second-bang.png"))
    _log.info("feed after second bang:\n%s", _format_feed(second_feed))
    problems += _timeline_problems(
        second_feed,
        prior_reply_token=follow_up_1,
        follow_up_token=follow_up_2,
        command=second_command,
        probe=second_probe,
        label="second bang",
    )

    items = _canonical_items(base_url, session_id)
    problems += _canonical_problems(
        items, command=first_command, probe=first_probe, label="first bang"
    )
    problems += _canonical_problems(
        items, command=second_command, probe=second_probe, label="second bang"
    )

    assert not problems, (
        "chat timeline corrupted after bang exec:\n- "
        + "\n- ".join(problems)
        + f"\n\nfeed after first bang:\n{_format_feed(first_feed)}"
        + f"\n\nfeed after second bang:\n{_format_feed(second_feed)}"
        + f"\n\ncanonical items:\n{_summarize(items)}"
    )
