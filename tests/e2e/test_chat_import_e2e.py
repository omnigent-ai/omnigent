"""End-to-end coverage for importing a local harness chat."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest


def _write_jsonl_import_fixture(home: Path, harness: str) -> str:
    """Write one two-message native transcript and return its source id."""
    if harness == "qwen":
        session_id = "019f8648-2797-7170-bf73-837f2655c471"
        transcript = home / ".qwen" / "projects" / "-repo" / "chats" / f"{session_id}.jsonl"
        records = [
            {
                "uuid": "user-1",
                "parentUuid": None,
                "sessionId": session_id,
                "timestamp": "2026-07-21T12:00:00Z",
                "type": "user",
                "cwd": "/repo",
                "message": {"role": "user", "parts": [{"text": "inspect TODO.md"}]},
            },
            {
                "uuid": "assistant-1",
                "parentUuid": "user-1",
                "sessionId": session_id,
                "timestamp": "2026-07-21T12:00:01Z",
                "type": "assistant",
                "cwd": "/repo",
                "message": {"role": "model", "parts": [{"text": "Done."}]},
            },
        ]
    elif harness == "kiro":
        session_id = "kiro-import-e2e"
        root = home / ".kiro" / "sessions" / "cli"
        transcript = root / f"{session_id}.jsonl"
        root.mkdir(parents=True)
        (root / f"{session_id}.json").write_text(
            json.dumps({"cwd": "/repo", "created_at": "2026-07-21T12:00:00Z"}),
            encoding="utf-8",
        )
        records = [
            {
                "kind": "Prompt",
                "data": {
                    "message_id": "user-1",
                    "content": [{"kind": "text", "data": "inspect TODO.md"}],
                },
            },
            {
                "kind": "AssistantMessage",
                "data": {
                    "message_id": "assistant-1",
                    "content": [{"kind": "text", "data": "Done."}],
                },
            },
        ]
    elif harness == "pi":
        session_id = "019f8648-2797-7170-bf73-837f2655c472"
        transcript = home / ".pi" / "agent" / "sessions" / "--repo--" / f"stamp_{session_id}.jsonl"
        records = [
            {"type": "session", "version": 3, "id": session_id, "cwd": "/repo"},
            {
                "type": "message",
                "id": "11111111",
                "parentId": None,
                "timestamp": "2026-07-21T12:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "inspect TODO.md"}],
                    "timestamp": 0,
                },
            },
            {
                "type": "message",
                "id": "22222222",
                "parentId": "11111111",
                "timestamp": "2026-07-21T12:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done."}],
                    "api": "anthropic-messages",
                    "provider": "anthropic",
                    "model": "claude-sonnet",
                    "usage": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 0,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "total": 0,
                        },
                    },
                    "stopReason": "stop",
                    "timestamp": 0,
                },
            },
        ]
    else:
        session_id = "session_import_e2e"
        session_dir = home / ".kimi-code" / "sessions" / "wd_repo" / session_id
        transcript = session_dir / "agents" / "main" / "wire.jsonl"
        index = home / ".kimi-code" / "session_index.jsonl"
        index.parent.mkdir(parents=True)
        index.write_text(
            json.dumps({"sessionDir": str(session_dir), "workDir": "/repo"}) + "\n",
            encoding="utf-8",
        )
        records = [
            {
                "type": "turn.prompt",
                "origin": {"kind": "user"},
                "input": [{"type": "text", "text": "inspect TODO.md"}],
            },
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "uuid": "assistant-1",
                    "part": {"type": "text", "text": "Done."},
                },
            },
        ]
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    return f"-repo:{session_id}" if harness == "qwen" else session_id


def _write_opencode_cli_fixture(home: Path) -> tuple[Path, str]:
    """Write a fake public OpenCode CLI and return its bin dir plus session id."""
    session_id = "ses_import_e2e"
    listing = [
        {
            "id": session_id,
            "title": "OpenCode import",
            "updated": 1784247215390,
            "created": 1784247214912,
            "directory": "/repo",
        }
    ]
    export = {
        "info": {"id": session_id, "directory": "/repo", "version": "1.17.18"},
        "messages": [
            {
                "info": {"id": "msg_user", "role": "user"},
                "parts": [{"type": "text", "text": "inspect TODO.md"}],
            },
            {
                "info": {"id": "msg_assistant", "role": "assistant"},
                "parts": [
                    {
                        "type": "tool",
                        "callID": "call_1",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "rg TODO"},
                            "output": "TODO.md:1:item",
                        },
                    },
                    {"type": "text", "text": "Done."},
                ],
            },
        ],
    }
    bin_dir = home / "bin"
    executable = bin_dir / "opencode"
    bin_dir.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        f"listing = {listing!r}\n"
        f"export = {export!r}\n"
        "if sys.argv[1:3] == ['session', 'list']:\n"
        "    print(json.dumps(listing))\n"
        f"elif sys.argv[1:] == ['export', '{session_id}', '--pure']:\n"
        "    print(json.dumps(export))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_dir, session_id


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
    session_data = session.json()
    assert session_data["external_session_id"] == source_session_id
    assert session_data["workspace"] == "/repo"
    assert session_data["title"] == "inspect TODO.md"
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


@pytest.mark.parametrize("harness", ["qwen", "kiro", "pi", "kimi"])
def test_cli_imports_jsonl_harness_chat_end_to_end(
    live_server: str,
    tmp_path: Path,
    harness: str,
) -> None:
    """The real CLI discovers, uploads, and serves each supported JSONL format."""
    source_session_id = _write_jsonl_import_fixture(tmp_path, harness)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "QWEN_HOME": str(tmp_path / ".qwen"),
            "PI_CODING_AGENT_DIR": str(tmp_path / ".pi" / "agent"),
            "KIMI_CODE_HOME": str(tmp_path / ".kimi-code"),
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
            harness,
            "--last",
            "1",
            "--server",
            live_server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    match = re.search(
        rf"Imported 2 item\(s\) from {re.escape(source_session_id)} into (\S+)\.",
        result.stdout,
    )
    assert match is not None, result.stdout
    imported_session_id = match.group(1)
    session = httpx.get(
        f"{live_server}/v1/sessions/{imported_session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=10,
    )
    session.raise_for_status()
    session_data = session.json()
    assert session_data["external_session_id"] == source_session_id
    assert session_data["workspace"] == "/repo"
    assert session_data["title"] == "inspect TODO.md"
    items = httpx.get(
        f"{live_server}/v1/sessions/{imported_session_id}/items",
        timeout=10,
    )
    items.raise_for_status()
    item_data = items.json()["data"]
    assert [item["type"] for item in item_data] == ["message", "message"]
    assert [item["role"] for item in item_data] == ["user", "assistant"]
    assert [item["content"][0]["text"] for item in item_data] == [
        "inspect TODO.md",
        "Done.",
    ]
    assert item_data[1]["model"] == f"{harness}-native-ui"


def test_cli_imports_opencode_export_end_to_end(
    live_server: str,
    tmp_path: Path,
) -> None:
    """The real CLI discovers and imports OpenCode's public JSON export."""
    bin_dir, source_session_id = _write_opencode_cli_fixture(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
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
            "opencode",
            "--last",
            "1",
            "--server",
            live_server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    match = re.search(
        rf"Imported 4 item\(s\) from {source_session_id} into (\S+)\.",
        result.stdout,
    )
    assert match is not None, result.stdout
    imported_session_id = match.group(1)
    session = httpx.get(
        f"{live_server}/v1/sessions/{imported_session_id}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout=10,
    )
    session.raise_for_status()
    session_data = session.json()
    assert session_data["external_session_id"] == source_session_id
    assert session_data["workspace"] == "/repo"
    assert session_data["title"] == "inspect TODO.md"
    items = httpx.get(
        f"{live_server}/v1/sessions/{imported_session_id}/items",
        timeout=10,
    )
    items.raise_for_status()
    item_data = items.json()["data"]
    assert [item["type"] for item in item_data] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert item_data[1]["model"] == "opencode-native-ui"
