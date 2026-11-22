r"""A side chat opened while the parent's Claude Code turn runs answers only its question.

Reported journey: ask the main agent something, open a side chat right away, and
ask a different question there. The side chat must not pick up the main agent's
unanswered request. Requires an isolated real server + host with Claude Code and
working inference. Session creation, the fork, both native transcripts, the
parent's command and every model turn run normally, without intercepted requests
or injected application state::

    OMNIGENT_E2E_ALLOW_DEV_BASE_URL=1 uv run --no-sync pytest \
      tests/e2e_ui/chat/test_side_chat_mid_turn_live.py \
      --ui-base-url http://127.0.0.1:<port> --video=on --tracing=on \
      --output=/tmp/side-chat-mid-turn
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.chat.test_side_chat_entrypoints import _items, _start_side_chat
from tests.e2e_ui.start_session.test_cancel_initial_message_live import (
    _ASSISTANT,
    _choose_workspace,
)
from tests.e2e_ui.start_session.test_cancel_initial_message_startup_live import _REAL_ROUTE

pytestmark = [pytest.mark.workspace_panel_product_default, pytest.mark.timeout(600)]

_SIDE_PANE = ".side-chat-backdrop"
# Records each start, then blocks until the test writes the word it prints.
_GATED_COMMAND = """#!/bin/sh
cd "$(dirname "$0")"
echo started >> .gate/starts
while [ ! -s .gate/word ]; do sleep 0.2; done
cat .gate/word
"""


def _command_starts(workspace: Path) -> int:
    starts = workspace / ".gate" / "starts"
    return len(starts.read_text().splitlines()) if starts.exists() else 0


def _user_texts(items: list[dict[str, object]]) -> list[str]:
    return [
        json.dumps(item.get("content"))
        for item in items
        if item.get("type") == "message" and item.get("role") == "user"
    ]


def test_side_chat_opened_mid_turn_answers_only_its_question(
    page: Page, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A side chat forked during a running native turn ignores that turn's request."""
    base_url = request.config.getoption("--ui-base-url")
    if not base_url:
        pytest.skip("Requires --ui-base-url for a real server, host, and Claude Code")
    base_url = base_url.rstrip("/")
    nonce = uuid.uuid4().hex[:12]
    codename = f"CODENAME_{nonce}"
    ack = f"NOTED_{nonce}"
    word = f"WORD_{uuid.uuid4().hex[:12]}"
    command = f"reveal_{nonce}.sh"
    workspace = tmp_path / "workspace"
    (workspace / ".gate").mkdir(parents=True)
    (workspace / command).write_text(_GATED_COMMAND)
    (workspace / command).chmod(0o755)
    release = workspace / ".gate" / "word"

    try:
        page.goto(f"{base_url}/")
        picker = _choose_workspace(page, workspace)
        picker.click()
        page.get_by_role("menuitem", name="Claude Code", exact=True).click()
        expect(picker).to_have_attribute("aria-expanded", "false")
        page.get_by_test_id("new-chat-landing-input").fill(
            f"Remember the codename {codename} for later. "
            f"Reply with exactly {ack}. Do not use tools."
        )
        page.get_by_test_id("new-chat-landing-submit").click()
        expect(page).to_have_url(_REAL_ROUTE, timeout=60_000)
        session_id = urlparse(page.url).path.rsplit("/", 1)[1]
        expect(page.locator(_ASSISTANT, has_text=ack)).to_be_visible(timeout=180_000)
        expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)

        # Ask the main agent a question, then open a side chat once Claude Code has it.
        page.get_by_role("textbox", name="Message the agent").fill(
            f"What word does ./{command} in this folder print? Run it with the Bash tool to "
            "find out. It can take several minutes, so give the command a 600000 ms "
            "timeout. Reply with exactly that word."
        )
        page.get_by_role("button", name="Send", exact=True).click()
        poll_until(
            lambda: any(command in text for text in _user_texts(_items(base_url, session_id))),
            timeout_s=60,
        )
        fork_path = f"/v1/sessions/{session_id}/fork"
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and urlparse(response.url).path == fork_path
            )
        ) as fork:
            _start_side_chat(page, "panel", "What codename did I ask you to remember?")
        assert fork.value.ok, fork.value.text()
        child_id = fork.value.json()["id"]
        parent = page.request.get(f"{base_url}/v1/sessions/{session_id}")
        assert parent.json()["status"] in {"running", "waiting"}, "The parent was not mid-turn"
        pane = page.locator(_SIDE_PANE)

        # Each symptom is checked on its own so one failure does not hide another.
        failures: list[str] = []
        try:
            expect(pane.locator(_ASSISTANT, has_text=codename)).to_be_visible(timeout=120_000)
            expect(pane.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)
        except AssertionError:
            failures.append("The side chat did not finish answering its own question")
        deadline = time.monotonic() + 120
        while _command_starts(workspace) == 0:
            assert time.monotonic() < deadline, "The parent never started its command"
            page.wait_for_timeout(250)
        if (starts := _command_starts(workspace)) != 1:
            failures.append(f"The parent's command started {starts} times, not once")
        side_text = pane.inner_text()
        if command in side_text:
            failures.append(f"The side chat took up the parent's request:\n{side_text}")
        child_history = _user_texts(_items(base_url, child_id))
        if any(command in text for text in child_history):
            failures.append(
                f"The side chat inherited the parent's unanswered request: {child_history}"
            )
        if not any(codename in text for text in child_history):
            failures.append(f"The side chat lost the parent's earlier turns: {child_history}")

        release.write_text(f"{word}\n")
        expect(page.locator(_ASSISTANT, has_text=word)).to_have_count(1, timeout=180_000)
        expect(pane.get_by_text(re.compile(word))).to_have_count(0)
        if (starts := _command_starts(workspace)) != 1:
            failures.append(f"The parent's command started {starts} times by the end, not once")
        assert not failures, "\n".join(failures)
    finally:
        if not release.exists():
            release.write_text(f"{word}\n")
