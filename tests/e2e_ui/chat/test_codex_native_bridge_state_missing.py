"""E2E: a codex-native web turn survives its bridge dir being torn down.

A runner teardown (``_delete_native_bridge_dirs``) can remove a session's
native bridge dir while its tmux pane stays alive. The next composer turn must
still be delivered: the turn-time self-heal relaunches Codex and the reply
renders instead of the error pill ``inner executor error: Codex native bridge
state is missing``.
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native.bridge import bridge_dir_for_bridge_id
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

# The booted TUI writes state.json asynchronously a beat after the terminal WS
# connects.
_STATE_WRITE_TIMEOUT_S = 90.0
# Delivery after the teardown covers a full Codex relaunch plus the executor's
# wait for the fresh bridge state; the mock LLM then answers instantly.
_TURN_DELIVERY_TIMEOUT_MS = 180_000

_MISSING_STATE_MESSAGE = "Codex native bridge state is missing"

# Runner-written bridge files; all absent is the production teardown signature.
# Kept local so the base revision fails on behavior, not on an import.
_BRIDGE_FILES = ("state.json", "startup_error.json", "bridge.json")


def _bridge_torn_down(bridge_dir: Path) -> bool:
    return not any((bridge_dir / name).is_file() for name in _BRIDGE_FILES)


def _wait_for_state_file(state_file: Path, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if state_file.is_file():
            return
        time.sleep(0.5)
    raise AssertionError(
        f"Codex bridge state was never written at {state_file}; the codex-native "
        "TUI did not boot far enough to record its thread state."
    )


def _tear_down_bridge_dir(bridge_dir: Path, timeout_s: float = 10.0) -> None:
    """Remove the runner-written bridge files even while the TUI writes into the dir."""
    deadline = time.monotonic() + timeout_s
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            shutil.rmtree(bridge_dir)
        except FileNotFoundError:
            pass
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
            continue
        last_error = None
        time.sleep(0.5)
        if _bridge_torn_down(bridge_dir):
            return
    if last_error is not None:
        raise AssertionError(f"bridge dir could not be removed at {bridge_dir}: {last_error!r}")
    remaining = [name for name in _BRIDGE_FILES if (bridge_dir / name).is_file()]
    raise AssertionError(
        f"bridge files were rewritten faster than they could be removed at "
        f"{bridge_dir}; still present: {remaining!r}"
    )


def _expanded_error_pill_texts(page: Page) -> list[str]:
    pills = page.get_by_test_id("error-pill")
    for index in range(pills.count()):
        toggles = pills.nth(index).locator('button[aria-expanded="false"]')
        if toggles.count() > 0:
            toggles.first.click()
    contents = page.get_by_test_id("error-message-content")
    return contents.all_inner_texts()


def _wait_turn_outcome(page: Page, assistant_token: str, timeout_ms: int) -> str:
    """Return ``"delivered"``, ``"missing_state"``, or ``"timeout"``."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    assistant = page.locator(_ASSISTANT, has_text=assistant_token).first
    while time.monotonic() < deadline:
        if assistant.count() > 0 and assistant.is_visible():
            return "delivered"
        if page.get_by_test_id("error-pill").count() > 0:
            if any(_MISSING_STATE_MESSAGE in text for text in _expanded_error_pill_texts(page)):
                return "missing_state"
        page.wait_for_timeout(1000)
    return "timeout"


@pytest.mark.timeout(360)
def test_codex_native_turn_delivers_after_bridge_dir_teardown(
    request: pytest.FixtureRequest,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A composer turn still delivers when the bridge dir was torn down."""
    base_url, session_id = native_codex_mock_session

    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # No bridge-id label is set, so the bridge id defaults to the session id;
    # the runner shares this process's HOME, so the path resolves identically.
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    _wait_for_state_file(bridge_dir / "state.json", _STATE_WRITE_TIMEOUT_S)

    nonce = uuid.uuid4().hex[:8]
    user_marker = f"usr-{nonce}"
    assistant_token = f"ast-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}],
        key=user_marker,
        match=user_marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    _tear_down_bridge_dir(bridge_dir)
    assert _bridge_torn_down(bridge_dir)

    _send(page, _turn_prompt(1, user_marker, assistant_token))

    outcome = _wait_turn_outcome(page, assistant_token, _TURN_DELIVERY_TIMEOUT_MS)
    pill_texts = _expanded_error_pill_texts(page)
    assert outcome == "delivered", (
        "assistant reply never rendered after the bridge-dir teardown "
        f"(outcome={outcome!r}); error pills seen: {pill_texts!r}"
    )

    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible()
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    assert not any(_MISSING_STATE_MESSAGE in text for text in pill_texts), (
        f"turn delivered but a failed-turn pill still surfaced "
        f"{_MISSING_STATE_MESSAGE!r}: {pill_texts!r}"
    )
