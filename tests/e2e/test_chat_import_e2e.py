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
