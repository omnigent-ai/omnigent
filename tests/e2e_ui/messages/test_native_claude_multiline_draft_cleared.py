"""Web messages and setting changes must not submit an unsent multi-line terminal draft.

The embedded terminal and the web chat share one Claude Code input box, which the
bridge must empty before it pastes a web-composer message or types a web-initiated
slash command (``/effort high``). Both journeys here type a three-line draft
through the real xterm, trigger from the web UI, and read back what Claude Code
actually submitted to the (mock) model.

The TUI screen is read by replaying the terminal-attach WebSocket frames the
browser receives through a ``pyte`` screen: the runner's tmux socket lives in
another sandbox, so ``capture-pane`` is not reachable from the test process.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pyte
import pytest
from playwright.sync_api import Page, WebSocket, expect

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _item_text,
    _ordered_message_items,
    _send,
)
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _focus_tui,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_DRAFT_LINES = ("STALE ONE", "STALE TWO", "STALE THREE")
_DRAFT_RENDER_TIMEOUT_S = 20.0
_TURN_TIMEOUT_MS = 90_000
# The slash-command path waits out its draft-visibility and dialog budgets
# before anything observable happens, so give the outcome room to land.
_EFFORT_OUTCOME_TIMEOUT_S = 60.0
# Claude Code's own acknowledgement that the command was applied, not prompted.
_EFFORT_ACK = "Set effort level to high"
_PATCH_TIMEOUT_MS = 120_000
_EVIDENCE_DIR_ENV = "OMNIGENT_E2E_EVIDENCE_DIR"


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, object]) -> dict[str, object]:
    """Film at the viewport size so the TUI text stays legible in the clip."""
    if "record_video_dir" not in browser_context_args:
        return browser_context_args
    viewport = browser_context_args.get("viewport") or {"width": 1280, "height": 720}
    return {**browser_context_args, "record_video_size": viewport}


@pytest.fixture(scope="session", autouse=True)
def _requires_mockable_claude() -> None:
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip(
            "machine-managed Claude settings override mock auth; use an isolated container"
        )


class _TerminalScreen:
    """Mirror the TUI screen from the bytes the browser's xterm receives."""

    def __init__(self, page: Page) -> None:
        self._screen = pyte.Screen(80, 24)
        self._stream = pyte.ByteStream(self._screen)
        self._active: WebSocket | None = None
        self.attached = False
        page.on("websocket", self._on_websocket)

    def _on_websocket(self, ws: WebSocket) -> None:
        if "/resources/terminals/" not in ws.url or "/attach" not in ws.url:
            return
        # A redial re-seeds the whole screen, so start from a blank screen and
        # parser, and stop listening to the connection it replaces.
        self._screen = pyte.Screen(80, 24)
        self._stream = pyte.ByteStream(self._screen)
        self._active = ws
        self.attached = True
        ws.on("framesent", lambda payload: self._on_sent(ws, payload))
        ws.on("framereceived", lambda payload: self._on_received(ws, payload))

    def _on_sent(self, ws: WebSocket, payload: str | bytes) -> None:
        if ws is not self._active or not isinstance(payload, str):
            return
        try:
            message = json.loads(payload)
        except ValueError:
            return
        if isinstance(message, dict) and message.get("type") == "resize":
            rows, cols = message.get("rows"), message.get("cols")
            if rows and cols:
                self._screen.resize(int(rows), int(cols))

    def _on_received(self, ws: WebSocket, payload: str | bytes) -> None:
        if ws is self._active and isinstance(payload, bytes):
            self._stream.feed(payload)

    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self._screen.display)


def _save_evidence(name: str, text: str) -> None:
    """Keep a screen dump or request journal next to the recording when asked to."""
    target = os.environ.get(_EVIDENCE_DIR_ENV)
    _log.info("%s:\n%s", name, text)
    if not target:
        return
    directory = Path(target)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def _composer_holds_draft(screen_text: str) -> bool:
    """True when the live input box (not the scrollback) shows all three draft lines."""
    lines = screen_text.splitlines()
    starts = [i for i, line in enumerate(lines) if "❯" in line and _DRAFT_LINES[0] in line]
    if not starts:
        return False
    tail = "\n".join(lines[starts[-1] :])
    return all(line in tail for line in _DRAFT_LINES[1:])


def _type_multiline_draft(page: Page, screen: _TerminalScreen, label: str) -> None:
    """Type the three-line draft into the TUI and wait until its input box renders it."""
    _focus_tui(page)
    for index, line in enumerate(_DRAFT_LINES):
        if index:
            # Backslash + Enter is Claude Code's newline inside the input box.
            page.keyboard.type("\\")
            page.keyboard.press("Enter")
        page.keyboard.type(line, delay=15)
    deadline = time.monotonic() + _DRAFT_RENDER_TIMEOUT_S
    text = ""
    while time.monotonic() < deadline:
        page.wait_for_timeout(300)
        text = screen.text()
        if _composer_holds_draft(text):
            _save_evidence(f"{label}-screen-with-draft.txt", text)
            return
    raise AssertionError(
        f"TUI input box never showed the three-line draft (attach seen: {screen.attached}); "
        f"screen was:\n{text}"
    )


def _request_count(mock_url: str) -> int:
    resp = httpx.get(f"{mock_url}/stats", timeout=10.0)
    resp.raise_for_status()
    return int(resp.json()["request_count"])


