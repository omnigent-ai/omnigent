"""E2E (web): imported message timestamps must show the source time.

The chat renders each
message's own time (``message-timestamp``, from the item's ``created_at``). When
``omnigent import`` discards the source per-record timestamps and the server
stamps ``now()`` on every imported item, every message in an imported session
displays the *import* moment rather than when the message was actually sent.

This drives the real user journey against the spawned server: the genuine
``omnigent import`` CLI imports a Claude Code transcript whose messages are
dated in the past (July 2026), then the SPA opens that session and reads the
per-message timestamp. On the current build the timestamp shows the import time
(today), so the ``"Jul 21"`` assertion fails; the fix that preserves per-item
source timestamps makes the bubble show the historical date and the test pass.

This is also the recording driver for the ``web`` facet: it films the imported
chat showing the wrong (import-time) message timestamps.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

# The July 2026 source records (months before any test run) with the ISO-8601
# ``timestamp`` Claude writes per line. The session id is randomized per run so
# re-imports never collide on the deterministic imported-conversation id (the
# ``live_server`` fixture is shared across tests / flaky reruns).
_SOURCE_RECORDS: tuple[tuple[str, str, str], ...] = (
    ("user", "2026-07-21T12:00:00.000Z", "inspect TODO.md"),
    ("assistant", "2026-07-21T12:00:30.000Z", "TODO.md has three items."),
    ("user", "2026-07-21T12:09:00.000Z", "fix the first one"),
    ("assistant", "2026-07-21T12:10:00.000Z", "Done."),
)


def _seed_claude_transcript(home: Path, session_id: str) -> None:
    """Write the July transcript and set its mtime to the last activity time."""
    transcript = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    parent: str | None = None
    for index, (record_type, iso_timestamp, text) in enumerate(_SOURCE_RECORDS):
        uuid = f"{record_type}-{index}"
        if record_type == "user":
            message: dict[str, object] = {"role": "user", "content": text}
        else:
            message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        lines.append(
            json.dumps(
                {
                    "type": record_type,
                    "uuid": uuid,
                    "parentUuid": parent,
                    "sessionId": session_id,
                    "timestamp": iso_timestamp,
                    "cwd": "/repo",
                    "message": message,
                }
            )
        )
        parent = uuid
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Last-message time, mirroring a real on-disk transcript's mtime.
    os.utime(transcript, (1784635800, 1784635800))


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_imported_message_timestamps_reflect_source_not_import(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """An imported message shows its July source time, not the import time.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the spawned server serving the built SPA.
    :param tmp_path: Per-test temp dir used as the import CLI's ``HOME``.
    """
    source_session_id = str(uuid4())
    _seed_claude_transcript(tmp_path, source_session_id)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
            "OMNIGENT_DATA_DIR": str(tmp_path / "omnigent-data"),
        }
    )
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
            live_server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    match = re.search(r"Imported \d+ item\(s\).*?/c/(\S+)", result.stdout)
    assert match is not None, result.stdout
    session_id = match.group(1)

    # Open the imported chat. Wait on concrete transcript content (an imported
    # session keeps its SSE stream / terminal socket open, so networkidle never
    # settles); the first user message anchors the assertion.
    page.goto(f"{live_server}/c/{session_id}")
    # Scope to the transcript log: the message text also appears in the sidebar
    # row and the header title, so an unscoped match is ambiguous.
    transcript = page.get_by_role("log")
    first_message = transcript.get_by_text("inspect TODO.md")
    expect(first_message).to_be_visible(timeout=30_000)

    # Reveal the per-message timestamp (hover-revealed on desktop) so it's in
    # frame for the recording, then assert on its text.
    first_message.hover()
    timestamps = transcript.get_by_test_id("message-timestamp")
    expect(timestamps.first).to_be_visible(timeout=10_000)
    # Let the revealed (wrong) timestamps settle on screen so they are legible
    # in the recording; harmless to the assertion below.
    page.wait_for_timeout(1500)

    # The message was sent 2026-07-21, so a preserved timestamp shows the July
    # date. The bug stamps the import time (today) instead, which renders as a
    # bare clock time with no month -- so no rendered timestamp contains "Jul".
    stamp_texts = timestamps.all_inner_texts()
    assert any("Jul" in text for text in stamp_texts), (
        "no imported message shows its July source date; every message time "
        f"reflects the import run instead: {stamp_texts}"
    )
