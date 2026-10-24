"""E2E: a follow-up pushed forward mid-turn must not read as the active request.

While the first turn is still running, the queued strip's "Send now" POSTs the
follow-up. An openai-agents turn cannot read it until that turn ends, so the
first turn's remaining work, its Working… indicator and its reply must stay
above the follow-up, and the follow-up's own reply must come after it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import tool_fold_session_with_script

_COMPOSER_LABEL = "Message the agent"
_FIRST_PROMPT = "Inspect the workspace and tell me what is in it."
_FOLLOW_UP = "sentinel-mid-turn-followup push this prompt forward"
_FIRST_REPLY = "Workspace inspected."
_FOLLOW_UP_REPLY = "Follow-up answered."
_QUEUED_STRIP = '[data-testid="composer-queued-strip"]'
_WORKING = '[data-testid="working-indicator"]'
_WORKING_ROW = "working"


@pytest.fixture
def two_pause_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, str]]:
    """An openai-agents session whose first turn pauses before and after its read call.

    :returns: ``(base_url, session_id, mock_llm_url)``.
    """
    shell_arguments = json.dumps({"command": "ls"})
    read_arguments = json.dumps({"path": "README.md"})
    shell_call = {"call_id": "call_ls", "name": "sys_os_shell", "arguments": shell_arguments}
    read_call = {"call_id": "call_read", "name": "sys_os_read", "arguments": read_arguments}
    with tool_fold_session_with_script(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        model_prefix="two-pause-turn",
        script=[
            {"tool_calls": [shell_call]},
            {"block": True, "tool_calls": [read_call]},
            {"block": True, "text": _FIRST_REPLY},
            {"text": _FOLLOW_UP_REPLY},
        ],
        fallback_text="Unexpected extra model call.",
    ) as session:
        yield session


def _gate_pending(mock_url: str) -> bool:
    response = httpx.get(f"{mock_url}/gate/pending", timeout=5.0)
    response.raise_for_status()
    return bool(response.json()["pending"])


def _release_gate(mock_url: str) -> None:
    response = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    response.raise_for_status()
    assert response.json()["released"], "no model call was waiting on the gate"


def _send(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _transcript_rows(page: Page) -> list[str]:
    """Visible transcript rows in order: ``working`` or ``<role>: <text>``."""
    return page.evaluate(
        """(workingSel) => [
          ...document.querySelectorAll(`[data-testid="message-bubble"], ${workingSel}`),
        ]
          .filter((el) => el.checkVisibility() && el.getClientRects().length > 0)
          .map((el) =>
            el.matches(workingSel)
              ? "working"
              : `${el.getAttribute("data-role")}: ${el.innerText.replace(/\\s+/g, " ")}`,
          )""",
        _WORKING,
    )


def _split_at_follow_up(rows: list[str]) -> tuple[list[str], list[str]] | None:
    for index, row in enumerate(rows):
        if row.startswith("user:") and _FOLLOW_UP in row:
            return rows[:index], rows[index + 1 :]
    return None


def _first_turn_still_runs_above(rows: list[str]) -> bool:
    split = _split_at_follow_up(rows)
    return split is not None and split[0].count(_WORKING_ROW) == 1 and split[1] == []


def _replies_on_either_side(rows: list[str]) -> bool:
    split = _split_at_follow_up(rows)
    if split is None or _WORKING_ROW in rows:
        return False
    above, below = split
    return (
        any(row.startswith("assistant:") and _FIRST_REPLY in row for row in above)
        and len(below) == 1
        and below[0].startswith("assistant:")
        and _FOLLOW_UP_REPLY in below[0]
        and _FIRST_REPLY not in below[0]
    )


def _await_rows(page: Page, layout: Callable[[list[str]], bool], expectation: str) -> None:
    rows: list[str] = []

    def matches() -> bool:
        nonlocal rows
        rows = _transcript_rows(page)
        return layout(rows)

    try:
        poll_until(matches, timeout_s=20.0, interval_s=0.2)
    except AssertionError as exc:
        raise AssertionError(f"{expectation}; transcript rows were {rows}") from exc


def test_follow_up_sent_mid_turn_waits_behind_the_running_turn(
    request: pytest.FixtureRequest,
    two_pause_session: tuple[str, str, str],
    output_path: str,
) -> None:
    base_url, session_id, mock_url = two_pause_session
    print(f"product_session_id={session_id}")
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)

    _send(page, _FIRST_PROMPT)
    poll_until(lambda: _gate_pending(mock_url))
    _send(page, _FOLLOW_UP)
    strip = page.locator(_QUEUED_STRIP)
    expect(strip).to_contain_text(_FOLLOW_UP)
    page.get_by_role("button", name="Send queued message now").click()
    expect(strip).to_have_count(0)

    _await_rows(
        page,
        _first_turn_still_runs_above,
        "while the first request waits on its model, its Working… indicator must sit "
        "above the sent follow-up with nothing beneath the follow-up",
    )
    assert _gate_pending(mock_url), "the first request must still be executing here"
    page.screenshot(path=os.path.join(output_path, "first-pause.png"))

    _release_gate(mock_url)
    expect(page.get_by_text("README.md").first).to_be_visible(timeout=30_000)
    poll_until(lambda: _gate_pending(mock_url))
    _await_rows(
        page,
        _first_turn_still_runs_above,
        "after the first request's read call, that call and Working… must still sit "
        "above the follow-up, which the turn has not read",
    )
    page.screenshot(path=os.path.join(output_path, "second-pause.png"))

    _release_gate(mock_url)
    _await_rows(
        page,
        _replies_on_either_side,
        f"{_FIRST_REPLY!r} must sit above the follow-up and only {_FOLLOW_UP_REPLY!r} below it",
    )
    page.screenshot(path=os.path.join(output_path, "both-replies.png"))

    page.reload()
    expect(page.get_by_text(_FOLLOW_UP_REPLY)).to_be_visible(timeout=30_000)
    _await_rows(
        page,
        _replies_on_either_side,
        f"after a reload, {_FIRST_REPLY!r} must still sit above the follow-up and only "
        f"{_FOLLOW_UP_REPLY!r} below it",
    )
