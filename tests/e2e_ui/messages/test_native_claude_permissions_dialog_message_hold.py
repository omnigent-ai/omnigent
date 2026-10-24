r"""UI journey: a web message sent while Claude Code's ``/permissions`` dialog is open.

The runner backs out of the dialog with Escape and delivers the message instead
of holding it behind what looks like a pending question.

Kept apart from the occupied-composer journey in
``test_native_claude_render_parity``: a slash-command dialog submits a turn of
its own, so the session reads busy and the message has to be steered out of
the queue, and the dialog is left two Escapes deep on its add-rule field.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

import pytest
from playwright.sync_api import Page

from tests.e2e_ui.conftest import reset_mock_llm

from .test_message_render_parity import _ensure_chat_view
from .test_native_claude_render_parity import (
    _focus_tui,
    _marker_rendered,
    _open_terminal_view,
    _pane_text,
    _send_composer_turn,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

# The add-rule field's title and footer on this Claude Code build.
_RULE_EDITOR_TITLE = "permission rule"
_RULE_EDITOR_FOOTER = ("Enter to submit", "Esc to cancel")
_ADD_RULE_ROW = "❯ 1. Add a new rule"
# The rule list's footer offers tab switching; transcript text only ever says "/model to switch".
_LIST_FOOTER = "←/→ to switch"
_DIALOG_TIMEOUT_S = 40.0
_ALLOW_RULE = "Bash(echo:*)"


def _rule_editor_open(pane: str) -> bool:
    return _marker_rendered(pane, _RULE_EDITOR_TITLE) and all(
        _marker_rendered(pane, hint) for hint in _RULE_EDITOR_FOOTER
    )


def _permissions_dialog_open(pane: str) -> bool:
    return _rule_editor_open(pane) or _marker_rendered(pane, _LIST_FOOTER)


def _wait_for_pane(
    base_url: str, session_id: str, predicate: Callable[[str], bool], *, what: str
) -> str:
    deadline = time.monotonic() + _DIALOG_TIMEOUT_S
    pane = ""
    while time.monotonic() < deadline:
        pane = _pane_text(base_url, session_id)
        if predicate(pane):
            return pane
        time.sleep(1.0)
    raise AssertionError(
        f"TUI never showed {what} within {_DIALOG_TIMEOUT_S}s; last pane:\n{pane}"
    )


def _open_add_rule_field(page: Page, base_url: str, session_id: str) -> None:
    """Open /permissions, focus the Allow list, and open the add-rule field.

    Leaves the dialog on the "Enter permission rule…" field, two Escapes deep.
    """
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _focus_tui(page)
    page.keyboard.type("/permissions", delay=40)
    _wait_for_pane(
        base_url,
        session_id,
        lambda pane: _marker_rendered(pane, "/permissions"),
        what="the typed command",
    )
    page.keyboard.press("Enter")
    _wait_for_pane(
        base_url,
        session_id,
        lambda pane: (
            _marker_rendered(pane, _LIST_FOOTER) and _marker_rendered(pane, "Esc to cancel")
        ),
        what="the /permissions dialog",
    )
    _log.info("permissions dialog open")
    page.keyboard.press("ArrowDown")
    _wait_for_pane(
        base_url,
        session_id,
        lambda pane: _marker_rendered(pane, _ADD_RULE_ROW),
        what="the add-rule row",
    )
    page.keyboard.press("Enter")
    _wait_for_pane(
        base_url,
        session_id,
        lambda pane: _marker_rendered(pane, _RULE_EDITOR_TITLE),
        what="the rule editor",
    )
    page.keyboard.type(_ALLOW_RULE, delay=40)
    _wait_for_pane(
        base_url,
        session_id,
        lambda pane: _rule_editor_open(pane) and _marker_rendered(pane, _ALLOW_RULE),
        what="the typed rule",
    )
    _log.info("add-rule field open with %s typed", _ALLOW_RULE)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_web_message_is_delivered_past_an_open_permissions_dialog(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A message sent while the /permissions dialog is open is delivered and answered."""
    base_url, session_id = native_claude_mock_session
    # Create the page after the session exists, so a recording starts on the journey.
    page: Page = request.getfixturevalue("page")
    _log.info("permissions-dialog journey: base_url=%s session_id=%s", base_url, session_id)
    (baseline_marker, baseline_token), (held_marker, held_token) = (
        (f"usr-{i + 1}-{uuid.uuid4().hex[:8]}", f"ast-{i + 1}-{uuid.uuid4().hex[:8]}")
        for i in range(2)
    )

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    reset_mock_llm(mock_llm_server_url)
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
    _log.info("baseline turn answered")

    _open_add_rule_field(page, base_url, session_id)
    _ensure_chat_view(page)
    # The open dialog keeps the session busy, so the message is steered from the queue.
    _log.info("sending from the web composer with the dialog open")
    _send_composer_turn(
        page,
        mock_llm_server_url,
        base_url=base_url,
        session_id=session_id,
        index=2,
        user_marker=held_marker,
        assistant_token=held_token,
        expected_user_bubbles=2,
        steer_queued=True,
    )
    _log.info("message delivered and answered")

    pane = _pane_text(base_url, session_id)
    assert not _permissions_dialog_open(pane), (
        f"the reply arrived but the /permissions dialog is still open:\n{pane}"
    )
    # Hold the reply, then end on the terminal the runner backed out of, so a
    # recording of this journey shows the dialog gone as well.
    page.wait_for_timeout(2_000)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("terminal view shows the dialog closed")
    page.wait_for_timeout(2_000)
