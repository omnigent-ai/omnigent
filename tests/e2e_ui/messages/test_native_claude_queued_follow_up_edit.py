"""Editing a queued follow-up keeps the GUI chat and the Claude Code TUI in sync."""

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import _USER, _WORKING, _ensure_chat_view
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _pane_text,
    _queued_input_texts,
    _tmux_advert,
)

_QUEUED_HINT = "Press up to edit queued messages"


@pytest.fixture(scope="session", autouse=True)
def _requires_mockable_claude() -> None:
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")


@pytest.fixture
def held_first_turn(mock_llm_server_url: str) -> Iterator[dict[str, str]]:
    nonce = uuid.uuid4().hex[:8]
    tokens = {
        "hold": f"hold-{nonce}",
        "original": f"original-follow-up-{nonce}",
        "edited": f"edited-follow-up-{nonce}",
    }
    reset_mock_llm(mock_llm_server_url)
    # Claude sends a title request alongside the turn; both must stay blocked.
    held = [{"block": True, "text": "first reply"}] * 2
    configure_mock_llm(mock_llm_server_url, held, match=tokens["hold"])
    for name in ("original", "edited"):
        replies = [{"text": f"reply {tokens[name]}"}] * 3
        configure_mock_llm(mock_llm_server_url, replies, match=tokens[name])
    for model in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, model, f"fallback reply {nonce}")
    try:
        yield tokens
    finally:
        release_url = f"{mock_llm_server_url}/gate/release"
        while httpx.post(release_url, timeout=5.0).json().get("released"):
            pass


def _squashed(text: str) -> str:
    return "".join(text.split())


def _queue_gui_follow_up(
    page: Page, base_url: str, session_id: str, mock_llm_server_url: str, tokens: dict[str, str]
) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    composer.fill(f"Start a long task {tokens['hold']}")
    composer.press("Enter")
    poll_until(
        lambda: httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0).json()["pending"],
        timeout_s=90,
    )
    composer.fill(f"Then summarize {tokens['original']}")
    composer.press("Enter")
    expect(page.locator(_USER, has_text=tokens["original"])).to_have_count(1, timeout=30_000)
    poll_until(
        lambda: (
            _QUEUED_HINT in (pane := _pane_text(base_url, session_id))
            and tokens["original"] in _squashed(pane)
        ),
        timeout_s=60,
    )


def _expect_claude_answers_only_the_edit(
    page: Page, base_url: str, session_id: str, mock_llm_server_url: str, tokens: dict[str, str]
) -> None:
    reply = page.get_by_text(f"reply {tokens['edited']}")

    def answered() -> bool:
        # Releasing the held first turn lets Claude take the queued follow-up.
        httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0)
        return reply.count() > 0

    poll_until(answered, timeout_s=90)
    # Let the queue drain so a later answer to the original would still be caught.
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    poll_until(
        lambda: (
            not _queued_input_texts(base_url, session_id)
            and _QUEUED_HINT not in _pane_text(base_url, session_id)
        ),
        timeout_s=30,
    )
    assert tokens["original"] not in json.dumps(get_mock_requests(mock_llm_server_url))
    expect(page.get_by_text(f"reply {tokens['original']}")).to_have_count(0)
    expect(page.locator(_USER, has_text=tokens["edited"])).to_have_count(1)
    expect(page.locator(_USER, has_text=tokens["original"])).to_have_count(0)


def _tmux_keys(base_url: str, session_id: str, *keys: str, literal: bool = False) -> None:
    advert = _tmux_advert(base_url, session_id)
    assert advert is not None
    command = ["tmux", "-S", advert["socket_path"], "send-keys", "-t", advert["tmux_target"]]
    subprocess.run([*command, *(["-l"] if literal else []), *keys], check=True, timeout=10)


@pytest.mark.nightly
@pytest.mark.timeout(240)
def test_tui_edit_of_queued_follow_up_updates_the_gui_bubble(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    held_first_turn: dict[str, str],
) -> None:
    base_url, session_id = native_claude_mock_session
    tokens = held_first_turn
    _queue_gui_follow_up(page, base_url, session_id, mock_llm_server_url, tokens)

    _tmux_keys(base_url, session_id, "Up")
    poll_until(lambda: _QUEUED_HINT not in _pane_text(base_url, session_id), timeout_s=15)
    _tmux_keys(base_url, session_id, *["BSpace"] * len(tokens["original"]))
    _tmux_keys(base_url, session_id, tokens["edited"], literal=True)
    _tmux_keys(base_url, session_id, "Enter")
    poll_until(
        lambda: (
            _QUEUED_HINT in (pane := _pane_text(base_url, session_id))
            and tokens["edited"] in _squashed(pane)
        ),
        timeout_s=30,
    )

    expect(page.locator(_USER, has_text=tokens["edited"])).to_have_count(1, timeout=20_000)
    expect(page.locator(_USER, has_text=tokens["original"])).to_have_count(0)
    _expect_claude_answers_only_the_edit(page, base_url, session_id, mock_llm_server_url, tokens)


@pytest.mark.nightly
@pytest.mark.timeout(240)
def test_gui_arrow_up_edit_of_queued_follow_up_replaces_it_in_the_tui(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    held_first_turn: dict[str, str],
) -> None:
    base_url, session_id = native_claude_mock_session
    tokens = held_first_turn
    _queue_gui_follow_up(page, base_url, session_id, mock_llm_server_url, tokens)

    composer = page.get_by_label("Message the agent")
    composer.click()
    composer.press("ArrowUp")
    expect(composer).to_have_value(f"Then summarize {tokens['original']}", timeout=5_000)
    composer.fill(f"Then summarize {tokens['edited']}")
    composer.press("Enter")
    poll_until(
        lambda: tokens["edited"] in _squashed(_pane_text(base_url, session_id)), timeout_s=30
    )

    expect(page.locator(_USER, has_text="Then summarize")).to_have_count(1, timeout=20_000)
    expect(page.locator(_USER, has_text=tokens["edited"])).to_have_count(1)
    assert tokens["original"] not in _squashed(_pane_text(base_url, session_id))
    _expect_claude_answers_only_the_edit(page, base_url, session_id, mock_llm_server_url, tokens)