def _submitted_prompts(mock_url: str) -> list[str]:
    """Text of the newest user message in every captured model request, in order."""
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=15.0)
    resp.raise_for_status()
    prompts: list[str] = []
    for req in resp.json().get("requests", []):
        if not isinstance(req, dict):
            continue
        messages = req.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            continue
        content = last.get("content")
        if isinstance(content, str):
            prompts.append(content)
        elif isinstance(content, list):
            prompts.append(
                "\n".join(
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                )
            )
    return prompts


def _open_session_terminal(page: Page, base_url: str, session_id: str) -> _TerminalScreen:
    screen = _TerminalScreen(page)
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    return screen


def _show_terminal_outcome(page: Page) -> None:
    """Return to the Terminal view so the TUI's submitted-prompt echo is on screen."""
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    page.wait_for_timeout(2_000)


def _transcript_user_texts(base_url: str, session_id: str) -> list[str]:
    items = _ordered_message_items(base_url, session_id)
    return [_item_text(item) for item in items if item.get("role") == "user"]


@pytest.mark.nightly
@pytest.mark.timeout(480)
def test_web_message_replaces_multi_line_tui_draft(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A web-composer message must reach Claude without the draft left in the TUI."""
    base_url, session_id = native_claude_mock_session
    nonce = uuid.uuid4().hex[:8]
    web_message = f"hello from the web {nonce}"
    reply = f"ast-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", reply)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, reply)

    page: Page = request.getfixturevalue("page")
    screen = _open_session_terminal(page, base_url, session_id)
    requests_before = _request_count(mock_llm_server_url)
    _type_multiline_draft(page, screen, "message")
    assert _request_count(mock_llm_server_url) == requests_before, (
        "typing the draft must not start a turn"
    )

    _ensure_chat_view(page)
    _send(page, web_message)
    expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)
    _show_terminal_outcome(page)

    _save_evidence("message-screen-after-send.txt", screen.text())
    prompts = _submitted_prompts(mock_llm_server_url)
    _save_evidence("message-submitted-prompts.json", json.dumps(prompts, indent=2))
    user_texts = _transcript_user_texts(base_url, session_id)
    _save_evidence("message-transcript-user-items.json", json.dumps(user_texts, indent=2))

    delivered = [prompt for prompt in prompts if nonce in prompt]
    assert delivered, f"the web message never reached the model; prompts: {prompts!r}"
    for prompt in delivered:
        stale = [line for line in _DRAFT_LINES if line in prompt]
        assert not stale, (
            f"Claude submitted the web message together with leftover draft text {stale}: "
            f"{prompt[-300:]!r}"
        )
    for text in user_texts:
        assert not any(line in text for line in _DRAFT_LINES), (
            f"transcript user message carries draft text: {text!r}"
        )


def _change_effort_from_web(page: Page, session_id: str, level: str) -> tuple[int, str]:
    """Pick *level* in the composer's Configure session gear; return the PATCH outcome."""
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=30_000)
    gear.click()
    page.get_by_test_id("composer-agent-edit").click()
    expect(page.get_by_test_id("composer-agent-efforts")).to_be_visible(timeout=15_000)
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
        ),
        timeout=_PATCH_TIMEOUT_MS,
    ) as patched:
        page.get_by_test_id(f"composer-agent-effort-{level}").click()
    response = patched.value
    return response.status, response.text()


@pytest.mark.nightly
@pytest.mark.timeout(480)
def test_web_effort_change_replaces_multi_line_tui_draft(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Changing the effort from the web must apply the command, not prompt the model with it."""
    base_url, session_id = native_claude_mock_session
    reply = f"ast-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", reply)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, reply)

    page: Page = request.getfixturevalue("page")
    screen = _open_session_terminal(page, base_url, session_id)
    requests_before = _request_count(mock_llm_server_url)
    _type_multiline_draft(page, screen, "effort")
    assert _request_count(mock_llm_server_url) == requests_before, (
        "typing the draft must not start a turn"
    )

    _ensure_chat_view(page)
    status, body = _change_effort_from_web(page, session_id, "high")
    _save_evidence("effort-patch-response.txt", f"{status}\n{body}")

    # The outcome is either Claude Code's acknowledgement in the terminal or,
    # when the command was submitted as a prompt instead, a model request.
    _show_terminal_outcome(page)
    deadline = time.monotonic() + _EFFORT_OUTCOME_TIMEOUT_S
    screen_text = screen.text()
    while time.monotonic() < deadline:
        screen_text = screen.text()
        if _EFFORT_ACK in screen_text or _request_count(mock_llm_server_url) > requests_before:
            break
        page.wait_for_timeout(500)
    _save_evidence("effort-screen-after-change.txt", screen_text)
    session_resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    session_resp.raise_for_status()
    session = session_resp.json()
    _save_evidence(
        "effort-session-after-change.json",
        json.dumps({"reasoning_effort": session.get("reasoning_effort")}, indent=2),
    )
    prompts = _submitted_prompts(mock_llm_server_url)
    _save_evidence("effort-submitted-prompts.json", json.dumps(prompts, indent=2))
    user_texts = _transcript_user_texts(base_url, session_id)
    _save_evidence("effort-transcript-user-items.json", json.dumps(user_texts, indent=2))

    assert status == 200, f"effort change failed: HTTP {status} {body}"
    assert _EFFORT_ACK in screen_text, (
        f"Claude Code never acknowledged the effort change; screen was:\n{screen_text}"
    )
    for prompt in prompts:
        assert "/effort" not in prompt and not any(line in prompt for line in _DRAFT_LINES), (
            f"the effort change was submitted to the model as prompt text: {prompt!r}"
        )
    assert not any("/effort" in text for text in user_texts), (
        f"the effort command landed in the transcript as a user message: {user_texts!r}"
    )
