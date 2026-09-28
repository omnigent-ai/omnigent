"""E2E: ``omnigent import`` keeps the source transcript's timestamps.

Drives the real CLI (``python -m omnigent import --harness claude``) against
the live server with a Claude Code transcript whose records are dated months
in the past, then reads the result where a user sees it:

* the sidebar row's hover tooltip labels the session by ``updated_at``
  (``2mo`` for a July session; ``now`` when stamped at import time);
* the hover-revealed ``message-timestamp`` on each imported bubble renders
  the item's ``created_at`` (``Jul 21, 12:00 PM`` vs. today's clock time).

Selectors:
  - sidebar row: ``li[data-sidebar-session-id=<id>]``; its tooltip is
    ``data-testid="session-tooltip-content"``
  - bubbles: ``data-testid="message-bubble"`` + ``data-role="user|assistant"``;
    timestamp ``data-testid="message-timestamp"``
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_TIMESTAMP = '[data-testid="message-timestamp"]'

_FIRST_AT = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
_LAST_AT = datetime(2026, 7, 21, 13, 31, tzinfo=UTC)
_TRANSCRIPT_RECORDS: tuple[tuple[str, str, str], ...] = (
    ("user", "2026-07-21T12:00:00.000Z", "inspect TODO.md"),
    ("assistant", "2026-07-21T12:05:00.000Z", "Looking now."),
    ("user", "2026-07-21T13:30:00.000Z", "thanks, summarize"),
    ("assistant", "2026-07-21T13:31:00.000Z", "Done."),
)


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Pin locale and zone so the rendered date strings are deterministic."""
    return {**browser_context_args, "locale": "en-US", "timezone_id": "UTC"}


def _write_claude_transcript(home: Path, session_id: str) -> Path:
    transcript = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    records = []
    parent: str | None = None
    for index, (role, stamp, text) in enumerate(_TRANSCRIPT_RECORDS, start=1):
        uuid = f"{role}-{index}"
        content: object = text if role == "user" else [{"type": "text", "text": text}]
        records.append(
            {
                "type": role,
                "uuid": uuid,
                "parentUuid": parent,
                "sessionId": session_id,
                "cwd": "/repo",
                "timestamp": stamp,
                "message": {"role": role, "content": content},
            }
        )
        parent = uuid
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    last_modified = _LAST_AT.timestamp()
    os.utime(transcript, (last_modified, last_modified))
    return transcript


def _import_with_cli(base_url: str, home: Path, session_id: str) -> str:
    """Run the real ``omnigent import`` for ``session_id`` and return the new session id."""
    env = {
        **os.environ,
        "HOME": str(home),
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
            session_id,
            "--server",
            base_url,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    match = re.search(r"Imported \d+ item\(s\) into \S+/c/(\S+)", result.stdout)
    assert match is not None, result.stdout
    return match.group(1)


def _relative_label(timestamp_s: float, now_s: float) -> str:
    """Mirror ``web/src/lib/relativeTime.ts`` for the sidebar tooltip."""
    diff = max(0.0, now_s - timestamp_s)
    minute, hour, day = 60.0, 3600.0, 86400.0
    week, month, year = 7 * day, 30 * day, 365 * day
    if diff < minute:
        return "now"
    if diff < hour:
        return f"{int(diff // minute)}m"
    if diff < day:
        return f"{int(diff // hour)}h"
    if diff < week:
        return f"{int(diff // day)}d"
    if diff < month:
        return f"{int(diff // week)}w"
    if diff < year:
        return f"{int(diff // month)}mo"
    return f"{int(diff // year)}y"


def _bubble_stamp(at: datetime) -> re.Pattern[str]:
    """en-US/UTC rendering of ``formatBubbleTimestamp`` for a past-day ``at``."""
    year = "" if at.year == datetime.now(UTC).year else f", {at.year}"
    hour = at.hour % 12 or 12
    meridiem = "AM" if at.hour < 12 else "PM"
    return re.compile(rf"^{at:%b} {at.day}{year}, {hour}:{at:%M}\s?{meridiem}$")


def test_imported_session_recency_reflects_source_time(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """The sidebar dates an imported session by its last source message, not the import."""
    source_session_id = "a1b2c3d4-0721-4000-8000-00000000000a"
    _write_claude_transcript(tmp_path, source_session_id)
    session_id = _import_with_cli(live_server, tmp_path, source_session_id)

    page.goto(f"{live_server}/c/{session_id}")
    row = page.locator(f'li[data-sidebar-session-id="{session_id}"]')
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    tooltip = page.get_by_test_id("session-tooltip-content")
    expect(tooltip).to_be_visible(timeout=10_000)
    expect(tooltip).to_contain_text(f"· {_relative_label(_LAST_AT.timestamp(), time.time())}")

    session = httpx.get(
        f"{live_server}/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=10,
    )
    session.raise_for_status()
    body = session.json()
    first_s, last_s = int(_FIRST_AT.timestamp()), int(_LAST_AT.timestamp())
    assert first_s <= body["created_at"] <= last_s, body["created_at"]
    assert first_s <= body["updated_at"] <= last_s, body["updated_at"]


def test_imported_messages_keep_source_timestamps(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """Each imported bubble shows its own source record time, not the import time."""
    source_session_id = "a1b2c3d4-0721-4000-8000-00000000000b"
    _write_claude_transcript(tmp_path, source_session_id)
    session_id = _import_with_cli(live_server, tmp_path, source_session_id)

    page.goto(f"{live_server}/c/{session_id}")
    first_user = page.locator(_USER_BUBBLE).first
    expect(first_user).to_be_visible(timeout=30_000)
    first_user.hover()
    first_stamp = first_user.locator(_TIMESTAMP)
    expect(first_stamp).to_be_visible(timeout=10_000)
    expect(first_stamp).to_have_text(_bubble_stamp(_FIRST_AT))

    last_assistant = page.locator(_ASSISTANT_BUBBLE).last
    expect(last_assistant).to_be_visible()
    last_assistant.hover()
    last_stamp = last_assistant.locator(_TIMESTAMP)
    expect(last_stamp).to_be_visible(timeout=10_000)
    expect(last_stamp).to_have_text(_bubble_stamp(_LAST_AT))

    items = httpx.get(f"{live_server}/v1/sessions/{session_id}/items", timeout=10)
    items.raise_for_status()
    created = [item["created_at"] for item in items.json()["data"]]
    assert len(created) == len(_TRANSCRIPT_RECORDS), created
    assert len(set(created)) > 1, f"every imported item shares one created_at: {created}"
