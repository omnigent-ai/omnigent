r"""UI journey: a Codex TUI ``/new`` must notify the superseded web conversation.

Native Codex ``/new`` starts a fresh app-server thread; the codex-native
forwarder rotates the Omnigent binding onto a replacement conversation and
transfers the terminal. A user watching the OLD conversation in the web UI
must be told what happened, exactly as claude-native's
``_post_clear_supersession`` does after a ``/clear`` rotation: the client
auto-redirects to the replacement conversation (the transient
``session.superseded`` event) and the old transcript gains a durable assistant
notice linking to it.

Journey (real ``codex`` CLI, mock LLM): open the conversation, exchange one
composer turn, switch to the Terminal view, run ``/new`` in the TUI, and wait
for the rotation to land (the bridge state's ``session_id`` moves to the
replacement). Then assert the old conversation's viewer is redirected and the
old transcript links to the new conversation. While the bug is live the
rotation completes silently, so the redirect assertion times out on the dead
conversation's URL.

Marked ``nightly``: boots a real codex CLI and drives its TUI, like
``tests/e2e_ui/messages/test_native_codex_render_parity.py``.
"""

from __future__ import annotations

import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native.bridge import (
    bridge_dir_for_bridge_id,
    read_bridge_state,
)
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _item_text,
    _ordered_message_items,
    _send,
    _turn_prompt,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

_ROTATION_TIMEOUT_S = 90.0
_REDIRECT_TIMEOUT_MS = 30_000
_NOTICE_TIMEOUT_S = 30.0


def _wait_for_rotation(old_session_id: str, *, timeout_s: float = _ROTATION_TIMEOUT_S) -> str:
    """Wait until codex-native rotates the bridge onto a replacement session.

    ``_create_thread_replacement_session`` rewrites the bridge state (keyed by
    the original session id) with the replacement session id as the final
    rotation step, so this is the harness's own signal that ``/new`` landed.
    In the e2e_ui harness the runner is in-process on this machine, so the
    file is directly readable.

    :param old_session_id: The session id ``/new`` rotates away from (also the
        codex-native bridge id).
    :param timeout_s: Max seconds to wait for the rotation to land.
    :returns: The replacement Omnigent session id.
    :raises AssertionError: When no rotation lands within the budget — a
        TUI-driving problem, distinct from the missing-notification bug this
        test guards.
    """
    deadline = time.monotonic() + timeout_s
    observed: str | None = None
    while time.monotonic() < deadline:
        state = read_bridge_state(bridge_dir_for_bridge_id(old_session_id))
        if state is not None:
            observed = state.session_id
            if observed and observed != old_session_id:
                return observed
        time.sleep(0.5)
    raise AssertionError(
        f"codex /new did not rotate the Omnigent session within {timeout_s:.0f}s "
        f"(bridge session_id still {observed!r}); this is a TUI-driving problem, "
        "not the supersession-notice bug under test"
    )


def _wait_for_supersession_notice(base_url: str, old_session_id: str, new_session_id: str) -> None:
    """Wait for the durable continuation notice on the superseded conversation.

    :param base_url: Spawned server base URL.
    :param old_session_id: The superseded conversation id.
    :param new_session_id: The replacement conversation the notice must link.
    :raises AssertionError: When no assistant message linking to the
        replacement conversation appears within the budget.
    """
    deadline = time.monotonic() + _NOTICE_TIMEOUT_S
    while time.monotonic() < deadline:
        for item in _ordered_message_items(base_url, old_session_id):
            if item.get("role") == "assistant" and f"/c/{new_session_id}" in _item_text(item):
                return
        time.sleep(1.0)
    raise AssertionError(
        "the superseded conversation never received an assistant notice "
        f"linking to the replacement conversation /c/{new_session_id}"
    )


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_codex_new_rotation_notifies_superseded_conversation(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A ``/new`` in the Codex TUI redirects and annotates the old conversation."""
    base_url, old_session_id = native_codex_mock_session

    page.goto(f"{base_url}/c/{old_session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    nonce = uuid.uuid4().hex[:8]
    user_marker = f"usr-1-{nonce}"
    assistant_token = f"ast-1-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}],
        key=user_marker,
        match=user_marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    _send(page, _turn_prompt(1, user_marker, assistant_token))
    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _type_into_tui(page, "/new")

    new_session_id = _wait_for_rotation(old_session_id)
    replacement = httpx.get(f"{base_url}/v1/sessions/{new_session_id}", timeout=10.0)
    replacement.raise_for_status()

    # Best-effort return to the chat surface: the toggle can vanish once the
    # terminal transfers to the replacement session.
    chat_segment = page.get_by_test_id("view-mode-chat")
    if chat_segment.is_visible():
        chat_segment.click()

    expect(page).to_have_url(
        re.compile(re.escape(f"/c/{new_session_id}")), timeout=_REDIRECT_TIMEOUT_MS
    )

    _wait_for_supersession_notice(base_url, old_session_id, new_session_id)
