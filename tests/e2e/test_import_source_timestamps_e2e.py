"""Real CLI and host imports retain source timestamps in sessions and items."""

from __future__ import annotations

import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online

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
    """The CLI and server preserve two historical activity windows."""
    for session_id, records in _SOURCE_SESSIONS:
        _write_claude_transcript(tmp_path, session_id, records)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
            "OMNIGENT_DATA_DIR": str(tmp_path / "omnigent-data"),
        }
    )

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
    imported_ids = re.findall(r"Imported \d+ item\(s\).*?/c/(\S+)", result.stdout)
    assert len(imported_ids) == len(_SOURCE_SESSIONS), result.stdout

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

        assert created_at < import_started - 60, (
            f"session {source_id} created_at={created_at} looks like the import "
            f"time, not the source ({source_first})"
        )
        assert updated_at < import_started - 60, (
            f"session {source_id} updated_at={updated_at} looks like the import "
            f"time, not the source ({source_last})"
        )
        assert abs(created_at - source_first) <= 60, (
            f"session {source_id} created_at={created_at} != source first message {source_first}"
        )
        assert abs(updated_at - source_last) <= 60, (
            f"session {source_id} updated_at={updated_at} != source last message {source_last}"
        )

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
        assert abs(min(item_stamps) - source_first) <= 60
        assert abs(max(item_stamps) - source_last) <= 60

    assert len(set(session_updated_ats)) == len(_SOURCE_SESSIONS), (
        f"imported sessions share one updated_at {session_updated_ats}; "
        "recency sorting of imported history is meaningless"
    )


def test_host_import_preserves_source_timestamps(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real host forwards source times through the tunnel to the server."""
    source_id = "a1b2c3d4-0801-4000-8000-000000000003"
    records = (
        ("user", "2026-08-01T10:00:00.000Z", "inspect TODO.md"),
        ("assistant", "2026-08-01T10:05:00.000Z", "Done."),
    )
    _write_claude_transcript(tmp_path, source_id, records)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id)
        imported = http_client.post(
            "/v1/imports/local",
            json={"host_id": daemon.host_id, "source": "claude", "session_id": source_id},
            timeout=60,
        )
        imported.raise_for_status()
        assert imported.json()["imported"] == 1
        session_id = imported.json()["sessions"][0]["session_id"]

        session = http_client.get(f"/v1/sessions/{session_id}")
        session.raise_for_status()
        assert int(session.json()["created_at"]) == _epoch(records[0][1])
        assert int(session.json()["updated_at"]) == _epoch(records[-1][1])
        items = http_client.get(f"/v1/sessions/{session_id}/items")
        items.raise_for_status()
        assert [int(item["created_at"]) for item in items.json()["data"]] == [
            _epoch(record[1]) for record in records
        ]
    finally:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait()
