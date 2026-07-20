"""End-to-end coverage for importing a local harness chat."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx


def test_cli_imports_claude_chat_into_live_server(live_server: str, tmp_path: Path) -> None:
    """The real CLI and server create a readable session from Claude JSONL."""
    source_session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / ".claude" / "projects" / "-repo" / f"{source_session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "cwd": "/repo",
                        "message": {"role": "user", "content": "inspect TODO.md"},
                    }
                ),
                "\n",
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    }
                ),
                "\n",
            ]
        ),
        encoding="utf-8",
    )
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
        timeout=30,
        env=env,
    )

    match = re.search(r"Imported \d+ item\(s\) into (\S+)\.", result.stdout)
    assert match is not None, result.stdout
    session_id = match.group(1)
    session = httpx.get(
        f"{live_server}/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=10,
    )
    session.raise_for_status()
    assert session.json()["external_session_id"] == source_session_id
    assert session.json()["workspace"] == "/repo"
    items = httpx.get(f"{live_server}/v1/sessions/{session_id}/items", timeout=10)
    items.raise_for_status()
    assert [item["type"] for item in items.json()["data"]] == ["message", "message"]


def test_cli_imports_recent_claude_chats_as_batch(live_server: str, tmp_path: Path) -> None:
    """The real CLI imports a bounded recent batch from oldest to newest."""
    source_session_ids = (
        "a1b2c3d4-1234-5678-9abc-def012345671",
        "a1b2c3d4-1234-5678-9abc-def012345672",
        "a1b2c3d4-1234-5678-9abc-def012345673",
    )
    project = tmp_path / ".claude" / "projects" / "-repo"
    project.mkdir(parents=True)
    for modified_at, source_session_id in enumerate(source_session_ids, start=1):
        transcript = project / f"{source_session_id}.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"user-{modified_at}",
                    "cwd": "/repo",
                    "message": {"role": "user", "content": f"prompt {modified_at}"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(transcript, (modified_at, modified_at))
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
            "--last",
            "2",
            "--server",
            live_server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    imported = re.findall(
        r"Imported \d+ item\(s\) from (\S+) into (\S+)\.",
        result.stdout,
    )
    assert [source_id for source_id, _ in imported] == list(source_session_ids[1:])
    assert "Imported: 2" in result.stdout
    assert "Already imported: 0" in result.stdout
    assert "Failed: 0" in result.stdout
    for source_id, session_id in imported:
        session = httpx.get(
            f"{live_server}/v1/sessions/{session_id}",
            params={"include_items": "false", "include_liveness": "false"},
            timeout=10,
        )
        session.raise_for_status()
        assert session.json()["external_session_id"] == source_id


def test_cli_imports_recent_codex_chats_as_batch(live_server: str, tmp_path: Path) -> None:
    """The real CLI discovers and imports recent Codex rollout files."""
    source_session_ids = (
        "019f7777-0001-7000-8000-000000000001",
        "019f7777-0001-7000-8000-000000000002",
        "019f7777-0001-7000-8000-000000000003",
    )
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "07" / "16"
    sessions.mkdir(parents=True)
    for modified_at, source_session_id in enumerate(source_session_ids, start=1):
        rollout = sessions / f"rollout-2026-07-16T00-00-0{modified_at}-{source_session_id}.jsonl"
        rollout.write_text(
            "".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"id": source_session_id, "cwd": "/repo"},
                        }
                    ),
                    "\n",
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": f"prompt {modified_at}",
                                    }
                                ],
                            },
                        }
                    ),
                    "\n",
                ]
            ),
            encoding="utf-8",
        )
        os.utime(rollout, (modified_at, modified_at))
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
            "codex",
            "--last",
            "2",
            "--server",
            live_server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    imported = re.findall(
        r"Imported \d+ item\(s\) from (\S+) into (\S+)\.",
        result.stdout,
    )
    assert [source_id for source_id, _ in imported] == list(source_session_ids[1:])
    assert "Imported: 2" in result.stdout
    for source_id, session_id in imported:
        session = httpx.get(
            f"{live_server}/v1/sessions/{session_id}",
            params={"include_items": "false", "include_liveness": "false"},
            timeout=10,
        )
        session.raise_for_status()
        assert session.json()["external_session_id"] == source_id
