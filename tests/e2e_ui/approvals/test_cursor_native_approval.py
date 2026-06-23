"""E2E (UI): a cursor-native tool-approval card renders BELOW its user message.

cursor-native is terminal-first and never emits a turn lifecycle event
(``response_created``), so its approval card (``phase == "pre_tool_use"``) had no
turn to anchor to and rendered ABOVE the message that triggered it in the LIVE
stream — correct only after a page reload. This drives the real browser: send a
gated command through the web composer, wait for the ``ApprovalCard``
(``ap-web/src/components/blocks/ApprovalCard.tsx``), and assert it sits below
the user message, matching the reload layout. The regression guard for the
blockStream "standalone bubble for a no-active-turn elicitation" +
``reorderCommittedRequestElicitations`` fix.

Gated like the cursor-native render-parity suite: needs a logged-in
``cursor-agent`` and ``tmux``; skipped (not failed) otherwise — CI does not
provision a Cursor account.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.messages.test_message_render_parity import _USER, _ensure_chat_view, _send

_APPROVAL_CARD = '[data-testid="approval-card"]'
# A cursor-native turn is a full agent loop (cold start + real Cursor backend),
# so allow well past the streaming default.
_AGENT_TURN_TIMEOUT_MS = 180_000


def _cursor_unavailable_reason() -> str | None:
    """Skip reason when cursor-native prerequisites are absent.

    Mirrors the render-parity suite's gate: cursor-native needs the
    ``cursor-agent`` binary, ``tmux``, and a usable Cursor login (ambient
    ``CURSOR_API_KEY`` or a prior ``cursor-agent login`` under ``$HOME/.cursor``).

    :returns: A human-readable skip reason, or ``None`` when all are present.
    """
    if shutil.which("cursor-agent") is None:
        return "cursor-native approval test needs the `cursor-agent` binary on PATH."
    if shutil.which("tmux") is None:
        return "cursor-native approval test needs `tmux` on PATH (runner-owned TUI pane)."
    if not (bool(os.environ.get("CURSOR_API_KEY")) or (Path.home() / ".cursor").is_dir()):
        return (
            "cursor-native approval test needs a Cursor login: export CURSOR_API_KEY "
            "or run `cursor-agent login` (state under $HOME/.cursor). Skipped (not "
            "failed) because CI does not provision a Cursor account by default."
        )
    return None


pytestmark = pytest.mark.skipif(
    _cursor_unavailable_reason() is not None,
    reason=_cursor_unavailable_reason() or "",
)


@pytest.mark.nightly
@pytest.mark.timeout(900)
def test_cursor_native_approval_card_renders_below_user_message(
    page: Page,
    native_cursor_approval_session: tuple[str, str],
) -> None:
    """Bug 2 regression: the approval card renders BELOW the triggering message.

    Without ``-f``, ``cursor-agent`` prompts before running a tool, and a shell
    redirect can never be auto-approved by Cursor's backend — so it reliably
    raises the prompt, which the runner-side mirror surfaces as an
    ``ApprovalCard``. The card must sit visually below the user message that
    triggered it (greater ``y``); before the fix it rendered above in the live
    stream and only corrected on reload.
    """
    base_url, session_id = native_cursor_approval_session
    page.goto(f"{base_url}/c/{session_id}")

    # Terminal-first sessions default to the Terminal view; the bubbles + card
    # live in the Chat view.
    _ensure_chat_view(page)

    # Send (web composer → injected into the Cursor TUI) a command Cursor cannot
    # auto-run; the first turn also dismisses the one-time workspace-trust modal.
    _send(
        page,
        "Run exactly this shell command and nothing else, do not explain: "
        "echo it-works > approval_test.txt",
    )

    user_message = page.locator(_USER).last
    expect(user_message).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)

    # Bug 2: the card must render BELOW the user message (greater y = lower on
    # the page in the single scrollable chat column).
    user_box = user_message.bounding_box()
    card_box = card.bounding_box()
    assert user_box is not None and card_box is not None, "could not measure bubble/card geometry"
    assert card_box["y"] > user_box["y"], (
        "cursor-native approval card rendered ABOVE the user message it gated "
        f"(card.y={card_box['y']:.0f} <= user.y={user_box['y']:.0f}) — Bug 2 regression."
    )
