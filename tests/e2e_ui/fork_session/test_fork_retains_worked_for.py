"""Browser e2e: forking must retain historical "Worked for" durations.

Forking a conversation could reset a historical response's "Worked for"
duration to 1s: the server-side fork copy
(``SqlAlchemyConversationStore._fork_conversation_with_id``) stamped
every copied item's ``created_at`` with the fork creation time, so the
SPA's reloaded-history duration (``turnWorkedForS``: last block's
``createdAtS`` minus first block's) collapsed to 0 and
``formatWorkedFor`` clamped it to "1s".

The journey mirrors the report:

1. Complete a turn with intermediate assistant activity (a tool call)
   and a final answer several seconds later (mock-LLM ``delay``).
2. Reload the conversation and note its "Worked for" duration — the
   control assertion proves the source renders a real multi-second span
   from stored timestamps.
3. Fork the conversation from that response (the same copy path serves
   full and truncated forks) and reload the fork.
4. The fork's copy of the same turn must retain the source duration —
   with the bug it shows "Worked for 1s", so this test fails until the
   copy path preserves item timestamps.

Runs fully in the e2e_ui harness (hello_world agent + mock LLM); the
tool call executes on the spawned runner via ``sys_os_shell``.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_FOLD = '[data-testid="turn-worked-fold"]'

# Unique marker routed to a content-matched mock queue, so no other LLM
# traffic (other tests' turns, fallback consumers) can pop this turn's
# scripted responses out of order.
_PROMPT_MARKER = "fork-workedfor-run"
_DONE_SENTINEL = "fork-workedfor-done"

# LLM "thinking" time before the final answer. Large enough that the
# stored first→last item span is unambiguously multi-second even with
# whole-second timestamp rounding, small enough to keep the test fast.
_FINAL_DELAY_S = 4.0

_WORKED_RE = re.compile(r"Worked for\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?")


def _worked_for_seconds(fold_text: str) -> int:
    """Parse a fold label ("Worked for 4s", "1m 46s", "1h 2m") to seconds.

    :param fold_text: The fold's rendered inner text.
    :returns: Total seconds shown in the label.
    :raises AssertionError: If the text carries no "Worked for" duration.
    """
    match = _WORKED_RE.search(fold_text)
    assert match is not None, f"no 'Worked for' duration in fold text: {fold_text!r}"
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    assert total > 0, f"unparseable 'Worked for' duration in fold text: {fold_text!r}"
    return total


def test_fork_retains_worked_for_duration(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A forked conversation keeps the source turn's "Worked for" duration.

    Failure modes this catches:

    - The fork copy path re-stamps copied items' ``created_at`` with the
      fork time: the fork's reloaded duration collapses to the clamped
      "1s" while the source shows the real span.
    - Any future regression that drops or zeroes item timestamps on the
      fork copy path, since the assertion pins the fork's rendered
      duration to the source's.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` session.
    :param mock_llm_server_url: Mock LLM server to script the turn on.
    """
    base_url, session_id = seeded_session

    # One turn: a real tool round (intermediate assistant activity, so
    # the settled turn folds behind "Worked for"), then a final answer
    # delayed several seconds so the stored item span is multi-second.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_fork_workedfor",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": "echo fork-workedfor-marker"}),
                    }
                ]
            },
            {"text": _DONE_SENTINEL, "delay": _FINAL_DELAY_S},
        ],
        key="fork_worked_for",
        match=_PROMPT_MARKER,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()

    composer.fill(f"Run the marker command, then reply done. ({_PROMPT_MARKER})")
    page.get_by_role("button", name="Send", exact=True).click()

    assistant = page.locator(_ASSISTANT)
    expect(assistant.filter(has_text=_DONE_SENTINEL).first).to_be_visible(timeout=60_000)

    # Reload so the turn renders from stored server timestamps
    # (``createdAtS``) — the same clock the fork's copy renders from —
    # rather than live page-relative stamps.
    page.reload()
    source_fold = page.locator(_FOLD).first
    expect(source_fold).to_be_visible(timeout=30_000)
    expect(source_fold).to_contain_text("Worked for")
    source_seconds = _worked_for_seconds(source_fold.inner_text())

    # Control: the source itself must show the real multi-second span.
    # If this fails the harness never produced a measurable duration and
    # the fork assertion below would be vacuous.
    assert source_seconds >= 2, (
        f"source turn shows 'Worked for {source_seconds}s' after reload; expected >= 2s "
        f"(the scripted final answer was delayed {_FINAL_DELAY_S}s)"
    )

    # Fork from the turn's response — the last (only) response, so the
    # "truncated" fork copies everything: the shared copy path under test.
    bubble = assistant.filter(has_text=_DONE_SENTINEL).first
    bubble.hover()
    bubble.get_by_test_id("fork-from-response").click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    page.get_by_test_id("fork-session-submit").click()

    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()

    # Render the fork purely from its stored (copied) items.
    page.reload()
    fork_fold = page.locator(_FOLD).first
    expect(fork_fold).to_be_visible(timeout=30_000)
    expect(fork_fold).to_contain_text("Worked for")
    fork_text = fork_fold.inner_text()
    fork_seconds = _worked_for_seconds(fork_text)

    # The copied history must retain the original duration. With the bug,
    # every copied item shares the fork's creation timestamp, so the span
    # is 0 and the label clamps to "Worked for 1s".
    assert fork_seconds >= source_seconds - 1, (
        f"fork shows {fork_text.strip()!r} for a turn the source shows as "
        f"'Worked for {source_seconds}s' — forking reset the historical response "
        f"duration (copied items re-stamped with the fork's creation time)"
    )
