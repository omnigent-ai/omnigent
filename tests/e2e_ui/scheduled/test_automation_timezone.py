"""UI journey: a chat-created automation's schedule uses the user's local timezone.

The browser runs in ``America/Los_Angeles`` so the SPA's ``Intl`` zone differs
from the server process zone (set ``TZ`` on the pytest process to make the
spawned server run in another zone, e.g. ``TZ=America/Chicago``).

``test_chat_created_automation_uses_user_local_timezone`` asks the agent in chat
to create a daily 9:00 AM automation without naming a zone; the mock model
answers with a ``sys_scheduled_task_create`` call that omits ``timezone`` (the
tool gives the agent no user-zone information), so the next run must still land
at 9:00 AM in the browser's zone.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state, configure_mock_llm
from tests.e2e_ui.scheduled.test_scheduled_tasks_page import _builtin_agent_id, _row_by_name

_BROWSER_TZ = "America/Los_Angeles"
_LA = ZoneInfo(_BROWSER_TZ)
# Optional evidence screenshots; unset means none are written.
_SHOTS_DIR = (
    Path(os.environ["OMNIGENT_E2E_SHOTS_DIR"])
    if os.environ.get("OMNIGENT_E2E_SHOTS_DIR")
    else None
)


def _server_process_tz() -> list[str]:
    pid = _server_state.get("pid")
    if pid is None:
        return ["<attached server: TZ unknown>"]
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return ["<TZ unknown: /proc unavailable on this platform>"]
    return [e.decode() for e in environ if e.startswith(b"TZ=")] or ["TZ unset (process default)"]


def _task_by_name(base_url: str, name: str) -> dict[str, Any] | None:
    resp = httpx.get(f"{base_url}/v1/scheduled-tasks", timeout=10.0)
    resp.raise_for_status()
    return next((t for t in resp.json()["scheduled_tasks"] if t["name"] == name), None)


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _clock_12h(hour24: int, minute: int) -> tuple[int, str]:
    period = "PM" if hour24 >= 12 else "AM"
    hour12 = hour24 % 12 or 12
    return hour12, period


def _fmt_12h(dt: datetime) -> str:
    hour12, period = _clock_12h(dt.hour, dt.minute)
    return f"{hour12}:{dt.minute:02d} {period}"


def _shot(page: Page, name: str, settle_ms: int = 0) -> None:
    if _SHOTS_DIR is None:
        return
    if settle_ms:
        page.wait_for_timeout(settle_ms)
    _SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(_SHOTS_DIR / f"{name}.png"))


def _send(page: Page, text: str) -> None:
    page.get_by_label("Message the agent").fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.browser_context_args(timezone_id=_BROWSER_TZ)
def test_chat_created_automation_uses_user_local_timezone(
    request: pytest.FixtureRequest,
    live_server: str,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """An automation the agent creates from chat for "9:00 AM" uses the user's local zone."""
    base_url, session_id = seeded_session
    agent_id = _builtin_agent_id(live_server, "hello_world")
    name = f"Daily inbox summary {uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:8]
    user_text = (
        f"Create an automation that runs every day at 9:00 AM and summarizes my inbox. [{nonce}]"
    )
    tool_arguments = {
        "name": name,
        "prompt": "Summarize my inbox.",
        "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        "agent_id": agent_id,
    }
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{nonce}",
                        "name": "sys_scheduled_task_create",
                        "arguments": json.dumps(tool_arguments),
                    }
                ]
            },
            {"text": f"Done. I created the automation “{name}” to run every day at 9:00 AM."},
        ],
        match=nonce,
        required_tools=["sys_scheduled_task_create"],
    )
    print(f"\nserver TZ env={_server_process_tz()} session={session_id}")

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    browser_tz = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    assert browser_tz == _BROWSER_TZ, browser_tz
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    _send(page, user_text)
    expect(page.get_by_text("I created the automation")).to_be_visible(timeout=120_000)
    _shot(page, "chat-reply", settle_ms=2_000)

    task = _task_by_name(live_server, name)
    assert task is not None, "the agent's sys_scheduled_task_create did not create a task"
    task_id = task["id"]
    try:
        page.goto(f"{live_server}/tasks")
        row = _row_by_name(page, name)
        expect(row).to_be_visible(timeout=30_000)
        expect(row.get_by_test_id("task-schedule-line")).to_contain_text("Every day at 9:00 AM")
        expect(row.get_by_test_id("task-next-run")).to_contain_text("Next run", timeout=30_000)
        _shot(page, "chat-created-row", settle_ms=3_000)
        line_text = row.get_by_test_id("task-schedule-line").inner_text()

        captured = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0).json()
        if _SHOTS_DIR is not None:
            _SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            (_SHOTS_DIR / "mock-requests.json").write_text(json.dumps(captured, indent=2))
        print(f"mock captured {len(captured.get('requests', []))} request(s)")
        # Scope to this turn (the session-scoped mock history isn't cleared), then
        # check instructions specifically: the model omitted the tool's timezone,
        # so a zone here proves the prompt path carried it, not the result echo.
        turn_requests = [req for req in captured.get("requests", []) if nonce in json.dumps(req)]
        assert turn_requests, "mock captured no request carrying this turn's nonce"
        assert any(
            _BROWSER_TZ in json.dumps(req.get("instructions", "")) for req in turn_requests
        ), f"framework instructions did not carry {_BROWSER_TZ} to the model"
        actual_instant = _parse_iso(task["next_run_at"])
        actual_local = actual_instant.astimezone(_LA)
        print(
            f"stored timezone={task['timezone']} next_run_at={task['next_run_at']} "
            f"(= {_fmt_12h(actual_local)} {_BROWSER_TZ}) row={line_text!r}"
        )
        # The next 9:00 AM in the browser's zone, regardless of which calendar day
        # it falls on; a UTC-evaluated schedule would read 1:00/2:00 AM here.
        assert task["timezone"] == _BROWSER_TZ and (actual_local.hour, actual_local.minute) == (
            9,
            0,
        ), (
            f"chat-created automation is evaluated in {task['timezone']!r} "
            f"(next_run_at {actual_instant.isoformat()}, i.e. "
            f"{_fmt_12h(actual_local)} {_BROWSER_TZ}) while the row reads "
            f"{line_text!r}; expected 9:00 AM {_BROWSER_TZ}"
        )
    finally:
        httpx.delete(f"{live_server}/v1/scheduled-tasks/{task_id}", timeout=10.0)
