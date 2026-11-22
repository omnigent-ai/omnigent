r"""A side chat opened after a follow-up is steered into a turn answers only its question.

Reported journey, continued: ask the main agent something, send it a follow-up
while it works, then open a side chat and ask a different question there. The
side chat must not pick up the main agent's running request. Requires an
isolated real server + host with Claude Code and working inference. Session
creation, the fork, both native transcripts, the parent's commands and every
model turn run normally, without intercepted requests or injected state::

    OMNIGENT_E2E_ALLOW_DEV_BASE_URL=1 uv run --no-sync pytest \
      tests/e2e_ui/chat/test_side_chat_steered_turn_live.py \
      --ui-base-url http://127.0.0.1:<port> --video=on --tracing=on \
      --output=/tmp/side-chat-steered-turn
"""

from __future__ import annotations

import contextlib
import re
import uuid
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.chat.test_side_chat_entrypoints import _items, _start_side_chat
from tests.e2e_ui.chat.test_side_chat_mid_turn_live import _SIDE_PANE, _user_texts
from tests.e2e_ui.start_session.test_cancel_initial_message_live import (
    _ASSISTANT,
    _choose_workspace,
)
from tests.e2e_ui.start_session.test_cancel_initial_message_startup_live import _REAL_ROUTE

pytestmark = [pytest.mark.workspace_panel_product_default, pytest.mark.timeout(600)]


def _gated_command(release: str) -> str:
    """A script that records its start, then prints *release* once the test writes it."""
    return (
        "#!/bin/sh\n"
        'cd "$(dirname "$0")"\n'
        'echo "$(basename "$0")" >> .gate/starts\n'
        f"while [ ! -s .gate/{release} ]; do sleep 0.2; done\n"
        f"cat .gate/{release}\n"
    )


def _starts(workspace: Path, command: str) -> int:
    starts = workspace / ".gate" / "starts"
    return starts.read_text().splitlines().count(command) if starts.exists() else 0


def _started(workspace: Path, command: str) -> bool:
    return _starts(workspace, command) > 0


def test_side_chat_opened_after_a_steered_follow_up_answers_only_its_question(
    page: Page, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A side chat forked after a steered follow-up ignores the turn's own request."""
    base_url = request.config.getoption("--ui-base-url")
    if not base_url:
        pytest.skip("Requires --ui-base-url for a real server, host, and Claude Code")
    base_url = base_url.rstrip("/")
    nonce = uuid.uuid4().hex[:12]
    codename = f"CODENAME_{nonce}"
    ack = f"NOTED_{nonce}"
    done = f"DONE_{nonce}"
    # Commands the parent runs one after another, each naming the next.
    commands = [f"step{n}_{nonce}.sh" for n in range(1, 5)]
    words = [f"WORD_{uuid.uuid4().hex[:12]}" for _ in commands]
    workspace = tmp_path / "workspace"
    gates = workspace / ".gate"
    gates.mkdir(parents=True)
    for n, command in enumerate(commands):
        (workspace / command).write_text(_gated_command(f"release{n}"))
        (workspace / command).chmod(0o755)

    def release(n: int) -> None:
        gate = gates / f"release{n}"
        if not gate.exists():
            following = f"Next, run ./{commands[n + 1]} the same way.\n" if n + 1 < 4 else ""
            gate.write_text(f"{words[n]}\n{following}")

    def steered_in() -> bool:
        return any(done in text for text in _user_texts(_items(base_url, session_id)))

    def starts() -> list[int]:
        return [_starts(workspace, command) for command in commands]

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

        # Ask the main agent a question, then steer a follow-up into the turn.
        composer = page.get_by_role("textbox", name="Message the agent")
        composer.fill(
            f"Run ./{commands[0]} in this folder with the Bash tool. It can take several "
            "minutes, so give it a 600000 ms timeout. It prints a word and may name another "
            "script; run each script it names the same way, one at a time. Reply with exactly "
            "the words they print, separated by spaces."
        )
        page.get_by_role("button", name="Send", exact=True).click()
        poll_until(partial(_started, workspace, commands[0]), timeout_s=120)
        composer.fill(f"Also end your reply with {done}.")
        events_path = f"/v1/sessions/{session_id}/events"
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and urlparse(response.url).path == events_path
            )
        ) as steered:
            page.get_by_role("button", name="Send", exact=True).click()
        assert steered.value.ok, steered.value.text()
        # Claude Code hands a queued follow-up to its turn when a command returns. Release
        # one command at a time until it has, so the next command keeps the turn running.
        running = 0
        while not steered_in():
            assert running < 3, "Claude Code never took the follow-up while its turn ran"
            release(running)
            running += 1
            poll_until(partial(_started, workspace, commands[running]), timeout_s=180)
            with contextlib.suppress(AssertionError):
                poll_until(steered_in, timeout_s=15)

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
        side_text = pane.inner_text()
        if any(marker in side_text for marker in [*commands, *words]):
            failures.append(f"The side chat took up the parent's request:\n{side_text}")
        child_history = _user_texts(_items(base_url, child_id))
        if any(commands[0] in text for text in child_history):
            failures.append(
                f"The side chat inherited the parent's running request: {child_history}"
            )
        if any(done in text for text in child_history):
            failures.append(f"The side chat inherited the parent's follow-up: {child_history}")
        if not any(codename in text for text in child_history):
            failures.append(f"The side chat lost the parent's earlier turns: {child_history}")
        expected_starts = [1] * (running + 1) + [0] * (3 - running)
        if starts() != expected_starts:
            failures.append(f"The parent's commands started {starts()} times")

        for n in range(running, 4):
            release(n)
        expect(page.locator(_ASSISTANT, has_text=words[-1])).to_have_count(1, timeout=180_000)
        expect(pane.get_by_text(re.compile(words[-1]))).to_have_count(0)
        if starts() != [1, 1, 1, 1]:
            failures.append(f"The parent's commands started {starts()} times by the end")
        assert not failures, "\n".join(failures)
    finally:
        for n in range(4):
            release(n)
