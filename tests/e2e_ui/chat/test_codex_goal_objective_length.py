"""E2E regression: an over-long ``/goal`` must not fail the turn with a raw
Codex app-server injection error.

Codex app-server rejects goal objectives longer than 4000 characters with a
JSON-RPC ``-32600`` error, but Omnigent's ``/goal`` chat-command path sends
the objective without any client-side length check. The rejection then
reaches the user verbatim as an injection failure — an error pill reading
``Codex native executor error: {'code': -32600, 'message': 'goal objective
must be at most 4000 characters'}`` — instead of a clear "your goal is too
long" message or a client-side truncation.

This test drives the real user journey: in a native Codex session, type a
standalone ``/goal`` command whose objective exceeds the cap into the chat
composer and send it. It asserts the length-checked behavior — the raw
app-server payload must never surface in chat — so it fails on the unfixed
build and passes once the objective is validated before the
``thread/goal/set`` request (whether the fix truncates with a marker or
rejects with a clear user-facing message).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.codex_parity.helpers import (
    ev_assistant_message,
    ev_completed,
    ev_response_created,
)
from tests.e2e_ui.conftest import MockedCodexNativeSession
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_NATIVE_CODEX_TIMEOUT_MS = 180_000

# Codex app-server rejects goal objectives above this many characters with a
# raw JSON-RPC -32600 error (verified against codex-cli 0.139.0).
_CODEX_GOAL_OBJECTIVE_LIMIT = 4000

# One sentence repeated past the cap: a realistic over-long goal objective.
_OVERLONG_OBJECTIVE_SENTENCE = "Ship the migration end to end and keep every suite green. "

# Two scripted mock turns: the bootstrap turn that proves the pipeline is
# healthy, and the /goal turn itself so a post-fix truncating implementation
# can complete its model call instead of exhausting the sidecar script.
_RESPONSES = [
    [
        ev_response_created("resp-goal-length-bootstrap"),
        ev_assistant_message("msg-goal-length-bootstrap", "E2E_GOAL_LENGTH_BOOTSTRAP"),
        ev_completed("resp-goal-length-bootstrap"),
    ],
    [
        ev_response_created("resp-goal-length-turn"),
        ev_assistant_message("msg-goal-length-turn", "E2E_GOAL_LENGTH_TURN_DONE"),
        ev_completed("resp-goal-length-turn"),
    ],
]


def _overlong_objective() -> str:
    """Return a realistic goal objective just past the Codex 4000-char cap."""
    repeats = _CODEX_GOAL_OBJECTIVE_LIMIT // len(_OVERLONG_OBJECTIVE_SENTENCE) + 1
    objective = _OVERLONG_OBJECTIVE_SENTENCE * repeats
    assert len(objective) > _CODEX_GOAL_OBJECTIVE_LIMIT
    return objective


# Boots a real native Codex CLI against the prebuilt codex-parity sidecar
# (CI supplies the binary through CODEX_PARITY_SIDECAR_BIN); timeout matches
# the sibling native-Codex goal-mode / render-parity tests.
@pytest.mark.parametrize(
    "mocked_native_codex_session",
    [_RESPONSES],
    indirect=True,
    ids=["overlong-goal"],
)
@pytest.mark.timeout(300)
def test_overlong_goal_command_does_not_surface_raw_injection_error(
    page: Page,
    mocked_native_codex_session: MockedCodexNativeSession,
) -> None:
    """A ``/goal`` past the 4000-char Codex cap must not fail the turn raw."""
    session = mocked_native_codex_session
    page.goto(f"{session.base_url}/c/{session.session_id}")

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # Prove the session is healthy before the oversized goal: one normal
    # mocked turn must complete end to end.
    _send(page, "Bootstrap the goal-length e2e thread.")
    expect(page.locator(_ASSISTANT, has_text="E2E_GOAL_LENGTH_BOOTSTRAP").first).to_be_visible(
        timeout=_NATIVE_CODEX_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_CODEX_TIMEOUT_MS)

    # The reported user journey: a standalone /goal chat command whose
    # objective exceeds the Codex app-server's 4000-character cap.
    _send(page, f"/goal {_overlong_objective()}")

    # The turn must reach some observable outcome: the goal takes effect (a
    # truncating fix shows the goal-mode chip), the turn completes (mocked
    # assistant reply), or feedback appears as an error pill — which on the
    # unfixed build carries the raw app-server payload.
    outcome = (
        page.get_by_test_id("error-pill")
        .or_(page.get_by_test_id("composer-goal-mode"))
        .or_(page.locator(_ASSISTANT, has_text="E2E_GOAL_LENGTH_TURN_DONE"))
    )
    expect(outcome.first).to_be_visible(timeout=_NATIVE_CODEX_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_CODEX_TIMEOUT_MS)

    # Expand any error pill so its full detail is on screen (a failing run's
    # recording then shows the raw payload; harmless for a post-fix pill that
    # carries a clear client-side message instead).
    error_pill = page.get_by_test_id("error-pill")
    if error_pill.count() > 0:
        error_pill.first.click()

    # The bug itself: the app-server rejection reaches the user verbatim as a
    # raw injection failure. Neither the executor passthrough prefix nor the
    # raw JSON-RPC payload may appear anywhere in the chat.
    expect(page.get_by_text("Codex native executor error")).to_have_count(0)
    expect(page.get_by_text("-32600")).to_have_count(0)
