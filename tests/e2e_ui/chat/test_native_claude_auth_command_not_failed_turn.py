r"""E2E: ``/login`` from web chat must not surface as a FAILED turn.

On a claude-native session, typing ``/login`` or ``/logout`` into the web
composer is intercepted before injection (``run_turn`` in
``omnigent/inner/claude_native_executor.py``) and answered with the remedy
that actually re-authenticates: "Claude Code's sign-in runs in its own
terminal, so /login and /logout do nothing from the web chat. Run omni setup
on the host to sign in again — or to sign out — then retry."

The interception (pointing at ``omni setup``) is correct — but it delivers that guidance
by yielding ``ExecutorError``. The executor adapter re-raises that as
``RuntimeError("inner executor error: …")``
(``omnigent/runtime/harnesses/_executor_adapter.py``), the runner classifies
it as a broken turn, and:

- the session status flips to ``failed`` with ``{'code': 'runner_error', …}``,
- ``_publish_turn_status`` logs the canonical failed-turn KPI ERROR line
  ``"turn surfaced to UI as failed for … (harness=claude-native): …"``
  (``omnigent/runner/app.py``) — polluting the failed-turn KPI with an
  expected, user-remediable dead end, and
- the SPA shows the **destructive** error pill
  ("Something went wrong setting up the turn on the host.", code
  ``runner_error``) with the guidance buried behind "Expand for details".

So an expected, user-remediable dead end is counted and displayed as an
Omnigent turn failure. This test drives the real journey (a live ``claude``
CLI in the session terminal) and asserts the desired contract:

- the ``omni setup`` guidance must still reach the user (the interception
  must not regress into silently spending a model turn), and
- the turn must NOT surface as a failed turn (no destructive
  ``data-level="error"`` pill carrying the guidance).

While the bug is live the second assertion trips and the test FAILS. A fix
that surfaces the guidance as a non-failed outcome — e.g. an ``info``-level
notice pill (``ErrorBanner level="info"``) or a plain assistant/system
message — makes both halves pass without pinning the fix's exact UI shape.

The test deliberately keys on the failed-turn **status/pill** (published by
the runner's own ``session.status`` stream), not on any assistant transcript
bubble: the claude-native transcript forwarder is not required to observe this
failure, so the reproduction holds even where transcript forwarding is
unavailable.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Page, expect

_COMPOSER_LABEL = "Message the agent"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_DESTRUCTIVE_PILL = '[data-testid="error-pill"][data-level="error"]'
_INFO_PILL = '[data-testid="error-pill"][data-level="info"]'
_PILL_MESSAGE = '[data-testid="error-message-content"]'

# ASCII-safe distinctive substrings of the interception's guidance message
# (omnigent/inner/claude_native_executor.py). Uniquely identify THIS bug's
# error text — no unrelated failure carries them.
_GUIDANCE_SNIPPET = "sign-in runs in its own terminal"
_REMEDY_SNIPPET = "Run omni setup on the host"

# claude-native auto-launch + first-run pre-accept in the session terminal.
_FIRST_TURN_TIMEOUT_MS = 180_000

# Settle time after the composer is editable, so the claude-native terminal
# finishes auto-launching before the auth command is sent (a command sent
# mid-launch would not reach run_turn's interception cleanly).
_TERMINAL_SETTLE_S = 30.0

# How long to watch for the auth-command turn's outcome. The interception
# short-circuits before any model call, so the outcome (the failed-turn pill
# on the buggy build, or the guidance notice on a fixed build) lands quickly.
_OUTCOME_WATCH_S = 120.0


def _send(page: Page, text: str) -> None:
    """Type *text* into the web composer and click Send.

    :param page: The Playwright page, on the session's chat surface.
    :param text: The message body to send.
    """
    composer = page.get_by_role("textbox", name=_COMPOSER_LABEL)
    expect(composer).to_be_editable(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _ensure_chat_view(page: Page) -> None:
    """Switch the terminal-first native session to its chat bubble view.

    :param page: The Playwright page, on the session's chat surface.
    """
    toggle = page.get_by_test_id("view-mode-toggle")
    expect(toggle).to_be_visible(timeout=_FIRST_TURN_TIMEOUT_MS)
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


@pytest.mark.nightly
@pytest.mark.timeout(600)
@pytest.mark.parametrize("command", ["/login", "/logout"])
def test_auth_slash_command_does_not_surface_as_failed_turn(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    command: str,
) -> None:
    """An intercepted ``/login``/``/logout`` must inform, not fail, the turn.

    Journey: open a claude-native session in the web chat, let the terminal
    come up, then send the auth command. The ``omni setup`` guidance must
    reach the user WITHOUT the turn surfacing as failed (destructive error
    pill / ``runner_error`` classification / the failed-turn KPI ERROR log it
    funnels through).

    :param page: Playwright page (fresh context per test).
    :param native_claude_mock_session: ``(base_url, session_id)`` on the
        real claude-native wrapper.
    :param command: The auth slash command under test.
    """
    base_url, session_id = native_claude_mock_session

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    # Wait for the composer to become editable, then let the claude-native
    # terminal finish auto-launching. Sending the auth command against a live,
    # idle terminal ensures it reaches run_turn's interception (not the
    # mid-turn live-injection queue).
    composer = page.get_by_role("textbox", name=_COMPOSER_LABEL)
    expect(composer).to_be_editable(timeout=_FIRST_TURN_TIMEOUT_MS)
    time.sleep(_TERMINAL_SETTLE_S)

    # Environment guard: if the claude-native terminal failed to launch here,
    # a proactive destructive pill appears whose body is a launch error, NOT
    # this bug's guidance. Distinguish that from the bug so we never claim a
    # reproduction on an environment failure.
    pre = page.locator(_DESTRUCTIVE_PILL)
    if pre.count() > 0:
        pre.first.click()
        pre_body = page.locator(_PILL_MESSAGE).first.inner_text(timeout=10_000)
        if _GUIDANCE_SNIPPET not in pre_body:
            pytest.fail(
                "claude-native terminal did not come up cleanly before the "
                f"auth command (environment issue, not this bug): {pre_body!r}"
            )

    # The user does what Claude Code's own error screens tell them to.
    _send(page, command)
    expect(page.locator(_USER, has_text=command).first).to_be_visible(timeout=60_000)

    # Watch the turn's outcome surface: a destructive pill carrying the
    # guidance (the bug), or a non-failed notice carrying the guidance (a
    # fixed build).
    destructive_with_guidance = False
    non_failed_guidance = False
    deadline = time.monotonic() + _OUTCOME_WATCH_S
    while time.monotonic() < deadline:
        dest = page.locator(_DESTRUCTIVE_PILL)
        if dest.count() > 0:
            dest.first.click()
            try:
                body = page.locator(_PILL_MESSAGE).first.inner_text(timeout=5_000)
            except Exception:
                body = ""
            if _GUIDANCE_SNIPPET in body or _REMEDY_SNIPPET in body:
                destructive_with_guidance = True
                break
        # A fixed build surfaces the guidance without failing the turn: an
        # info-level notice pill, or the remedy text rendered as a message.
        if page.locator(_INFO_PILL).count() > 0:
            info = page.locator(_INFO_PILL)
            if page.get_by_text(_REMEDY_SNIPPET).count() == 0:
                info.first.click()
            if page.get_by_text(_REMEDY_SNIPPET).count() > 0:
                non_failed_guidance = True
                break
        if page.get_by_text(_REMEDY_SNIPPET).count() > 0:
            non_failed_guidance = True
            break
        time.sleep(1.0)

    if destructive_with_guidance:
        pytest.fail(
            f"{command} typed into a claude-native session's web composer "
            "surfaced to the user as a FAILED turn: a destructive error pill "
            "('Something went wrong setting up the turn on the host.', code "
            "runner_error) carrying the omni-setup guidance behind 'Expand "
            "for details' — and the runner counted it in the failed-turn KPI "
            "('turn surfaced to UI as failed'). An expected, user-remediable "
            "dead end must be surfaced as a non-failed notice (e.g. an "
            "info-level pill or a plain message), not as a turn failure."
        )

    if not non_failed_guidance:
        pytest.fail(
            f"{command} produced no user-visible outcome within "
            f"{_OUTCOME_WATCH_S:.0f}s: neither a failed-turn pill nor the "
            "omni-setup guidance surfaced. The interception must answer the "
            "user, not swallow the command."
        )

    # Fixed path: guidance surfaced and the turn did not fail. Keep it a
    # non-failure — no destructive pill materializes late.
    expect(page.get_by_text(_REMEDY_SNIPPET).first).to_be_visible(timeout=30_000)
    expect(page.locator(_DESTRUCTIVE_PILL)).to_have_count(0)
