"""E2E: an imported Claude Code session shows its source times in the app.

``omnigent import`` reads a local Claude Code JSONL transcript whose records
carry ISO ``timestamp`` fields and posts them to ``/v1/imports``. The sidebar
tooltip renders the session's ``updated_at`` as a relative age and each bubble
reveals its ``created_at`` on hover, so a session worked on months ago must
read as months old in both places rather than as if it happened at import time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

_RECORD_TIMES = (
    "2026-07-21T12:00:00.000Z",
    "2026-07-21T12:05:00.000Z",
    "2026-07-21T13:30:00.000Z",
    "2026-07-21T13:31:00.000Z",
)
_TEXTS = (
    "inspect TODO.md",
    "TODO.md lists three open items.",
    "close the first item",
    "Done.",
)
# en-US renders the day once the stamp is not today; the year appears only
# after the calendar year rolls over.
_FIRST_MESSAGE_TIME = re.compile(r"^Jul 21(, 2026)?, 12:00 PM$")
_LAST_MESSAGE_TIME = re.compile(r"^Jul 21(, 2026)?, 1:31 PM$")
_MONTHS_OR_YEARS_OLD = re.compile(r"· \d+(mo|y)$")
_BUBBLE = '[data-testid="message-bubble"]'
_TIMESTAMP = '[data-testid="message-timestamp"]'


def _write_claude_transcript(home: Path) -> str:
    """Write a four-record Claude Code transcript dated Jul 21 2026; return its session id."""
    session_id = str(uuid.uuid4())
    transcript = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    parent: str | None = None
    for index, (stamp, text) in enumerate(zip(_RECORD_TIMES, _TEXTS, strict=True)):
        role = "user" if index % 2 == 0 else "assistant"
        record_id = f"{role}-{index // 2 + 1}"
        records.append(
            {
                "type": role,
                "uuid": record_id,
                "parentUuid": parent,
                "sessionId": session_id,
                "cwd": "/repo",
                "timestamp": stamp,
                "message": {
                    "role": role,
                    "content": text if role == "user" else [{"type": "text", "text": text}],
                },
            }
        )
        parent = record_id
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    last = datetime.fromisoformat(_RECORD_TIMES[-1].replace("Z", "+00:00")).timestamp()
    os.utime(transcript, (last, last))
    return session_id


def _import_claude_session(base_url: str, home: Path) -> str:
    """Import the transcript under ``home`` with the real CLI; return the new session id."""
    source_session_id = _write_claude_transcript(home)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "OMNIGENT_CONFIG_HOME": str(home / "config"),
        "OMNIGENT_DATA_DIR": str(home / "omnigent-data"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "import",
            "--harness",
            "claude",
            "--session",
            source_session_id,
            "--server",
            base_url,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    match = re.search(r"Imported \d+ item\(s\) into \S+/c/(\S+)", result.stdout)
    assert match is not None, result.stdout
    return match.group(1)


def _sidebar_row(page: Page, session_id: str) -> Locator:
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _revealed_timestamp(bubble: Locator) -> str:
    """Hover ``bubble`` until its action row is opaque and return the timestamp text."""
    bubble.scroll_into_view_if_needed()
    bubble.hover()
    timestamp = bubble.locator(_TIMESTAMP)
    expect(timestamp.locator("..")).to_have_css("opacity", "1")
    return timestamp.inner_text()


def _hold_for_recording(page: Page, request: pytest.FixtureRequest) -> None:
    """Pause so a video capture can read the asserted state; a no-op otherwise.

    The preceding ``expect`` assertions already verify the state, so a normal
    test run skips the wait and only a recording run keeps it on screen.
    """
    recording = bool(os.environ.get("OMNIGENT_E2E_RECORD_DIR")) or (
        request.config.getoption("--video", "off") != "off"
    )
    if recording:
        page.wait_for_timeout(1_500)


@pytest.mark.browser_context_args(timezone_id="UTC", locale="en-US")
def test_imported_session_sidebar_recency_is_the_source_time(
    request: pytest.FixtureRequest, live_server: str, tmp_path: Path
) -> None:
    """The sidebar tooltip dates an imported session by its last source message."""
    session_id = _import_claude_session(live_server, tmp_path)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{live_server}/c/{session_id}")
    row = _sidebar_row(page, session_id)
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    tooltip = page.get_by_test_id("session-tooltip-content")
    expect(tooltip).to_be_visible()
    title_line = tooltip.locator("p").first
    expect(title_line).to_contain_text("inspect TODO.md")
    expect(title_line).to_have_text(_MONTHS_OR_YEARS_OLD)
    _hold_for_recording(page, request)


@pytest.mark.browser_context_args(timezone_id="UTC", locale="en-US")
def test_imported_messages_show_their_source_timestamps(
    request: pytest.FixtureRequest, live_server: str, tmp_path: Path
) -> None:
    """Hovering imported bubbles reveals each record's own date and time."""
    session_id = _import_claude_session(live_server, tmp_path)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{live_server}/c/{session_id}")
    bubbles = page.locator(_BUBBLE)
    expect(bubbles).to_have_count(len(_TEXTS), timeout=30_000)
    first_time = _revealed_timestamp(bubbles.first)
    _hold_for_recording(page, request)
    last_time = _revealed_timestamp(bubbles.last)
    _hold_for_recording(page, request)

    assert _FIRST_MESSAGE_TIME.match(first_time), first_time
    assert _LAST_MESSAGE_TIME.match(last_time), last_time
