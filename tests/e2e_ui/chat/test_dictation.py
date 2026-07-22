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

from playwright.sync_api import Browser, expect

from omnigent.server.dictation import FAKE_SCRIPT as _FAKE_SCRIPT

# The capability probe caches per page load; the worklet chunks audio at
# 100 ms; CI machines are slow — a generous ceiling keeps this deflaked.
_TRANSCRIPT_TIMEOUT_MS = 20_000


def test_dictation_streams_transcript_into_composer(
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
) -> None:
    """Click the mic, speak (fake device), watch the transcript form."""
    base_url, session_id = seeded_session
    # Spread the plugin's context args so --video/--tracing keep working
    # even though this test builds its own context for the mic permission.
    context = browser.new_context(**browser_context_args, permissions=["microphone"])
    try:
        page = context.new_page()
        # Force server mode deterministically: without Web Speech
        # constructors the button picks the server path directly instead
        # of relying on Chromium's runtime "network" failure timing.
        page.add_init_script(
            "Object.defineProperty(window, 'SpeechRecognition',"
            " { value: undefined, configurable: true });"
            "Object.defineProperty(window, 'webkitSpeechRecognition',"
            " { value: undefined, configurable: true });"
        )
        page.goto(f"{base_url}/c/{session_id}")

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
