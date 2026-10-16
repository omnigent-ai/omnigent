"""E2E: sending a dictated message must end the voice take (phone profile).

The mobile apps load this SPA in a WebView; desktop Chromium at a 390x844
touch profile stands in for them. Both dictation paths are covered:

- Web Speech (``webkitSpeechRecognition``), taken by a WebView that exposes
  the API. Headless Chromium's recognizer has no backend, so a scripted one is
  installed before boot; it keeps listening until ``stop()``/``abort()`` and
  records those calls.
- Server dictation (``WS /v1/dictation/stream`` + the fake engine), taken by a
  WebView without Web Speech. The constructors are stripped before boot, as in
  ``tests/e2e_ui/chat/test_dictation.py``.

``getUserMedia`` is wrapped so the test can read whether the microphone
tracks the take opened are still live after the send.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from playwright.sync_api import Browser, BrowserContext, Page, expect

from omnigent.server.dictation import FAKE_SCRIPT as _FAKE_SCRIPT
from tests.e2e_ui.conftest import configure_mock_llm

_PHONE: dict[str, Any] = {
    "viewport": {"width": 390, "height": 844},
    "is_mobile": True,
    "has_touch": True,
}

_COMPOSER = 'textarea[aria-label="Message the agent"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'

_DICTATED = "voice dictated message summarize the deploy status"
_AFTER_SEND = "words spoken after the send"
_REPLY = "deploy-status-voice-reply"

_TURN_TIMEOUT_MS = 30_000
_TRANSCRIPT_TIMEOUT_MS = 20_000
# A manual stop flips the mic within a second; the take gets several to end.
_STOP_TIMEOUT_MS = 5_000

_TRACK_MIC_STREAMS_JS = """
(() => {
  const streams = [];
  window.__micStreams = streams;
  const original = MediaDevices.prototype.getUserMedia;
  MediaDevices.prototype.getUserMedia = async function (constraints) {
    const stream = await original.call(this, constraints);
    streams.push(stream);
    return stream;
  };
})();
"""

_FAKE_WEB_SPEECH_JS = """
(() => {
  const state = { starts: 0, stops: 0, aborts: 0, active: false, current: null };
  window.__fakeSpeech = state;
  class FakeSpeechRecognition extends EventTarget {
    constructor() {
      super();
      this.continuous = false;
      this.interimResults = false;
      this.lang = "en-US";
      state.current = this;
    }
    start() {
      state.starts += 1;
      state.active = true;
      setTimeout(() => this.dispatchEvent(new Event("start")), 0);
    }
    stop() {
      state.stops += 1;
      this._end();
    }
    abort() {
      state.aborts += 1;
      this._end();
    }
    _end() {
      if (!state.active) return;
      state.active = false;
      setTimeout(() => this.dispatchEvent(new Event("end")), 0);
    }
  }
  window.__fakeSpeechSay = (text) => {
    if (!state.active || !state.current) return false;
    const event = new Event("result");
    event.resultIndex = 0;
    event.results = { length: 1, 0: { length: 1, isFinal: true, 0: { transcript: text } } };
    state.current.dispatchEvent(event);
    return true;
  };
  window.SpeechRecognition = FakeSpeechRecognition;
  window.webkitSpeechRecognition = FakeSpeechRecognition;
})();
"""

_STRIP_WEB_SPEECH_JS = (
    "Object.defineProperty(window, 'SpeechRecognition',"
    " { value: undefined, configurable: true });"
    "Object.defineProperty(window, 'webkitSpeechRecognition',"
    " { value: undefined, configurable: true });"
)

_MIC_TRACK_STATES_JS = (
    "() => window.__micStreams.flatMap(s => s.getAudioTracks().map(t => t.readyState))"
)


def _open_phone_session(
    browser: Browser,
    browser_context_args: dict[str, Any],
    base_url: str,
    session_id: str,
    init_scripts: list[str],
) -> tuple[BrowserContext, Page]:
    context = browser.new_context(**{**browser_context_args, **_PHONE}, permissions=["microphone"])
    page = context.new_page()
    for script in init_scripts:
        page.add_init_script(script)
    page.goto(f"{base_url}/c/{session_id}")
    return context, page


def _dictate_then_send(page: Page, dictated: str) -> None:
    """Tap the mic, wait for ``dictated`` to land, tap Send while the take is live."""
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    mic = page.get_by_role("button", name="Voice dictation")
    expect(mic).to_be_visible()
    mic.tap()
    expect(mic).to_have_attribute("aria-pressed", "true", timeout=_TRANSCRIPT_TIMEOUT_MS)
    expect(composer).to_have_value(re.compile(re.escape(dictated)), timeout=_TRANSCRIPT_TIMEOUT_MS)
    expect(mic).to_have_attribute("aria-pressed", "true")

    page.get_by_role("button", name="Send", exact=True).tap()
    expect(page.locator(_USER_BUBBLE).filter(has_text=dictated)).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )
    expect(composer).to_have_value("")
    # The composer is still usable after the send, so the mic's auto-stop on a
    # disabled composer does not apply; only a stop on send can end the take.
    expect(composer).to_be_enabled()
    expect(mic).to_be_enabled()


def _expect_take_ended(page: Page) -> None:
    mic = page.get_by_role("button", name="Voice dictation")
    try:
        expect(mic).to_have_attribute("aria-pressed", "false", timeout=_STOP_TIMEOUT_MS)
    except AssertionError as exc:
        observed = {
            "mic_tracks": page.evaluate(_MIC_TRACK_STATES_JS),
            "composer": page.locator(_COMPOSER).input_value(),
        }
        raise AssertionError(
            f"voice take survived the send: mic still listening after {_STOP_TIMEOUT_MS} ms;"
            f" observed {observed}"
        ) from exc
    track_states = page.evaluate(_MIC_TRACK_STATES_JS)
    assert track_states, "no microphone tracks were captured, so release is unverifiable"
    assert "live" not in track_states, f"microphone tracks still live: {track_states}"


def test_web_speech_take_ends_when_message_is_sent(
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    configure_mock_llm(
        mock_llm_server_url, [{"text": _REPLY}], key="voice-send-web-speech", match=_DICTATED
    )
    context, page = _open_phone_session(
        browser,
        browser_context_args,
        base_url,
        session_id,
        [_TRACK_MIC_STREAMS_JS, _FAKE_WEB_SPEECH_JS],
    )
    try:
        composer = page.locator(_COMPOSER)
        expect(composer).to_be_visible(timeout=_TURN_TIMEOUT_MS)
        mic = page.get_by_role("button", name="Voice dictation")
        expect(mic).to_be_visible()
        mic.tap()
        expect(mic).to_have_attribute("aria-pressed", "true")
        assert page.evaluate(f"() => window.__fakeSpeechSay({json.dumps(_DICTATED)})")
        expect(composer).to_have_value(re.compile(re.escape(_DICTATED)))

        page.get_by_role("button", name="Send", exact=True).tap()
        expect(page.locator(_USER_BUBBLE).filter(has_text=_DICTATED)).to_be_visible(
            timeout=_TURN_TIMEOUT_MS
        )
        expect(composer).to_have_value("")
        expect(composer).to_be_enabled()
        expect(mic).to_be_enabled()

        # Confirm the take ended before probing. Only then can speech injected
        # afterwards reveal a recognizer that wrongly survived the send, and the
        # stop/abort it triggered is already recorded for the snapshot below.
        _expect_take_ended(page)
        heard_after_send = page.evaluate(
            f"() => window.__fakeSpeechSay({json.dumps(_AFTER_SEND)})"
        )
        assert not heard_after_send, "recognizer was still listening after the send"

        recognizer = page.evaluate(
            "() => ({starts: __fakeSpeech.starts, stops: __fakeSpeech.stops,"
            " aborts: __fakeSpeech.aborts, active: __fakeSpeech.active})"
        )
        assert recognizer["stops"] + recognizer["aborts"] >= 1, recognizer
        expect(composer).to_have_value("")
    finally:
        context.close()


def test_server_dictation_take_ends_when_message_is_sent(
    browser: Browser,
    browser_context_args: dict[str, Any],
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    response = httpx.get(f"{base_url}/v1/info", timeout=10.0)
    response.raise_for_status()
    info = response.json()
    # The shared fixture defaults OMNIGENT_DICTATION_ENGINE=fake, so dictation is
    # always advertised here; assert it rather than skip so a setup or capability
    # regression fails loudly instead of silently bypassing this coverage.
    assert info.get("dictation_available"), "server dictation must be enabled for this regression"
    configure_mock_llm(
        mock_llm_server_url, [{"text": _REPLY}], key="voice-send-server", match=_FAKE_SCRIPT
    )
    context, page = _open_phone_session(
        browser,
        browser_context_args,
        base_url,
        session_id,
        [_TRACK_MIC_STREAMS_JS, _STRIP_WEB_SPEECH_JS],
    )
    try:
        _dictate_then_send(page, _FAKE_SCRIPT)
        _expect_take_ended(page)
    finally:
        context.close()
