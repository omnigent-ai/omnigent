"""Browser e2e: forking from the MIDDLE of a conversation truncates history.

The "Fork from here" per-message action passes that response's id as
``up_to_response_id``, so the server deep-copies the transcript only up to
and including the selected turn. This test drives the real chain — two
marked turns → fork from the FIRST assistant's action → navigate into the
clone — and asserts truncation two independent ways:

1. The rendered fork transcript shows the pre-fork (kept) user turn and
   NOT the post-fork (dropped) one — the DOM proves the server copied a
   truncated item list, not the whole conversation.
2. Asking the clone "what did I ask you" surfaces only the kept code word.
   The SDK harness replays the copied Omnigent transcript as context, so a
   reply that recalls the dropped word would mean the truncation point was
   ignored and the full history leaked into the fork.

Both source and fork run the seeded ``hello_world`` (openai-agents SDK)
agent, so this is fully runnable in the e2e_ui harness (no host / native
CLI needed).
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

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
    runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Fork from the first turn — the clone carries only history up to it.

    Failure modes this catches:

    - ``up_to_response_id`` is dropped on the wire or ignored server-side
      (the clone renders the dropped turn too — full-history copy).
    - The fork action on a non-last message forks the WHOLE session (the
      per-message action wires the wrong response id).
    - The truncated transcript hydrates but the SDK replay still feeds the
      dropped turn to the model (recall surfaces the dropped word).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` session.
    :param runner_id: The shared spawned runner's id — the fork is bound to
        it before the recall turn (the non-coding fork flow creates the
        clone unbound, so a message would otherwise have no runner).
    """
    base_url, session_id = seeded_session

    # Content-route the recall turn so the mock echoes the kept marker.
    # Turns 1 & 2 ("reply with just OK") hit the generic fallback ("Mock LLM
    # response.") which is enough for the fork-point anchor assertions.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _KEPT_MARKER}],
        key="fork-recall",
        match="What code word did I ask you to remember",
    )

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()

    # Turn 1 (KEPT): plant the first code word and wait for its reply so the
    # fork has a committed assistant response to anchor the action on.
    composer.fill(f"Remember this code word and reply with just OK: {_KEPT_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator(_ASSISTANT)
    expect(assistant).to_have_count(1, timeout=60_000)

    # Turn 2 (DROPPED): plant the second code word AFTER the fork point.
    # Forking from turn 1's response must exclude this turn entirely.
    composer.fill(f"Now also remember this code word and reply with just OK: {_DROPPED_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(assistant).to_have_count(2, timeout=60_000)

    # Fork from the FIRST assistant response (the middle of the now
    # two-turn conversation). The action lives inside that bubble's action
    # bar (dimmed until hover but clickable); scoping the locator to the
    # first bubble guarantees we pass turn 1's response id, not turn 2's.
    first_assistant = assistant.nth(0)
    first_assistant.hover()
    first_assistant.get_by_test_id("fork-from-response").click()

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
        re.compile(rf"/c/(?!{re.escape(session_id)})conv_[0-9a-f]+"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # (1) DOM truncation: the kept user turn is present, the dropped one is
    # absent. to_have_count(0) retries, so this holds even if the dropped
    # bubble would only ever appear via a (buggy) full-history copy.
    expect(page.locator(_USER).filter(has_text=_KEPT_MARKER).first).to_be_visible(timeout=30_000)
    expect(page.locator(_USER).filter(has_text=_DROPPED_MARKER)).to_have_count(0)

    # The non-coding fork is created UNBOUND (the dialog only launches a
    # runner for a coding source), so a recall turn would have nothing to
    # dispatch to. Bind it to the shared runner — same PATCH the
    # ``seeded_session`` fixture uses — then reload so the client picks up
    # the binding before sending.
    bind = httpx.patch(
        f"{base_url}/v1/sessions/{fork_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    bind.raise_for_status()
    page.reload()

    # (2) Recall: ask the clone what it was told to remember. The reply must
    # echo the kept word and never the dropped one. The copied OK reply is
    # the only assistant bubble so far; the recall answer is the second.
    fork_composer = page.get_by_placeholder("Ask the agent anything…")
    expect(fork_composer).to_be_visible()
    fork_composer.fill("What code word did I ask you to remember? Reply with the code word only.")
    page.get_by_role("button", name="Send", exact=True).click()
    fork_assistant = page.locator(_ASSISTANT)
    expect(fork_assistant).to_have_count(2, timeout=60_000)

    recall = fork_assistant.nth(1)
    expect(recall).to_contain_text(_KEPT_MARKER, timeout=60_000)
    expect(recall).not_to_contain_text(_DROPPED_MARKER)
