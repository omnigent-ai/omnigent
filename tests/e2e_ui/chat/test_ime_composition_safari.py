"""IME composition guard against Safari's compositionend-before-Enter order.

Covers issue #433 (PR #567): Safari fires ``compositionend`` BEFORE the
``keydown(Enter)`` that confirmed the composition, and that keydown reports
``isComposing=false`` / ``keyCode=13`` (not the ``229`` IME-processing
fallback). The #132 guard (``isComposing`` ref + ``keyCode === 229``) missed
it because the synchronous ``isComposing=false`` reset in the
``compositionEnd`` handler had already cleared the flag by the time the
confirming keydown fired. The fix defers the reset to the next macrotask
(``setTimeout(0)``) in both live composers (``ChatPage`` + the new-chat
landing screen) so the confirming Enter still observes composition active
and is ignored.

These tests drive the **chat** composer (``ChatPage.tsx``) in a real
browser. Playwright's real keyboard cannot emit composition events, so the
Safari order is modeled with synthetic ``CompositionEvent`` /
``KeyboardEvent`` dispatched via ``page.evaluate`` on the composer
``<textarea>`` (React's root-level delegation picks them up). The Safari
test asserts the confirming Enter does NOT submit mid-composition (the
composer keeps its draft and no user bubble / Working indicator appears),
then asserts a deliberate Enter after the deferred reset flush DOES
submit. The Chrome-order test is the non-regression guard: the confirming
Enter fired mid-composition (``isComposing=true``) must still be ignored.

Like the rest of the ``tests/e2e_ui`` suite, this requires the live
Databricks LLM gateway the harness boots against — see
``tests/e2e_ui/conftest.py``. The negative assertions don't depend on the
LLM responding (submission is blocked client-side before any request),
but the deliberate-Enter positive assertion optimistically renders a user
bubble, and the run is dispatched to the real runner.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

_COMPOSER_LABEL = "Message the agent"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'

# JS that drives Safari's compositionend-before-Enter order on the chat
# composer. Dispatches compositionstart → (value set via the React-aware
# native setter) → compositionend → keydown(Enter) with the Safari-confirming
# signature (isComposing=false, keyCode=13). The deferred reset in the
# compositionEnd handler keeps isComposingRef true through this keydown, so
# the IME guard in handleKeyDown ignores it. Returns nothing.
_SAFARI_ORDER_JS = """
() => {
  const ta = document.querySelector('textarea[aria-label="Message the agent"]');
  const proto = window.HTMLTextAreaElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  ta.focus();
  ta.dispatchEvent(new CompositionEvent('compositionstart', {
    bubbles: true, data: '',
  }));
  setter.call(ta, 'オムニジェント');
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.dispatchEvent(new CompositionEvent('compositionend', {
    bubbles: true, data: 'オムニジェント',
  }));
  // Safari's confirming keydown: fires AFTER compositionend, with
  // isComposing=false / keyCode=13 (NOT the 229 IME-processing fallback).
  ta.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', keyCode: 13, isComposing: false,
    bubbles: true, cancelable: true,
  }));
}
"""

# Chrome/Firefox order: the confirming keydown fires DURING composition, so
# isComposing=true. The #132 guard already catches this; the test is a
# non-regression guard for #567's deferred reset (it must not clear the flag
# before this keydown). Returns nothing.
_CHROME_ORDER_JS = """
() => {
  const ta = document.querySelector('textarea[aria-label="Message the agent"]');
  const proto = window.HTMLTextAreaElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  ta.focus();
  ta.dispatchEvent(new CompositionEvent('compositionstart', {
    bubbles: true, data: '',
  }));
  setter.call(ta, 'オムニジェント');
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  // Chrome's confirming keydown: fires DURING composition, isComposing=true.
  ta.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true,
    bubbles: true, cancelable: true,
  }));
  ta.dispatchEvent(new CompositionEvent('compositionend', {
    bubbles: true, data: 'オムニジェント',
  }));
}
"""

# A single deliberate Enter after the deferred reset has flushed. Used by
# both tests to confirm the composer still sends normally once composition
# has fully ended (the guard is not over-suppressing). Returns nothing.
_DELIBERATE_ENTER_JS = """
() => {
  const ta = document.querySelector('textarea[aria-label="Message the agent"]');
  ta.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', keyCode: 13, isComposing: false,
    bubbles: true, cancelable: true,
  }));
}
"""

# Flush the deferred isComposing reset. Real browsers clamp setTimeout(0) to
# ~4ms; 50ms is a safe margin that still keeps the test fast.
_FLUSH_JS = "() => new Promise((resolve) => setTimeout(resolve, 50))"


def _composer(page: Page):
    """Locator for the chat composer textarea (aria-label)."""
    return page.get_by_label(_COMPOSER_LABEL)


@pytest.mark.llm_flaky(reruns=2, reruns_delay=1)
def test_safari_compositionend_before_enter_does_not_submit(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Safari fires compositionend before the confirming Enter; Enter is suppressed.

    The confirming keydown reports ``isComposing=false`` / ``keyCode=13`` (not
    229), which the #132 guard missed once the synchronous
    ``compositionEnd`` reset had cleared the flag. The deferred reset (#567)
    keeps ``isComposingRef`` true through this keydown, so ``handleKeyDown``
    bails on the IME guard and ``submit()`` never runs.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` — the chat composer
        lives at ``/c/{session_id}``.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = _composer(page)
    expect(composer).to_be_visible()

    # Safari order: compositionstart → value → compositionend → Enter.
    page.evaluate(_SAFARI_ORDER_JS)

    # Enter was suppressed mid-composition: the draft is still in the
    # composer, no user bubble was added, and no run started (no Working
    # indicator). submit() would have cleared the value and appended a user
    # bubble synchronously, so these are strong negative signals.
    expect(composer).to_have_value("オムニジェント")
    expect(page.locator(_USER_BUBBLE)).to_have_count(0)
    expect(page.locator(_WORKING)).to_have_count(0)

    # After the deferred reset flushes, a deliberate Enter sends normally —
    # the guard is not over-suppressing. The optimistic user bubble is the
    # submission signal (it renders before the LLM responds).
    page.evaluate(_FLUSH_JS)
    page.evaluate(_DELIBERATE_ENTER_JS)
    expect(page.locator(_USER_BUBBLE)).to_have_count(1, timeout=15_000)


@pytest.mark.llm_flaky(reruns=2, reruns_delay=1)
def test_chrome_composition_keydown_during_composition_does_not_submit(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Chrome-order confirming Enter (during composition) is ignored (#132 non-regression).

    Chrome/Firefox fire the confirming keydown DURING composition with
    ``isComposing=true``, which #132 already caught. This test guards #567's
    deferred reset: it must not clear ``isComposingRef`` before this mid-
    composition keydown (a microtask reset would have). The deliberate Enter
    after the flush sends normally.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)``.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = _composer(page)
    expect(composer).to_be_visible()

    # Chrome order: compositionstart → value → Enter (isComposing=true)
    # → compositionend.
    page.evaluate(_CHROME_ORDER_JS)

    # Mid-composition Enter was ignored: draft preserved, no user bubble,
    # no run started.
    expect(composer).to_have_value("オムニジェント")
    expect(page.locator(_USER_BUBBLE)).to_have_count(0)
    expect(page.locator(_WORKING)).to_have_count(0)

    # After compositionend's deferred reset flushes, a deliberate Enter
    # sends the draft.
    page.evaluate(_FLUSH_JS)
    page.evaluate(_DELIBERATE_ENTER_JS)
    expect(page.locator(_USER_BUBBLE)).to_have_count(1, timeout=15_000)
