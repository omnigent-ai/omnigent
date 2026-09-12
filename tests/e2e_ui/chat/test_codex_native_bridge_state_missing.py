"""E2E: a codex-native web turn survives its bridge dir being torn down.

A runner teardown (session delete / resource cleanup) removes a session's
native bridge dir (``_delete_native_bridge_dirs``) while its tmux pane can
stay alive. A composer turn racing such a teardown used to find neither
bridge ``state.json`` nor a recorded startup error: the executor waited out
its poll and the turn surfaced to the UI as a failed error pill reading
``inner executor error: Codex native bridge state is missing``. The
turn-time self-heal (``_ensure_native_terminal_for_turn``) probed only pane
liveness, so the live pane masked the missing bridge and nothing ever
relaunched Codex.

Journey driven here (all real product paths, on a live server + runner):

1. Create a real ``codex-native`` session; the runner auto-launches the
   Codex TUI, which writes bridge ``state.json`` once its thread starts.
2. Confirm the TUI is live (terminal view connected) and its bridge state
   was written.
3. Tear the whole bridge dir out from under the live pane — the effect of
   the runner teardown path — while the tmux pane survives.
4. Send a web message from the composer.
5. The turn must be delivered: the self-heal detects the torn-down bridge,
   relaunches Codex (fresh bridge state), and the assistant reply renders in
   chat — no failed-turn error pill reading "Codex native bridge state is
   missing".

This is the durable regression guard: it fails on a tree where a live pane
masks a torn-down bridge (the turn errors instead of delivering) and passes
once turn delivery restores the bridge first.
"""

from __future__ import annotations

import hashlib
import shutil
import time
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native.bridge import bridge_root
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

# The booted TUI writes state.json asynchronously once its thread starts, a
# beat after the terminal WS connects.
_STATE_WRITE_TIMEOUT_S = 90.0
# Delivery after the teardown covers a full Codex relaunch (the self-heal
# closes the stale pane and boots a fresh TUI) plus the executor's wait for
# the fresh bridge state; the mock LLM then answers instantly.
_TURN_DELIVERY_TIMEOUT_MS = 180_000

_MISSING_STATE_MESSAGE = "Codex native bridge state is missing"


def _codex_bridge_dir(session_id: str) -> Path:
    """Return the runner's Codex bridge dir for *session_id*.

    The codex-native session carries no explicit bridge-id label, so the
    bridge id defaults to the conversation id and the dir is
    ``bridge_root() / sha256(session_id)[:32]`` (see
    ``bridge_dir_for_bridge_id``). The shared ``live_server`` runner inherits
    this process's ``HOME``, so ``bridge_root()`` resolves identically here.

    :param session_id: The codex-native conversation id.
    :returns: Absolute bridge directory the runner writes state into.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return bridge_root() / digest


def _wait_for_state_file(state_file: Path, timeout_s: float) -> None:
    """Block until the booted TUI has written its bridge ``state.json``.

    :param state_file: The bridge dir's ``state.json`` path.
    :param timeout_s: Max seconds to wait for the file to appear.
    :raises AssertionError: If the file never appears (the TUI didn't boot).
    """
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
    """Remove *bridge_dir* even while the live TUI is still writing into it.

    :param bridge_dir: The session's Codex bridge directory.
    :param timeout_s: Max seconds to keep retrying the removal.
    :raises AssertionError: If the dir cannot be removed within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            shutil.rmtree(bridge_dir)
        except FileNotFoundError:
            pass
        except OSError:
            time.sleep(0.25)
            continue
        time.sleep(0.5)
        if not bridge_dir.exists():
            return
    recreated = [str(p.relative_to(bridge_dir)) for p in bridge_dir.rglob("*")][:20]
    raise AssertionError(
        f"could not remove the bridge dir at {bridge_dir}; recreated contents: {recreated!r}"
    )


def _expanded_error_pill_texts(page: Page) -> list[str]:
    """Expand every failed-turn error pill and return its raw message texts.

    The pill headline is a friendly, code-derived sentence; the raw executor
    message lives in the expanded body (``error-message-content``).

    :param page: Playwright page fixture, on the chat view.
    :returns: Raw message text of every rendered error pill.
    """
    pills = page.get_by_test_id("error-pill")
    for index in range(pills.count()):
        toggles = pills.nth(index).locator('button[aria-expanded="false"]')
        if toggles.count() > 0:
            toggles.first.click()
    contents = page.get_by_test_id("error-message-content")
    return contents.all_inner_texts()


@pytest.mark.timeout(360)
def test_codex_native_turn_delivers_after_bridge_dir_teardown(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A composer turn still delivers when the bridge dir was torn down.

    :param page: Playwright page fixture.
    :param native_codex_mock_session: ``(base_url, session_id)`` for a live
        codex-native session on the shared runner.
    :param mock_llm_server_url: In-process mock LLM server base URL.
    :returns: None.
    """
    base_url, session_id = native_codex_mock_session

    page.goto(f"{base_url}/c/{session_id}")

    # Boot the real Codex TUI (writes bridge state.json) and confirm it is live.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # The runner writes bridge state under this dir once the TUI's thread starts.
    bridge_dir = _codex_bridge_dir(session_id)
    _wait_for_state_file(bridge_dir / "state.json", _STATE_WRITE_TIMEOUT_S)

    # Route the delivered turn to a unique assistant token; extra internal
    # Codex calls (including the relaunched TUI's boot) get an empty fallback.
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

    # The teardown consequence: the whole bridge dir (state.json,
    # startup_error.json, MCP config, app-server socket) is removed while the
    # tmux pane stays alive, so nothing on disk will ever satisfy the
    # executor's wait for bridge state. The live TUI may be writing into the
    # dir concurrently (exactly the race the production teardown tolerates),
    # so retry until the removal wins.
    _tear_down_bridge_dir(bridge_dir)
    assert not bridge_dir.exists()

    # Deliver a web turn through the composer. Turn delivery must detect the
    # torn-down bridge, relaunch Codex, and inject into the fresh thread.
    _send(page, _turn_prompt(1, user_marker, assistant_token))

    assistant = page.locator(_ASSISTANT, has_text=assistant_token).first
    try:
        expect(assistant).to_be_visible(timeout=_TURN_DELIVERY_TIMEOUT_MS)
    except AssertionError:
        raise AssertionError(
            "assistant reply never rendered after the bridge-dir teardown; "
            f"error pills seen: {_expanded_error_pill_texts(page)!r}"
        ) from None

    # The turn settled and never surfaced the generic missing-state failure.
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    pill_texts = _expanded_error_pill_texts(page)
    assert not any(_MISSING_STATE_MESSAGE in text for text in pill_texts), (
        f"turn delivered but a failed-turn pill still surfaced "
        f"{_MISSING_STATE_MESSAGE!r}: {pill_texts!r}"
    )
