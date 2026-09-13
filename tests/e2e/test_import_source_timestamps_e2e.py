"""E2E regression guard: ``omnigent import`` preserves source timestamps.

``omnigent import`` must preserve the source session's timestamps. The real
CLI reads a Claude Code transcript whose records each carry an ISO-8601
``timestamp`` (and whose file mtime reflects last activity), then POSTs the
items to ``POST /v1/imports``. The bug: none of those source times are
threaded through, so the server stamps ``now()`` on the conversation row and
on every item row. The result is that every imported session -- and every
item inside it -- reports the moment the import ran, which makes recency
sorting of imported history meaningless.

This test drives the real user journey (``python -m omnigent import --harness
claude --last 2`` against a live server) with two seeded transcripts whose
message times are months in the past (June and July 2026), then reads the
result back through the public API. It fails on the current build because the
imported timestamps are the import time rather than the source times, and is
the fail->pass target for a fix that threads source timestamps end-to-end.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Two source sessions with distinct historical activity windows. Each entry is
# ``(session_id, [(record_type, iso_timestamp, text), ...])``; the file mtime
# is set to the last message time to mirror a real on-disk transcript.
_SOURCE_SESSIONS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "a1b2c3d4-0615-4000-8000-000000000001",
        (
            ("user", "2026-06-15T09:00:00.000Z", "summarize README"),
            ("assistant", "2026-06-15T09:00:20.000Z", "The README describes Omnigent."),
            ("user", "2026-06-15T09:04:00.000Z", "thanks"),
            ("assistant", "2026-06-15T09:05:00.000Z", "You're welcome!"),
        ),
    ),
    (
        "a1b2c3d4-0721-4000-8000-000000000002",
        (
            ("user", "2026-07-21T12:00:00.000Z", "inspect TODO.md"),
            ("assistant", "2026-07-21T12:00:30.000Z", "TODO.md has three items."),
            ("user", "2026-07-21T12:09:00.000Z", "fix the first one"),
            ("assistant", "2026-07-21T12:10:00.000Z", "Done."),
        ),
    ),
)


def _epoch(iso_timestamp: str) -> int:
    """Unix seconds for one of the transcript's ISO-8601 ``timestamp`` values."""
    return int(datetime.datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp())


def _write_claude_transcript(
    home: Path, session_id: str, records: tuple[tuple[str, str, str], ...]
) -> None:
    """Write one Claude Code transcript and set its mtime to the last activity."""
    transcript = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    parent: str | None = None
    for index, (record_type, iso_timestamp, text) in enumerate(records):
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
    last_epoch = _epoch(records[-1][1])
    os.utime(transcript, (last_epoch, last_epoch))


def test_cli_import_preserves_source_timestamps(live_server: str, tmp_path: Path) -> None:
    """Imported session + item timestamps reflect the source, not the import run.

    On a build that discards source times, every imported session and every
    imported item is stamped with ``now()`` (the import time), so this test
    fails at the first timestamp assertion. Threading source timestamps
    through ``ImportItemInput`` / the conversation store makes the imported
    rows carry the June/July source times and the test pass.

    :param live_server: Base URL of the spawned server fixture.
    :param tmp_path: Per-test temp dir used as the CLI's ``HOME``.
    """
    for session_id, records in _SOURCE_SESSIONS:
        _write_claude_transcript(tmp_path, session_id, records)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
            "OMNIGENT_DATA_DIR": str(tmp_path / "omnigent-data"),
        }
    )

    # Fence the import against the wall clock: the source times are months in
    # the past, so a correctly-preserved timestamp lands well before this.
    import_started = time.time()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "import",
            "--harness",
            "claude",
            "--last",
            "2",
            "--server",
            live_server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    # One "Imported N item(s) ... /c/<id>" line per source session.
    imported_ids = re.findall(r"Imported \d+ item\(s\).*?/c/(\S+)", result.stdout)
    assert len(imported_ids) == len(_SOURCE_SESSIONS), result.stdout

    # Map each server session back to its source id via external_session_id so
    # we can compare against the exact source times we wrote.
    sessions_by_source: dict[str, dict[str, object]] = {}
    for session_id in imported_ids:
        resp = httpx.get(
            f"{live_server}/v1/sessions/{session_id}",
            params={"include_items": "false", "include_liveness": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        sessions_by_source[data["external_session_id"]] = data

    session_updated_ats: list[int] = []
    for source_id, records in _SOURCE_SESSIONS:
        session = sessions_by_source[source_id]
        source_first = _epoch(records[0][1])
        source_last = _epoch(records[-1][1])

        created_at = int(session["created_at"])
        updated_at = int(session["updated_at"])
        session_updated_ats.append(updated_at)

        # The bug stamps the import time on both; a preserved timestamp is the
        # source window (months before this run), never after the import began.
        assert created_at < import_started - 60, (
            f"session {source_id} created_at={created_at} looks like the import "
            f"time, not the source ({source_first})"
        )
        assert updated_at < import_started - 60, (
            f"session {source_id} updated_at={updated_at} looks like the import "
            f"time, not the source ({source_last})"
        )
        # Tie tightly to the source first/last message times (small tolerance).
        assert abs(created_at - source_first) <= 60, (
            f"session {source_id} created_at={created_at} != source first message {source_first}"
        )
        assert abs(updated_at - source_last) <= 60, (
            f"session {source_id} updated_at={updated_at} != source last message {source_last}"
        )

        # Per-item times must be preserved, not collapsed to one import stamp.
        items_resp = httpx.get(
            f"{live_server}/v1/sessions/{session['id']}/items",
            params={"limit": 1000},
            timeout=10,
        )
        items_resp.raise_for_status()
        item_rows = items_resp.json()["data"]
        item_stamps = [int(item["created_at"]) for item in item_rows]
        assert len(set(item_stamps)) > 1, (
            f"session {source_id} items collapsed to a single created_at "
            f"{set(item_stamps)} -- source timestamps were discarded"
        )
        assert min(item_stamps) < import_started - 60, (
            f"session {source_id} item timestamps look like the import time, not the source window"
        )
        # The earliest/latest item times should bracket the source window.
        assert abs(min(item_stamps) - source_first) <= 60
        assert abs(max(item_stamps) - source_last) <= 60

    # Recency must be meaningful: the two imported sessions had distinct source
    # windows (June vs. July), so their updated_at must differ -- the bug makes
    # them identical (both the import time).
    assert len(set(session_updated_ats)) == len(_SOURCE_SESSIONS), (
        f"imported sessions share one updated_at {session_updated_ats}; "
        "recency sorting of imported history is meaningless"
    )
