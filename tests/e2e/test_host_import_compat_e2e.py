"""Cross-version coverage for host-mediated local session imports."""

from __future__ import annotations

import json
import signal
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online

pytestmark = [pytest.mark.compat_smoke, pytest.mark.timeout(120)]


def _write_ten_mib_claude_session(home: Path, session_id: str) -> None:
    """Write an importable session above the chunk threshold but below 100 MiB."""
    transcript = home / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "uuid": f"{session_id}-title",
            "cwd": "/repo",
            "message": {"role": "user", "content": "compat import title"},
        },
        {
            "type": "user",
            "uuid": f"{session_id}-payload",
            "cwd": "/repo",
            "message": {"role": "user", "content": "x" * (10 * 1024 * 1024)},
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def test_ten_mib_import_survives_mixed_server_and_host_versions(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A negotiated import works with either the server or host pinned old.

    Config 1 runs the current server with a released host; the extra request
    field must be ignored. Config 2 runs the current host with a released
    server; the missing field must retain legacy whole-session framing.
    """
    session_id = str(uuid.uuid4())
    _write_ten_mib_claude_session(tmp_path, session_id)
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id)
        response = http_client.post(
            "/v1/imports/local",
            json={
                "host_id": daemon.host_id,
                "source": "claude",
                "session_id": session_id,
            },
            timeout=90.0,
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["imported"] == 1
        assert result["already_imported"] == 0
        assert result["failed"] == 0
    finally:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait(timeout=5)
