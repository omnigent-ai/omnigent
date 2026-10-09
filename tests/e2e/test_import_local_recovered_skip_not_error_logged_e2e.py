"""Host-mediated Claude import: recovered per-session skips must not log at ERROR."""

from __future__ import annotations

import json
import re
import signal
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online

pytestmark = [pytest.mark.timeout(180)]

_GOOD_ID = "6f0d9f4e-2f0f-4bde-9b6e-2f6a0f6c1a01"
_DEEP_ID = "bad0bad0-c0de-4bad-9dad-badbadbadbad"
_UTF8_ID = "c0ffee00-0bad-4bad-9bad-0badc0ffee00"


def _user_record(session_id: str, cwd: str, content: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": f"{session_id}-u1",
            "sessionId": session_id,
            "cwd": cwd,
            "message": {"role": "user", "content": content},
        }
    )


def _write_claude_transcripts(home: Path, cwd: str) -> None:
    """Write one healthy transcript and two the import loader cannot read.

    The deeply nested record makes ``json.loads`` raise ``RecursionError``, which
    escapes the loader's ``ValueError`` net into the handler's catch-all; the
    invalid UTF-8 line raises ``UnicodeDecodeError`` inside that net.
    """
    project = home / ".claude" / "projects" / "-repo"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{_GOOD_ID}.jsonl").write_text(
        _user_record(_GOOD_ID, cwd, "healthy transcript") + "\n", encoding="utf-8"
    )
    nested = "[" * 20000 + '"x"' + "]" * 20000
    deep_record = (
        '{"type":"user","uuid":"'
        + _DEEP_ID
        + '-u2","cwd":'
        + json.dumps(cwd)
        + ',"message":{"role":"user","content":'
        + nested
        + "}}"
    )
    (project / f"{_DEEP_ID}.jsonl").write_text(
        _user_record(_DEEP_ID, cwd, "nested record follows") + "\n" + deep_record + "\n",
        encoding="utf-8",
    )
    (project / f"{_UTF8_ID}.jsonl").write_bytes(
        (_user_record(_UTF8_ID, cwd, "invalid utf-8 follows") + "\n").encode("utf-8")
        + b'{"type":"user","message":{"role":"user","content":"\xff\xfe"}}\n'
    )


def test_recovered_import_skips_are_not_logged_as_errors(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """The import still skips and counts unreadable transcripts, without ERROR records.

    The host mirrors ERROR records to its terminal and ships them to the
    debug-log sink, where the session-reliability KPI counts them; a recovered,
    reported skip must stay below ERROR with no traceback.
    """
    _write_claude_transcripts(tmp_path, str(tmp_path))
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id)
        response = http_client.post(
            "/v1/imports/local",
            json={"host_id": daemon.host_id, "source": "claude", "limit": 25},
            timeout=120.0,
        )
    finally:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait(timeout=5)

    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["imported"], result["already_imported"], result["failed"]) == (1, 0, 2)
    assert {f["external_session_id"]: f["reason"] for f in result["failures"]} == {
        _DEEP_ID: "This session could not be read.",
        _UTF8_ID: "This session's transcript could not be read.",
    }

    log_text = daemon.daemon_log.read_text(encoding="utf-8", errors="replace")
    skip_lines = [line for line in log_text.splitlines() if "import_local" in line]
    assert skip_lines, f"host never logged the skipped transcripts:\n{log_text}"
    error_lines = [line for line in skip_lines if line.startswith("ERROR")]
    assert error_lines == [], f"recovered skips logged at ERROR:\n{log_text}"
    assert not re.search(r"import_local[^\n]*\nTraceback \(most recent call last\)", log_text), (
        f"recovered skips logged with a traceback:\n{log_text}"
    )
    # One diagnostic line per skipped transcript, still naming what was wrong with it.
    deep_lines = [line for line in skip_lines if _DEEP_ID in line]
    utf8_lines = [line for line in skip_lines if _UTF8_ID in line]
    assert len(deep_lines) == 1 and "RecursionError" in deep_lines[0], log_text
    assert len(utf8_lines) == 1 and "UnicodeDecodeError" in utf8_lines[0], log_text
