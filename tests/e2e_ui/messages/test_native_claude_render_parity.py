r"""UI journeys: native Claude Code parity with its TUI, and delivery into it.

The native ``claude-native`` ("Claude Code") wrapper is terminal-first: a real
``claude`` CLI runs in the session terminal, the SPA's **Terminal** view
attaches to that live TUI over a WebSocket, and the SPA's **Chat** view renders
the SAME canonical transcript (``GET /v1/sessions/{id}/items``) the TUI prints.
A native bridge forwards web-composer messages INTO the Claude process and
forwards Claude's transcript back OUT as conversation items. This suite asserts
that round-trips both ways and renders exactly once — the three properties the
native forwarder has historically regressed on.

The LLM calls are served by the in-process mock LLM server rather than a real
Anthropic endpoint. Before each test run a mock ``anthropic`` provider config is
written to ``~/.omnigent/config.yaml`` (see ``native_claude_mock_session`` in
``conftest.py``), redirecting the runner's ``ANTHROPIC_BASE_URL`` to the mock
server. Each tested turn installs its expected token as the fallback response,
so Claude's private background requests cannot drain later turns from a queue.

The second journey covers the IN direction under the condition that broke it:
a person leaves the TUI composer occupied from the embedded terminal (a ctrl+r
history search, ``!`` shell mode, the double-Escape rewind dialog) and then
sends from the web composer. Those keystrokes go through the real xterm, so the
pane genuinely is in that state when the message is injected.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Callable

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state, reset_mock_llm, set_fallback_mock_llm

# Reuse the custom-agent suite's helpers — both surfaces render from the same
# canonical transcript, so parity / dedup / ordering are asserted identically.
from .test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _assert_no_duplicate_render,
    _assert_transcript_parity,
    _ensure_chat_view,
    _item_text,
    _ordered_message_items,
    _select_view_mode,
    _send,
    _turn_prompt,
)

_log = logging.getLogger(__name__)

_TERMINAL_VIEW = '[data-testid="terminal-view"]'
# xterm.js routes all keystrokes through a hidden helper <textarea>; focusing it
# and typing is how a user (and Playwright) drives the embedded TUI.
_XTERM_INPUT = ".xterm-helper-textarea"

# Mock LLM responds instantly; budget covers native CLI boot + terminal attach.
_MOCK_TURN_TIMEOUT_MS = 60_000
# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000

# Must match the model set in the mock anthropic provider config written by the
# native_claude_mock_session fixture (conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

# Two composer turns (the IN direction) + one TUI turn (the OUT direction).
_COMPOSER_TURNS = 2


def _open_terminal_view(page: Page) -> None:
    """Switch a terminal-first session to its Terminal (TUI) view.

    :param page: The Playwright page, on the session's chat surface.
    """
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view_mode(page, "Terminal")


def _wait_terminal_connected(page: Page) -> None:
    """Wait until the embedded xterm has attached to the live Claude TUI.

    :param page: The Playwright page, on the Terminal view.
    """
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _type_into_tui(page: Page, text: str) -> None:
    """Type *text* into the embedded Claude Code TUI and submit with Enter.

    :param page: The Playwright page, on the connected Terminal view.
    :param text: The single-line prompt to type into the TUI.
    """
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.keyboard.type(text, delay=15)
    page.keyboard.press("Enter")


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_native_claude_message_render_parity(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Native Claude Code renders parity with its TUI, both ways, with no dupes (mock LLM)."""
    base_url, session_id = native_claude_mock_session
    _log.info("native-claude mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Claude Code TUI attached (terminal-view connected)")
    _ensure_chat_view(page)

    # Generate all markers up front so the final parity assertion can verify
    # both composer-originated and terminal-originated turns together.
    nonces = [uuid.uuid4().hex[:8] for _ in range(_COMPOSER_TURNS + 1)]
    turns = [
        (f"usr-{i + 1}-{nonces[i]}", f"ast-{i + 1}-{nonces[i]}")
        for i in range(_COMPOSER_TURNS + 1)
    ]
    reset_mock_llm(mock_llm_server_url)

    user_markers: list[str] = []
    assistant_tokens: list[str] = []

    # --- Property 1 & 3: composer turns (IN) render parity, no dupes. ---
    for index, (user_marker, assistant_token) in enumerate(turns[:_COMPOSER_TURNS], start=1):
        user_markers.append(user_marker)
        assistant_tokens.append(assistant_token)
        _log.info(
            "composer turn %d: sending (marker=%s token=%s)", index, user_marker, assistant_token
        )
        set_fallback_mock_llm(mock_llm_server_url, "default", assistant_token)
        set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, assistant_token)
        _send(page, _turn_prompt(index, user_marker, assistant_token))
        expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
            timeout=_MOCK_TURN_TIMEOUT_MS
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
        expect(page.locator(_USER)).to_have_count(index, timeout=30_000)
        _log.info("composer turn %d: settled", index)

    # --- Property 2 & 3: a TUI-originated turn (OUT) surfaces in the web UI. ---
    tui_index = _COMPOSER_TURNS + 1
    tui_marker, tui_token = turns[_COMPOSER_TURNS]
    user_markers.append(tui_marker)
    assistant_tokens.append(tui_token)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info(
        "TUI turn %d: typing into xterm (marker=%s token=%s)", tui_index, tui_marker, tui_token
    )
    set_fallback_mock_llm(mock_llm_server_url, "default", tui_token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, tui_token)
    _type_into_tui(page, _turn_prompt(tui_index, tui_marker, tui_token))

    _ensure_chat_view(page)
    expect(page.locator(_ASSISTANT, has_text=tui_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_USER, has_text=tui_marker).first).to_be_visible(timeout=30_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    expect(page.locator(_USER)).to_have_count(len(user_markers), timeout=30_000)
    _log.info("TUI turn %d: surfaced in web UI (user + assistant bubbles present)", tui_index)

    # --- Assert all three properties over every turn. ---
    _assert_no_duplicate_render(page, user_markers, assistant_tokens)
    _assert_transcript_parity(base_url, session_id, user_markers, assistant_tokens)
    _log.info("all turns verified: render parity + no-duplicate-render + transcript parity")


# --- Journey 2: web-composer delivery into an occupied TUI composer. ---------

# How long to wait for a keystroke to travel browser -> WebSocket -> tmux ->
# Claude Code and for the TUI to repaint the surface it opened.
_TUI_SURFACE_TIMEOUT_S = 20.0
# How many times to re-send a surface's opening keystrokes across that
# budget. Under load the transit can spread a key sequence out until it
# stops reading as one gesture (the rewind dialog needs its two Escapes to
# land as a double tap), so a lost gesture is re-sent — only ever while the
# surface is verifiably absent, where the keys hit the bare idle composer.
_TUI_SURFACE_ATTEMPTS = 3
# The runner advertises the session terminal's private tmux socket + pane here,
# inside the claude-native bridge directory.
_TMUX_ADVERT_FILE = "tmux.json"
# A bubble renders from the streamed turn, which can land a beat before the
# forwarder has persisted the same item into the canonical transcript. The
# transcript assertions read that API, so each turn is given this long to
# settle into it.
_TRANSCRIPT_SETTLE_TIMEOUT_S = 30.0


def _focus_tui(page: Page) -> None:
    """Put keyboard focus on the embedded xterm.

    :param page: The Playwright page, on the connected Terminal view.
    """
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()


def _open_history_search(page: Page) -> None:
    """Open Claude Code's ctrl+r prompt-history search in the TUI.

    :param page: The Playwright page, on the connected Terminal view.
    """
    _focus_tui(page)
    page.keyboard.press("Control+r")


def _enter_shell_mode(page: Page) -> None:
    """Put the TUI composer into ``!`` shell mode.

    :param page: The Playwright page, on the connected Terminal view.
    """
    _focus_tui(page)
    page.keyboard.type("!")


def _open_rewind_dialog(page: Page) -> None:
    """Open the rewind dialog with the double Escape the TUI advertises.

    Needs conversation history — on a session with no turns the second
    Escape opens nothing, which is why the caller sends a baseline turn
    first.

    :param page: The Playwright page, on the connected Terminal view.
    """
    _focus_tui(page)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")


# Surfaces a person can leave occupying the composer from the embedded
# terminal, as (label, opener, text Claude Code renders while it is up).
# Each swallowed a web-composer message before the bridge reclaimed the
# input box: the history search filtered on it and replayed an old prompt
# on Enter, shell mode ran it as a bash command, and the rewind dialog
# dropped it while its Enter committed a checkpoint restore. Markers are
# matched case-insensitively: the TUI's chrome casing drifts across
# Claude Code releases (e.g. 'Search prompts' became 'search prompts').
_OCCUPYING_SURFACES: tuple[tuple[str, Callable[[Page], None], str], ...] = (
    ("ctrl+r history search", _open_history_search, "Search prompts"),
    ("shell mode", _enter_shell_mode, "! for shell mode"),
    ("rewind dialog", _open_rewind_dialog, "Rewind"),
)
# The panels a slash command opens (``/help``, ``/config``, ``/resume``,
# ``/bashes``) are the fourth shape, covered by the bridge's unit tests
# against captured panes instead: driven from here the case is unstable,
# because opening one SUBMITS a turn of its own that the composer message
# then races.


def _tmux_advert(base_url: str, session_id: str) -> dict[str, str] | None:
    """Return the session terminal's tmux advert (socket + pane target).

    The runner writes ``tmux.json`` into the claude-native bridge directory
    once the terminal launches; it is the handle both the bridge and these
    tests use to reach the live TUI.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The parsed advert, or ``None`` before the terminal is up.
    """
    from omnigent.harnesses.claude_native.bridge import (
        BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )

    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    labels = session.get("labels") or {}
    bridge_id = labels.get(BRIDGE_ID_LABEL_KEY) or session_id
    advert = bridge_dir_for_bridge_id(bridge_id) / _TMUX_ADVERT_FILE
    if not advert.exists():
        return None
    return json.loads(advert.read_text(encoding="utf-8"))


def _pane_text(base_url: str, session_id: str) -> str:
    """Capture the session terminal's tmux pane — the TUI's own screen.

    Read straight from tmux rather than from the SPA's xterm: the browser
    renders the terminal on a WebGL canvas, so its glyphs are not in the
    DOM, and the pane is anyway the surface the bridge itself reads.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The pane's visible text, or ``""`` before the terminal has
        been advertised (or if the capture fails).
    """
    info = _tmux_advert(base_url, session_id)
    if info is None:
        return ""
    proc = subprocess.run(
        ["tmux", "-S", info["socket_path"], "capture-pane", "-t", info["tmux_target"], "-p"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _marker_rendered(pane: str, marker: str) -> bool:
    """Return whether the TUI pane renders *marker*, wrap-tolerantly.

    Case-insensitive (the chrome's casing drifts across Claude Code
    releases), and tolerant of a narrow pane wrapping a footer's left
    cell across rows with the right column's text interleaved — each
    row's fragment before the first multi-space column gap is rejoined
    so the marker reads whole again.

    :param pane: The pane's visible text.
    :param marker: Text Claude Code renders while the surface is up.
    :returns: ``True`` when the marker is visible, wrapped or not.
    """
    needle = marker.lower()
    if needle in pane.lower():
        return True
    fragments = (
        re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
        for line in pane.splitlines()
        if line.strip()
    )
    return needle in " ".join(fragments).lower()


def _occupy_tui_surface(
    page: Page,
    base_url: str,
    session_id: str,
    label: str,
    occupy: Callable[[Page], None],
    marker: str,
) -> None:
    """Open a TUI surface and block until the pane proves it rendered.

    Without the proof the journey would pass vacuously: a keystroke that
    never reached Claude Code leaves an ordinary composer, which of course
    accepts the message. A gesture the transit spread out (so it never
    registered) is re-sent up to :data:`_TUI_SURFACE_ATTEMPTS` times — only
    while the marker is verifiably absent, so the retry keys can only land
    on the bare idle composer, never on the opened surface.

    :param page: The Playwright page, on the connected Terminal view.
    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :param label: The surface's name for the log, e.g. ``"shell mode"``.
    :param occupy: Sends the keystrokes that open the surface.
    :param marker: Text Claude Code renders while the surface is up,
        matched via :func:`_marker_rendered`.
    :raises AssertionError: If the marker never appears in the pane.
    """
    per_attempt_s = _TUI_SURFACE_TIMEOUT_S / _TUI_SURFACE_ATTEMPTS
    pane = ""
    for attempt in range(1, _TUI_SURFACE_ATTEMPTS + 1):
        occupy(page)
        deadline = time.monotonic() + per_attempt_s
        while time.monotonic() < deadline:
            pane = _pane_text(base_url, session_id)
            if _marker_rendered(pane, marker):
                return
            page.wait_for_timeout(500)
        _log.info(
            "%s did not render %r within %.1fs (attempt %d/%d); re-sending its keystrokes",
            label,
            marker,
            per_attempt_s,
            attempt,
            _TUI_SURFACE_ATTEMPTS,
        )
    raise AssertionError(
        f"TUI never rendered {marker!r} in {_TUI_SURFACE_ATTEMPTS} attempts over "
        f"{_TUI_SURFACE_TIMEOUT_S}s — the keystrokes did not reach Claude Code, so this "
        f"case would prove nothing. Pane was:\n{pane}"
    )


def _wait_for_transcript_message(
    page: Page, base_url: str, session_id: str, marker: str, *, role: str
) -> None:
    """Block until the canonical transcript holds *marker* in a *role* message.

    Reads the same ``type == "message"`` items the TUI renders from, which is
    what tells a delivered chat message from a shell-mode one: text handed to
    bash comes back as a ``terminal_command`` item, so it never lands here —
    even though it does draw a bubble and does make Claude answer.

    :param page: The Playwright page (used for its polling sleep).
    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :param marker: Unique text the turn carried.
    :param role: ``"user"`` or ``"assistant"``.
    :raises AssertionError: If the marker never reaches such an item.
    """
    deadline = time.monotonic() + _TRANSCRIPT_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        items = _ordered_message_items(base_url, session_id)
        if any(item.get("role") == role and marker in _item_text(item) for item in items):
            return
        page.wait_for_timeout(500)
    raise AssertionError(
        f"{marker!r} never reached the canonical transcript as a {role} message within "
        f"{_TRANSCRIPT_SETTLE_TIMEOUT_S}s — it was not delivered as a chat turn"
    )


def _send_composer_turn(
    page: Page,
    mock_llm_server_url: str,
    *,
    base_url: str,
    session_id: str,
    index: int,
    user_marker: str,
    assistant_token: str,
    expected_user_bubbles: int,
    steer_queued: bool = False,
) -> None:
    """Send one composer turn and assert it landed as a chat turn, answered.

    Both halves are asserted, because an occupied composer can fail either
    way: a swallowed message never draws an assistant bubble at all, while a
    message handed to ``!`` shell mode runs as a bash command that Claude
    then answers — a reply, a bubble, but no user message in the transcript.

    :param page: The Playwright page, on the session's chat surface.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :param index: 1-based turn number, for the prompt text.
    :param user_marker: Unique token embedded in the user message.
    :param assistant_token: Unique token the agent must echo back.
    :param expected_user_bubbles: How many user bubbles the transcript must
        hold once this turn has landed.
    :param steer_queued: Explicitly send a queued turn while a native dialog
        keeps the session busy.
    """
    set_fallback_mock_llm(mock_llm_server_url, "default", assistant_token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, assistant_token)
    if steer_queued:
        expect(page.locator(_WORKING)).to_be_visible(timeout=30_000)
    _send(page, _turn_prompt(index, user_marker, assistant_token))
    if steer_queued:
        queued = page.get_by_test_id("composer-queued-strip")
        expect(queued).to_contain_text(user_marker, timeout=30_000)
        expect(page.locator(_USER)).to_have_count(expected_user_bubbles - 1)
        queued.get_by_role("button", name="Send queued message now", exact=True).click()
        expect(queued).to_be_hidden(timeout=30_000)
    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    expect(page.locator(_USER)).to_have_count(expected_user_bubbles, timeout=30_000)
    _wait_for_transcript_message(page, base_url, session_id, user_marker, role="user")
    _wait_for_transcript_message(page, base_url, session_id, assistant_token, role="assistant")


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_native_claude_composer_delivers_into_an_occupied_tui(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A web-composer message lands even when the TUI composer is occupied.

    The embedded terminal shares one composer with the web chat, so anything
    a person leaves open there — a ctrl+r history search, ``!`` shell mode,
    the rewind dialog — used to take the injected keystrokes instead: the
    message was silently dropped, or worse, executed as a shell command. The
    bridge now reclaims the input box first. Each case opens its surface with
    real keystrokes through the xterm, verifies the TUI actually rendered it,
    then sends from the chat composer and asserts the message arrived as a
    user turn and was answered. Rewind reports a busy session, so its message
    is explicitly steered from the queue to exercise bridge delivery.
    """
    base_url, session_id = native_claude_mock_session
    _log.info("occupied-composer journey: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    reset_mock_llm(mock_llm_server_url)

    nonces = [uuid.uuid4().hex[:8] for _ in range(len(_OCCUPYING_SURFACES) + 1)]
    turns = [(f"usr-{i + 1}-{nonce}", f"ast-{i + 1}-{nonce}") for i, nonce in enumerate(nonces)]
    user_markers: list[str] = []
    assistant_tokens: list[str] = []

    # Baseline turn on a free composer: proves delivery works before anything
    # occupies it, and gives the session the history the rewind dialog needs.
    baseline_marker, baseline_token = turns[0]
    user_markers.append(baseline_marker)
    assistant_tokens.append(baseline_token)
    _send_composer_turn(
        page,
        mock_llm_server_url,
        base_url=base_url,
        session_id=session_id,
        index=1,
        user_marker=baseline_marker,
        assistant_token=baseline_token,
        expected_user_bubbles=1,
    )
    _log.info("baseline turn settled on a free composer")

    for offset, (label, occupy, pane_marker) in enumerate(_OCCUPYING_SURFACES):
        index = offset + 2
        user_marker, assistant_token = turns[offset + 1]
        user_markers.append(user_marker)
        assistant_tokens.append(assistant_token)

        _open_terminal_view(page)
        _wait_terminal_connected(page)
        _log.info("occupying the TUI composer with %s", label)
        _occupy_tui_surface(page, base_url, session_id, label, occupy, pane_marker)
        _log.info("%s is up in the TUI; sending from the web composer", label)

        _ensure_chat_view(page)
        _send_composer_turn(
            page,
            mock_llm_server_url,
            base_url=base_url,
            session_id=session_id,
            index=index,
            user_marker=user_marker,
            assistant_token=assistant_token,
            expected_user_bubbles=index,
            steer_queued=label == "rewind dialog",
        )
        _log.info("%s: message delivered as a user turn and answered", label)

    _assert_no_duplicate_render(page, user_markers, assistant_tokens)
    _assert_transcript_parity(base_url, session_id, user_markers, assistant_tokens)
    _log.info("every occupied surface released the composer; all turns landed once")


# --- Journey 3: a message the TUI never recorded must not duplicate the next one. ---

# The server records a web message as a queued input before the runner pastes
# it and only drops that record once the transcript mirrors the message back.
_QUEUED_INPUT_TIMEOUT_S = 30.0
# Tunnel teardown after SIGKILL is asynchronous (see chat/test_stale_stream.py).
_RUNNER_OFFLINE_TIMEOUT_S = 20.0
# A respawned runner boots a fresh Claude Code (``--resume``) before the next
# message can be pasted, so that turn gets a cold-boot budget.
_RESUMED_TURN_TIMEOUT_MS = 180_000
# Headline the SPA gives an undelivered web message (StatusBlocks.tsx).
_UNDELIVERED_HEADLINE = "was not delivered"


def _pane_process_ids(advert: dict[str, str]) -> list[int]:
    """Return the pane's root process and its descendants (Claude Code and children).

    :param advert: The session terminal's tmux advert (see :func:`_tmux_advert`).
    :returns: Process ids, roots first.
    """
    listed = subprocess.run(
        [
            "tmux",
            "-S",
            advert["socket_path"],
            "list-panes",
            "-t",
            advert["tmux_target"],
            "-F",
            "#{pane_pid}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    pending = [int(pid) for pid in listed.stdout.split() if pid.isdigit()]
    pids: list[int] = []
    while pending:
        pid = pending.pop(0)
        pids.append(pid)
        children = subprocess.run(
            ["pgrep", "-P", str(pid)], check=False, capture_output=True, text=True, timeout=10
        )
        pending.extend(int(child) for child in children.stdout.split() if child.isdigit())
    return pids


def _signal_each(pids: list[int], sig: signal.Signals) -> None:
    """Send *sig* to every pid, ignoring ones that already exited.

    :param pids: Target process ids.
    :param sig: Signal to deliver, e.g. ``signal.SIGSTOP``.
    """
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)


def _queued_input_texts(base_url: str, session_id: str) -> list[str]:
    """Return the text of every web message the server still holds as queued.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: One entry per ``pending_inputs`` record, in queue order.
    """
    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    texts: list[str] = []
    for entry in session.get("pending_inputs") or []:
        texts.append(
            " ".join(
                block["text"]
                for block in entry.get("content") or []
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
        )
    return texts


def _wait_for_queued_input(page: Page, base_url: str, session_id: str, marker: str) -> None:
    """Block until the server holds a queued web message carrying *marker*.

    :param page: The Playwright page (used for its polling sleep).
    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :param marker: Unique text of the message that must be queued.
    :raises AssertionError: If the server never records it.
    """
    deadline = time.monotonic() + _QUEUED_INPUT_TIMEOUT_S
    queued: list[str] = []
    while time.monotonic() < deadline:
        queued = _queued_input_texts(base_url, session_id)
        if any(marker in text for text in queued):
            return
        page.wait_for_timeout(500)
    raise AssertionError(
        f"the server never held {marker!r} as a queued input within "
        f"{_QUEUED_INPUT_TIMEOUT_S}s; queued: {queued!r}"
    )


def _wait_for_runner_offline(page: Page, base_url: str, session_id: str) -> None:
    """Block until the health endpoint reports the session's runner offline.

    :param page: The Playwright page (used for its polling sleep).
    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :raises AssertionError: If the runner is still reported online at the deadline.
    """
    deadline = time.monotonic() + _RUNNER_OFFLINE_TIMEOUT_S
    health: dict[str, object] = {}
    while time.monotonic() < deadline:
        health = httpx.get(
            f"{base_url}/health", params={"session_id": session_id}, timeout=5
        ).json()
        session = health.get("session")
        if isinstance(session, dict) and session.get("runner_online") is False:
            return
        page.wait_for_timeout(500)
    raise AssertionError(f"runner still reported online after the kill: {health}")


def _send_turn_and_settle(
    page: Page,
    mock_llm_server_url: str,
    *,
    base_url: str,
    session_id: str,
    index: int,
    user_marker: str,
    assistant_token: str,
    expected_assistant_bubbles: int,
    turn_timeout_ms: int = _MOCK_TURN_TIMEOUT_MS,
) -> None:
    """Send one composer turn and wait until Claude has answered it.

    Settles on the assistant bubble count rather than the echoed token, so the
    journey holds whether the turn is served by the mock or by a real model.

    :param page: The Playwright page, on the session's chat surface.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :param index: 1-based turn number, for the prompt text.
    :param user_marker: Unique token embedded in the user message.
    :param assistant_token: Unique token the agent is asked to echo back.
    :param expected_assistant_bubbles: Assistant bubbles once this turn is answered.
    :param turn_timeout_ms: Budget for the answer to render.
    """
    set_fallback_mock_llm(mock_llm_server_url, "default", assistant_token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, assistant_token)
    _send(page, _turn_prompt(index, user_marker, assistant_token))
    expect(page.locator(_ASSISTANT)).to_have_count(
        expected_assistant_bubbles, timeout=turn_timeout_ms
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=turn_timeout_ms)
    _wait_for_transcript_message(page, base_url, session_id, user_marker, role="user")


def _assert_user_bubbles_are_their_committed_items(
    page: Page, base_url: str, session_id: str, markers: list[str]
) -> None:
    """Assert each marker renders as exactly one bubble carrying its own item id.

    A user bubble's ``data-message-id`` is the committed transcript item it
    was promoted into (an optimistic bubble still awaiting its receipt carries
    a client temp id instead). A receipt that names the wrong queued message
    leaves a bubble promoted under another message's id and the real message
    stranded as optimistic — the state behind the duplicate the person sees
    once history is merged back in.

    :param page: The Playwright page, on the settled chat surface.
    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :param markers: Unique per-message markers, oldest first.
    """
    committed = {
        str(item.get("id")): _item_text(item)
        for item in _ordered_message_items(base_url, session_id)
        if item.get("role") == "user"
    }
    bubbles: list[list[str]] = page.locator(_USER).evaluate_all(
        "els => els.map(e => [e.getAttribute('data-message-id') ?? '', e.innerText])"
    )
    for marker in markers:
        matching = [(item_id, text) for item_id, text in bubbles if marker in text]
        assert len(matching) == 1, f"{marker!r} rendered {len(matching)}x: {bubbles!r}"
        item_id, _text = matching[0]
        assert marker in committed.get(item_id, ""), (
            f"the bubble for {marker!r} carries item id {item_id!r}, which is "
            f"{committed.get(item_id, 'not a committed user message')!r}; "
            f"committed user items: {committed!r}"
        )


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_native_claude_lost_message_does_not_duplicate_the_next_one(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    _recover_shared_runner: Callable[[], None],
) -> None:
    """A message Claude Code never recorded must not double-render the next one.

    The shape of a host reconnect: the runner accepts a web message into a TUI
    that then never records it (here the TUI is frozen, so the paste is never
    read), the runner and its terminal go away, a fresh runner resumes the
    session, and the person keeps chatting in the SAME tab. The server still
    holds the lost message as a queued input. When the next message is mirrored
    back, that stale record must not be the one the server drains: the receipt
    would name the wrong message, the tab would keep the new message's
    optimistic bubble beside its committed copy, and the lost message would
    vanish without a trace — while a fresh tab showed everything once.
    """
    base_url, session_id = native_claude_mock_session
    _log.info("lost-message journey: base_url=%s session_id=%s", base_url, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    reset_mock_llm(mock_llm_server_url)

    nonce = uuid.uuid4().hex[:8]
    first_marker, first_token = f"usr-1-{nonce}", f"ast-1-{nonce}"
    lost_marker, lost_token = f"usr-2-{nonce}", f"ast-2-{nonce}"
    next_marker, next_token = f"usr-3-{nonce}", f"ast-3-{nonce}"

    # Turn 1 on a healthy TUI: proves delivery and gives Claude Code a
    # transcript for the resume below.
    _send_turn_and_settle(
        page,
        mock_llm_server_url,
        base_url=base_url,
        session_id=session_id,
        index=1,
        user_marker=first_marker,
        assistant_token=first_token,
        expected_assistant_bubbles=1,
    )
    _log.info("turn 1 settled")

    # Freeze Claude Code. The bridge still pastes and submits (blind, once its
    # draft-visibility budget lapses), so the runner accepts the message and
    # the server keeps its queued record — but the TUI never reads the paste.
    advert = _tmux_advert(base_url, session_id)
    assert advert is not None, "the Claude terminal advertised no tmux pane"
    pane_pids = _pane_process_ids(advert)
    assert pane_pids, "the Claude terminal pane owns no process"
    _signal_each(pane_pids, signal.SIGSTOP)
    _log.info("froze the Claude Code pane (pids=%s); sending the message it will lose", pane_pids)
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/v1/sessions/{session_id}/events")
        ),
        timeout=60_000,
    ) as posted:
        _send(page, _turn_prompt(2, lost_marker, lost_token))
    assert posted.value.ok, f"the frozen-TUI message was refused: {posted.value.status}"
    expect(page.locator(_USER, has_text=lost_marker)).to_have_count(1)
    _wait_for_queued_input(page, base_url, session_id, lost_marker)
    _log.info("server holds the lost message as a queued input")

    # The host goes away: Claude Code, its tmux server, and the runner all die.
    _signal_each(pane_pids, signal.SIGKILL)
    subprocess.run(
        ["tmux", "-S", advert["socket_path"], "kill-server"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    os.kill(int(str(_server_state["runner_pid"])), signal.SIGKILL)
    _wait_for_runner_offline(page, base_url, session_id)
    _log.info("runner and terminal are gone; bringing a fresh runner back")

    # The host comes back: a fresh runner registers under the same id and
    # resumes the session's Claude Code when the next message arrives.
    _recover_shared_runner()
    _send_turn_and_settle(
        page,
        mock_llm_server_url,
        base_url=base_url,
        session_id=session_id,
        index=3,
        user_marker=next_marker,
        assistant_token=next_token,
        expected_assistant_bubbles=2,
        turn_timeout_ms=_RESUMED_TURN_TIMEOUT_MS,
    )
    _log.info("turn 3 settled after the reconnect")
    # The reported symptom: the tab that lived through the reconnect must show
    # the new message once, not committed copy + stranded optimistic bubble.
    expect(page.locator(_USER, has_text=next_marker)).to_have_count(1)
    expect(page.locator(_USER)).to_have_count(3)
    # And every bubble must be the message it says it is: the receipt for the
    # new message must not have promoted the lost message's bubble under the
    # new item id while the new message's own bubble stays optimistic.
    markers = [first_marker, lost_marker, next_marker]
    _assert_user_bubbles_are_their_committed_items(page, base_url, session_id, markers)
    # The lost message is flagged as undelivered instead of silently vanishing.
    expect(
        page.get_by_test_id("error-headline").filter(has_text=_UNDELIVERED_HEADLINE)
    ).to_have_count(1)

    # The canonical transcript agrees: each message once, in send order.
    user_texts = [
        _item_text(item)
        for item in _ordered_message_items(base_url, session_id)
        if item.get("role") == "user"
    ]
    assert [marker for text in user_texts for marker in markers if marker in text] == markers, (
        f"transcript user messages out of order or duplicated: {user_texts!r}"
    )

    # A fresh tab renders the same three messages — what the person saw when
    # they opened the conversation a second time.
    fresh = page.context.new_page()
    try:
        fresh.goto(f"{base_url}/c/{session_id}")
        expect(fresh.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=30_000)
        _select_view_mode(fresh, "Chat")
        expect(fresh.locator(_USER, has_text=next_marker)).to_have_count(1, timeout=30_000)
        expect(fresh.locator(_USER)).to_have_count(3, timeout=30_000)
    finally:
        fresh.close()
    _log.info("lost message surfaced once as undelivered; next message rendered once")
