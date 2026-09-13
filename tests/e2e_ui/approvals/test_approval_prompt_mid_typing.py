"""E2E: a permission prompt must not hijack keystrokes from a mid-typing user.

While the user is typing in the composer, a tool call trips a permission gate
and a pending ``ApprovalCard`` appears. Two user-visible failures follow on a
buggy build:

1. **Accidental approval.** ``useApproveHotkey`` binds Cmd/Ctrl+Enter on
   ``window`` in the capture phase with no grace period and no "user is
   composing" guard. Ctrl+Enter is a send-intent chord (and the *only* send
   key for users with the Mod+Enter send preference), so pressing it to send
   the draft the instant the prompt appears silently accepts the prompt
   instead — it auto-dismisses as "Approved" without the user realizing.

2. **Focus stolen mid-typing.** The pending prompt disables the composer
   textarea (``hasPendingElicitation``), which ejects browser focus to
   ``<body>`` mid-word. Keystrokes the user keeps typing vanish with no
   feedback.

Both tests drive a synthetic permission-request hook against a seeded
session (no LLM turn — seconds, like ``test_persistent_approval.py``), so
they run on every PR. They FAIL on the buggy build and pass once a fix
stops a typing-flow keystroke from resolving the prompt and stops the
prompt from silently ejecting the typist's focus.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_COMPOSER = 'textarea[aria-label="Message the agent"]'
_MOCK_ELICITATION_TIMEOUT_MS = 15_000

# The user's draft, mid-sentence when the prompt interrupts.
_DRAFT = "Deploying the fix now, please hold on"
_DRAFT_CONTINUATION = " while I restart the workers"


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _post_permission_hook(base_url: str, session_id: str, holder: dict) -> None:
    """POST a gated-Bash permission request; parks until the UI resolves it."""
    try:
        resp = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
            json={"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/prod-data"}},
            timeout=120.0,
        )
        resp.raise_for_status()
        holder["response"] = resp.json()
    except Exception as exc:  # surfaced by the caller after UI assertions
        holder["error"] = exc


def _start_parked_prompt(base_url: str, session_id: str) -> tuple[threading.Thread, dict]:
    """Fire the permission hook on a background thread and return it."""
    holder: dict = {}
    thread = threading.Thread(
        target=_post_permission_hook, args=(base_url, session_id, holder), daemon=True
    )
    thread.start()
    return thread, holder


def _active_element_descriptor(page: Page) -> str:
    """Describe ``document.activeElement`` (tag + aria-label) for assertions."""
    return page.evaluate(
        """() => {
            const el = document.activeElement;
            if (!el) return "null";
            const label = (el.getAttribute && el.getAttribute("aria-label")) || "";
            return `${el.tagName}:${label}`;
        }"""
    )


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


@pytest.mark.timeout(120)
def test_send_chord_right_after_prompt_appears_does_not_approve(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A send-intent Ctrl+Enter pressed as the prompt appears must not accept it.

    The user has the Mod+Enter send preference on (Ctrl+Enter is their
    ordinary send key), types a message, and presses Ctrl+Enter to send it —
    just as a permission prompt lands. The keystroke expressed send intent,
    not a verdict; the prompt must still be pending afterwards, on the
    client and on the server.
    """
    base_url, session_id = seeded_session
    # Make Ctrl+Enter the user's ordinary send chord, so the keystroke below
    # unambiguously means "send my draft", never "approve".
    page.add_init_script(
        "window.localStorage.setItem('omnigent:composer-submit-with-mod-enter', 'true')"
    )
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()
    page.keyboard.type(_DRAFT)
    expect(composer).to_have_value(_DRAFT)

    hook_thread, holder = _start_parked_prompt(base_url, session_id)

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"

    # The user finishes their thought and hits their send key — within the
    # immediate typing flow, moments after the card mounted.
    page.keyboard.press("Control+Enter")

    # The keystroke must NOT have resolved the prompt. Give the optimistic
    # flip (the bug) ample time to paint before asserting.
    page.wait_for_timeout(1_500)
    expect(
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first,
        "a mid-typing send chord silently approved the pending permission prompt",
    ).to_be_visible()
    assert _pending_elicitations(base_url, session_id), (
        "the send chord resolved the server-side elicitation — "
        "a typing-flow keystroke approved the prompt"
    )

    # Clean up: answer the prompt for real so the parked hook returns.
    card.get_by_role("button", name="Approve", exact=True).click()
    hook_thread.join(timeout=30)
    if "error" in holder:
        raise AssertionError(f"hook thread failed: {holder['error']}") from holder["error"]
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))


@pytest.mark.timeout(120)
def test_prompt_does_not_silently_eject_focus_mid_typing(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The prompt must not steal focus from a mid-typing composer.

    The user is mid-sentence when the prompt appears. Their keystrokes must
    keep landing in the composer draft — not vanish because the prompt
    disabled the textarea and ejected focus to ``<body>``.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()
    page.keyboard.type(_DRAFT)
    expect(composer).to_have_value(_DRAFT)

    hook_thread, holder = _start_parked_prompt(base_url, session_id)

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_MOCK_ELICITATION_TIMEOUT_MS)

    # Focus must still be in the composer — the user never left it.
    active = _active_element_descriptor(page)
    assert active == "TEXTAREA:Message the agent", (
        f"the pending prompt stole focus from the mid-typing composer (now on {active!r})"
    )

    # …and continued typing must land in the draft, not vanish.
    page.keyboard.type(_DRAFT_CONTINUATION)
    assert composer.input_value() == _DRAFT + _DRAFT_CONTINUATION, (
        "keystrokes typed after the prompt appeared were silently dropped"
    )

    # Clean up: answer the prompt for real so the parked hook returns.
    card.get_by_role("button", name="Approve", exact=True).click()
    hook_thread.join(timeout=30)
    if "error" in holder:
        raise AssertionError(f"hook thread failed: {holder['error']}") from holder["error"]
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
