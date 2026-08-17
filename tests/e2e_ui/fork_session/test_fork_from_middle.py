"""Browser e2e: forking from the MIDDLE of a conversation truncates history.

The "Fork from here" user-message action passes that prompt's response id as
``up_to_response_id``, so the server deep-copies the transcript only up to
and including the selected prompt. This test drives the real chain — two
marked turns → fork from the FIRST user message's action → navigate into the
clone — and asserts the exact truncation boundary in the rendered history:

1. The rendered fork transcript shows the selected prompt, but neither its
   reply nor the later turn — the DOM proves the server honored the user's
   distinct anchor instead of the assistant response id.

Both source and fork run the seeded ``hello_world`` (openai-agents SDK)
agent, so this is fully runnable in the e2e_ui harness (no host / native
CLI needed).
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from omnigent.entities import MessageData, NewConversationItem
from tests.e2e_ui.conftest import seed_committed_items

# Two distinct code words with no shared substring, so the kept/dropped
# assertions can't satisfy each other. Only the KEPT word is part of the
# turn the fork copies; the DROPPED word lives in the turn after the fork
# point and must never reach the clone.
_KEPT_MARKER = "zephyr-keepsake"
_DROPPED_MARKER = "quasar-castoff"

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'


def test_fork_from_middle_truncates_history(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Fork from the first prompt — its reply and later history are excluded.

    Failure modes this catches:

    - ``up_to_response_id`` is dropped on the wire or ignored server-side
      (the clone renders the dropped turn too — full-history copy).
    - The user action sends the assistant response id instead of the prompt's
      own id (the first reply appears in the clone).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` session.
    """
    base_url, session_id = seeded_session

    # Seed the same distinct turn_*/resp_* shape produced by the live web path.
    # Direct persistence makes the truncation test deterministic while still
    # exercising real history hydration, UI state, HTTP, and store slicing.
    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="turn_user_1",
                data=MessageData(
                    role="user",
                    content=[
                        {
                            "type": "input_text",
                            "text": (
                                f"Remember this code word and reply with just OK: {_KEPT_MARKER}"
                            ),
                        }
                    ],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_assistant_1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "OK"}],
                    agent="hello_world",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="turn_user_2",
                data=MessageData(
                    role="user",
                    content=[
                        {
                            "type": "input_text",
                            "text": (
                                f"Now also remember this code word and reply with just OK: "
                                f"{_DROPPED_MARKER}"
                            ),
                        }
                    ],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_assistant_2",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "OK"}],
                    agent="hello_world",
                ),
            ),
        ],
    )

    page.goto(f"{base_url}/c/{session_id}")

    assistant = page.locator(_ASSISTANT)
    expect(assistant).to_have_count(2, timeout=60_000)

    # Fork from the FIRST user message (the middle of the now two-turn
    # conversation). Its action must pass the prompt's turn_* id, not the
    # adjacent assistant bubble's resp_* id.
    first_user = page.locator(_USER).nth(0)
    first_user.hover()
    first_user.get_by_test_id("fork-from-user-message").click()

    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    # The truncated-fork title (distinct from the full-clone "Clone session")
    # confirms the dialog received an up_to_response_id.
    expect(dialog.get_by_text("Fork from this response")).to_be_visible()
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone")
    submit.click()

    # Land in a DIFFERENT session — a URL still on the source means
    # navigation never fired; a visible dialog means the fork call failed.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # DOM truncation: the selected prompt is present, but its assistant
    # reply and the later turn are absent. A copied first reply means the UI
    # sent the assistant anchor (or an imported/shared-id cutoff) instead.
    expect(page.locator(_USER).filter(has_text=_KEPT_MARKER).first).to_be_visible(timeout=30_000)
    expect(page.locator(_ASSISTANT)).to_have_count(0)
    expect(page.locator(_USER).filter(has_text=_DROPPED_MARKER)).to_have_count(0)
