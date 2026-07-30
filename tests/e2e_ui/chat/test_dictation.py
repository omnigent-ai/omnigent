"""e2e: server-side dictation streams transcripts into the composer.

Drives the full loop the unit tests can't: mic capture (Chromium's fake
media device) → AudioWorklet 16 kHz PCM frames → ``WS /v1/dictation/stream``
→ the server's fake engine (``OMNIGENT_DICTATION_ENGINE=fake``, set by the
``live_server`` fixture) → transcript events → live text in the composer
textarea. The fake engine reveals one word of its script per 100 ms of
audio received, so a second of fake-mic streaming produces the full
sentence, finalized by the engine, without any ASR model.

The test pins the *no-Web-Speech* entry into server mode by stripping the
SpeechRecognition constructors before the app boots (Playwright's Chromium
exposes them, but its cloud backend is dead in automation). The other
entry — Web Speech present but failing at runtime with a ``network`` error
— is pinned in ``web/src/components/ComposerMicButton.test.tsx``.

A failure here means one of:

- ``/v1/info`` stopped advertising ``dictation_available`` (capability
  plumbing in ``omnigent/server/app.py`` or ``web/src/lib/capabilities.ts``).
- The WebSocket route broke (``omnigent/server/routes/dictation.py``).
- The capture pipeline broke (``web/src/lib/dictation.ts`` worklet/socket).
- The composer stopped applying interim/final updates
  (``ComposerMicButton.tsx`` / ``useDictationInsert.ts`` / ``ChatPage.tsx``).
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, expect

from omnigent.server.dictation import FAKE_SCRIPT as _FAKE_SCRIPT

# The capability probe caches per page load; the worklet chunks audio at
# 100 ms; CI machines are slow — a generous ceiling keeps this deflaked.
_TRANSCRIPT_TIMEOUT_MS = 20_000


def _open_server_dictation_page(
    browser: Browser,
    browser_context_args: dict[str, Any],
    base_url: str,
    session_id: str,
) -> tuple[BrowserContext, Page]:
    """Open a chat page pinned to *server* dictation mode.

    Returns ``(context, page)``; the caller owns closing the context. A
    fresh context is built per test to grant the microphone permission,
    and the SpeechRecognition constructors are stripped before boot so the
    button takes the server path deterministically instead of relying on
    Chromium's runtime "network" failure timing.
    """
    # Spread the plugin's context args so --video/--tracing keep working
    # even though each test builds its own context for the mic permission.
    context = browser.new_context(**browser_context_args, permissions=["microphone"])
    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(window, 'SpeechRecognition',"
        " { value: undefined, configurable: true });"
        "Object.defineProperty(window, 'webkitSpeechRecognition',"
        " { value: undefined, configurable: true });"
    )
    page.goto(f"{base_url}/c/{session_id}")
    return context, page


def test_dictation_streams_transcript_into_composer(
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
) -> None:
    """Click the mic, speak (fake device), watch the transcript form."""
    base_url, session_id = seeded_session
    context, page = _open_server_dictation_page(
        browser, browser_context_args, base_url, session_id
    )
    try:
        composer = page.get_by_placeholder("Ask the agent anything…")
        expect(composer).to_be_visible()

        # The button only renders once /v1/info reports dictation_available,
        # so its visibility already asserts the capability plumbing.
        mic = page.get_by_role("button", name="Voice dictation")
        expect(mic).to_be_visible()

        mic.click()
        expect(mic).to_have_attribute("aria-pressed", "true")

        # The fake engine finalizes its script after ~0.5 s of audio; the
        # finalized sentence must land in the composer verbatim.
        expect(composer).to_have_value(
            re.compile(re.escape(_FAKE_SCRIPT)),
            timeout=_TRANSCRIPT_TIMEOUT_MS,
        )

        mic.click()
        expect(mic).to_have_attribute("aria-pressed", "false")
        # Stopping must not clobber the finalized text.
        expect(composer).to_have_value(re.compile(re.escape(_FAKE_SCRIPT)))
    finally:
        context.close()


def test_hotkey_toggles_dictation(
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
) -> None:
    """⌘⌥V (Ctrl+Alt+V on CI's Linux) starts and stops dictation.

    The chord is matched on the physical ``KeyV`` code, so this exercises
    the same window keydown path a real keyboard drives — not the mic
    button's onClick. Mirrors the Ctrl+Alt chord coverage in
    ``tests/e2e_ui/hotkeys/test_sidebar_hotkeys.py``.
    """
    base_url, session_id = seeded_session
    context, page = _open_server_dictation_page(
        browser, browser_context_args, base_url, session_id
    )
    try:
        expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()
        mic = page.get_by_role("button", name="Voice dictation")
        expect(mic).to_be_visible()
        expect(mic).to_have_attribute("aria-pressed", "false")

        # Start via the hotkey — server handshake flips aria-pressed once
        # audio flows.
        page.keyboard.press("Control+Alt+KeyV")
        expect(mic).to_have_attribute("aria-pressed", "true", timeout=_TRANSCRIPT_TIMEOUT_MS)

        # Stop via the hotkey.
        page.keyboard.press("Control+Alt+KeyV")
        expect(mic).to_have_attribute("aria-pressed", "false")
    finally:
        context.close()


def test_enter_while_listening_commits_text(
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
) -> None:
    """Enter ends dictation but keeps the dictated text in the composer."""
    base_url, session_id = seeded_session
    context, page = _open_server_dictation_page(
        browser, browser_context_args, base_url, session_id
    )
    try:
        composer = page.get_by_placeholder("Ask the agent anything…")
        expect(composer).to_be_visible()
        # Normalize the starting draft so the assertions are exact.
        composer.fill("")

        mic = page.get_by_role("button", name="Voice dictation")
        mic.click()
        expect(mic).to_have_attribute("aria-pressed", "true")
        expect(composer).to_have_value(
            re.compile(re.escape(_FAKE_SCRIPT)),
            timeout=_TRANSCRIPT_TIMEOUT_MS,
        )

        # Enter commits: dictation ends and the text stays. The capture-phase
        # handler also preempts the composer's Enter-to-send, so the draft is
        # not submitted — asserting the text is still present proves both.
        page.keyboard.press("Enter")
        expect(mic).to_have_attribute("aria-pressed", "false")
        expect(composer).to_have_value(re.compile(re.escape(_FAKE_SCRIPT)))
    finally:
        context.close()


def test_escape_while_listening_discards_text(
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
) -> None:
    """Esc ends dictation and reverts the composer to its pre-dictation text."""
    base_url, session_id = seeded_session
    context, page = _open_server_dictation_page(
        browser, browser_context_args, base_url, session_id
    )
    try:
        composer = page.get_by_placeholder("Ask the agent anything…")
        expect(composer).to_be_visible()
        # Empty snapshot at voice start, so a correct discard empties it again.
        composer.fill("")

        mic = page.get_by_role("button", name="Voice dictation")
        mic.click()
        expect(mic).to_have_attribute("aria-pressed", "true")
        expect(composer).to_have_value(
            re.compile(re.escape(_FAKE_SCRIPT)),
            timeout=_TRANSCRIPT_TIMEOUT_MS,
        )

        # Esc discards: dictation ends and the dictated text is dropped,
        # reverting to the empty pre-dictation snapshot.
        page.keyboard.press("Escape")
        expect(mic).to_have_attribute("aria-pressed", "false")
        expect(composer).to_have_value("")
    finally:
        context.close()
