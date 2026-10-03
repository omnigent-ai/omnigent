"""How ``omnigent import`` reports server failures, across newer and older server bodies."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from click.testing import CliRunner, Result

from omnigent.cli import cli
from omnigent.cli_diagnostics import suppresses_recovery_hint
from omnigent.session_import import local as local_import
from omnigent.session_import.errors import ImportErrorCode

_BASE = "http://localhost:6767"
# Oldest first: ``--last N`` imports the N newest of these.
_IDS = (
    "a1b2c3d4-1234-5678-9abc-def012345671",
    "a1b2c3d4-1234-5678-9abc-def012345672",
    "a1b2c3d4-1234-5678-9abc-def012345673",
)


def _imported() -> httpx.Response:
    return httpx.Response(
        201, json={"session_id": "conv_new", "status": "imported", "item_count": 1}
    )


def _error(status: int, **error: Any) -> httpx.Response:
    return httpx.Response(status, json={"error": error})


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A HOME holding three Claude transcripts, one per id in :data:`_IDS`."""
    for modified_at, session_id in enumerate(_IDS, start=1):
        transcript = tmp_path / ".claude" / "projects" / "-repo" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "user",
            "uuid": "u1",
            "cwd": "/repo",
            "message": {"role": "user", "content": "hi"},
        }
        transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
        os.utime(transcript, (modified_at, modified_at))
    return tmp_path


def _run(home: Path, replies: dict[str, httpx.Response], *args: str) -> Result:
    """Run ``omnigent import`` with each session's canned server reply (default: imported)."""

    def _respond(request: httpx.Request) -> httpx.Response:
        return replies.get(json.loads(request.content)["external_session_id"], _imported())

    with respx.mock:
        respx.post(f"{_BASE}/v1/imports").mock(side_effect=_respond)
        with patch("omnigent.cli._resolve_attach_server", return_value=_BASE):
            return CliRunner().invoke(
                cli, ["import", "--harness", "claude", *args], env={"HOME": str(home)}
            )


def test_batch_lists_each_reason_and_offers_a_retry(home: Path) -> None:
    """Each failed session prints its server message, and a retryable failure adds a retry hint."""
    replies = {
        _IDS[1]: _error(
            413,
            code="invalid_input",
            message="This session is too large to import: one message is 5 MB (limit 4 MB).",
            import_code=ImportErrorCode.SESSION_TOO_LARGE,
            retryable=False,
        ),
        _IDS[2]: _error(
            503,
            code="internal_error",
            message="Saving this session timed out.",
            import_code=ImportErrorCode.SESSION_SAVE_TIMEOUT,
            retryable=True,
        ),
    }
    result = _run(home, replies, "--last", "3")
    assert result.exit_code == 1, result.output
    assert (
        f"Failed {_IDS[1]}: Import failed (413): This session is too large to import"
        in result.output
    )
    assert (
        f"Failed {_IDS[2]}: Import failed (503): Saving this session timed out." in result.output
    )
    assert "Imported: 1" in result.output
    assert "Failed: 2" in result.output
    assert "Run the same command again to retry" in result.output


def test_no_retry_hint_when_no_failure_is_retryable(home: Path) -> None:
    """A batch whose failures can't succeed on retry prints no retry hint."""
    replies = {
        _IDS[2]: _error(
            413,
            message="too large",
            import_code=ImportErrorCode.SESSION_TOO_LARGE,
            retryable=False,
        )
    }
    result = _run(home, replies, "--last", "1")
    assert result.exit_code == 1, result.output
    assert "Run the same command again" not in result.output


def test_fix_commands_are_printed_indented(home: Path) -> None:
    """A classified 409 is a failure (not a skip) and prints its fix commands indented."""
    replies = {
        _IDS[0]: _error(
            409,
            code="conflict",
            message="Your machine's Python was built without SQLite.",
            import_code=ImportErrorCode.HOST_PYTHON_MISSING_SQLITE,
            retryable=False,
            fix_commands=[
                {"label": "macOS", "command": "brew install sqlite"},
                {"label": "Linux", "command": "sudo apt-get install libsqlite3-dev"},
                {"label": "no command"},
            ],
        )
    }
    result = _run(home, replies, "--session", _IDS[0])
    assert result.exit_code == 1, result.output
    assert "Python was built without SQLite" in result.output
    assert "\n    macOS: brew install sqlite\n" in result.output
    assert "\n    Linux: sudo apt-get install libsqlite3-dev" in result.output
    # An entry without a command has nothing to paste, so it is dropped.
    assert "no command" not in result.output


def test_plain_string_fix_commands_from_older_servers_still_print(home: Path) -> None:
    """Older servers' plain-string fix commands print as-is."""
    replies = {
        _IDS[0]: _error(
            409,
            message="No SQLite.",
            import_code=ImportErrorCode.HOST_PYTHON_MISSING_SQLITE,
            fix_commands=["macOS: brew install sqlite"],
        )
    }
    result = _run(home, replies, "--session", _IDS[0])
    assert "\n    macOS: brew install sqlite" in result.output


def test_internal_failure_shows_its_error_id(home: Path) -> None:
    """An internal failure prints the server's error id after its message."""
    replies = {
        _IDS[0]: _error(
            500,
            code="internal_error",
            message="Import stopped because of an internal error. Try again.",
            import_code=ImportErrorCode.INTERNAL,
            retryable=True,
            error_id="err_0123",
        )
    }
    result = _run(home, replies, "--session", _IDS[0])
    assert result.exit_code == 1, result.output
    assert (
        "Import failed (500): Import stopped because of an internal error. Try again. "
        "(error ID: err_0123)"
    ) in result.output


def test_batch_prints_fix_commands_under_the_failure(home: Path) -> None:
    """In a batch, fix commands follow the failed session's line."""
    replies = {
        _IDS[2]: _error(
            409,
            message="No SQLite.",
            import_code=ImportErrorCode.HOST_PYTHON_MISSING_SQLITE,
            fix_commands=["macOS: brew install sqlite"],
        )
    }
    result = _run(home, replies, "--last", "1")
    assert result.exit_code == 1, result.output
    assert (
        f"Failed {_IDS[2]}: Import failed (409): No SQLite.\n    macOS: brew install sqlite\n"
        in result.output
    )


def test_older_server_bodies_still_read(home: Path) -> None:
    """Bodies without import codes (or not JSON at all) still print, and count as retryable."""
    replies = {
        _IDS[1]: _error(500, code="internal_error", message="An internal error occurred."),
        _IDS[2]: httpx.Response(502, text="<html>bad gateway</html>"),
    }
    result = _run(home, replies, "--last", "3")
    assert result.exit_code == 1, result.output
    assert "Import failed (500): An internal error occurred." in result.output
    assert "Import failed (502): <html>bad gateway</html>" in result.output
    assert "Run the same command again to retry" in result.output


def test_clean_batch_exits_zero(home: Path) -> None:
    """A batch with no failures exits 0 and prints no retry hint."""
    result = _run(home, {}, "--last", "3")
    assert result.exit_code == 0, result.output
    assert "Imported: 3" in result.output
    assert "Failed: 0" in result.output
    assert "Run the same command again" not in result.output


@pytest.mark.parametrize(
    "error",
    [
        {
            "code": "conflict",
            "message": "This claude session already exists as conv_old",
            "import_code": ImportErrorCode.ALREADY_IMPORTED,
            "retryable": False,
            "session_id": "conv_old",
        },
        {"code": "conflict", "message": "This claude session already exists as conv_old"},
    ],
    ids=["current-server", "older-server"],
)
def test_single_session_duplicate_exits_zero_with_its_link(
    home: Path, error: dict[str, Any]
) -> None:
    """Re-importing one session exits 0 and links the existing session."""
    result = _run(home, {_IDS[0]: _error(409, **error)}, "--session", _IDS[0])
    assert result.exit_code == 0, result.output
    assert f"Already imported as {_BASE}/c/conv_old (use --force to replace)" in result.output


def test_duplicate_without_an_id_still_exits_zero(home: Path) -> None:
    """A duplicate whose body names no session still exits 0."""
    result = _run(home, {_IDS[0]: _error(409, message="already imported")}, "--session", _IDS[0])
    assert result.exit_code == 0, result.output
    assert "Already imported (use --force to replace)" in result.output


def test_batch_names_where_each_duplicate_lives(home: Path) -> None:
    """A batch's skipped duplicate links the existing session."""
    replies = {
        _IDS[2]: _error(
            409, message="dup", import_code=ImportErrorCode.ALREADY_IMPORTED, session_id="conv_dup"
        )
    }
    result = _run(home, replies, "--last", "2")
    assert result.exit_code == 0, result.output
    assert f"Already imported {_IDS[2]} as {_BASE}/c/conv_dup; skipped." in result.output
    assert "Already imported: 1" in result.output


def test_422_shows_the_first_readable_message(home: Path) -> None:
    """A validation error prints its first message with the field path, not the raw list."""
    detail = [
        {"type": "missing", "loc": ["body", "items", 0, "response_id"], "msg": "Field required"},
        {"type": "string_too_long", "loc": ["body", "title"], "msg": "too long"},
    ]
    replies = {_IDS[0]: httpx.Response(422, json={"detail": detail})}
    result = _run(home, replies, "--session", _IDS[0])
    assert result.exit_code == 1, result.output
    assert "Import failed (422): items.0.response_id: Field required" in result.output
    assert "string_too_long" not in result.output
    assert "'loc'" not in result.output


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {
                "loc": ["body", "external_session_id"],
                "msg": "Value error, external_session_id must not be blank",
            },
            "Import failed (422): external_session_id: external_session_id must not be blank",
        ),
        (
            {
                "loc": [
                    "body",
                    "items",
                    12345,
                    "data",
                    "content",
                    0,
                    "source",
                    "media_type_extra",
                ],
                "msg": "Input should be a string",
            },
            "Import failed (422): Input should be a string",
        ),
    ],
    ids=["value-error-prefix", "long-path"],
)
def test_422_trims_value_error_prefix_and_long_paths(
    home: Path, entry: dict[str, Any], expected: str
) -> None:
    """The pydantic ``Value error,`` prefix and over-long field paths are dropped."""
    replies = {_IDS[0]: httpx.Response(422, json={"detail": [entry]})}
    result = _run(home, replies, "--session", _IDS[0])
    assert expected in result.output


def test_422_keeps_a_field_named_body(home: Path) -> None:
    """Only FastAPI's leading ``body`` is dropped from a field path, not a field named body."""
    detail = [{"loc": ["body", "body", "text"], "msg": "Input should be a string"}]
    replies = {_IDS[0]: httpx.Response(422, json={"detail": detail})}
    result = _run(home, replies, "--session", _IDS[0])
    assert "Import failed (422): body.text: Input should be a string" in result.output


@pytest.mark.parametrize(
    ("exc", "retry_hint"),
    [(OSError("disk read failed"), True), (ValueError("bad transcript"), False)],
    ids=["read-fault", "parse-fault"],
)
def test_local_read_fault_is_retryable_but_parse_fault_is_not(
    home: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception, retry_hint: bool
) -> None:
    """A read fault may clear on retry and gets the retry hint; a parse fault does not."""
    real_load = local_import.load_local_session

    def _load(source: Any, session_id: str) -> Any:
        if session_id == _IDS[2]:
            raise exc
        return real_load(source, session_id)

    monkeypatch.setattr(local_import, "load_local_session", _load)
    result = _run(home, {}, "--last", "2")
    assert result.exit_code == 1, result.output
    assert f"Failed {_IDS[2]}: {exc}" in result.output
    assert "Imported: 1" in result.output
    assert ("Run the same command again to retry" in result.output) is retry_hint


def test_validation_failures_are_not_retryable(home: Path) -> None:
    """A 422 in a batch prints its message and no retry hint."""
    detail = [{"loc": ["body", "items"], "msg": "List should have at most 100000 items"}]
    replies = {_IDS[2]: httpx.Response(422, json={"detail": detail})}
    result = _run(home, replies, "--last", "1")
    assert "items: List should have at most 100000 items" in result.output
    assert "Run the same command again" not in result.output


def test_unreadable_detail_falls_back_to_the_body(home: Path) -> None:
    """A 422 with no readable message falls back to the raw body rather than printing nothing."""
    replies = {_IDS[0]: httpx.Response(422, json={"detail": [{"loc": ["body"]}]})}
    result = _run(home, replies, "--session", _IDS[0])
    assert result.exit_code == 1
    assert 'Import failed (422): {"detail"' in result.output


def _raised(home: Path, replies: dict[str, httpx.Response], *args: str) -> BaseException:
    """The exception ``omnigent import`` raises (not rendered by Click)."""
    with respx.mock:
        respx.post(f"{_BASE}/v1/imports").mock(
            side_effect=lambda request: replies.get(
                json.loads(request.content)["external_session_id"], _imported()
            )
        )
        with patch("omnigent.cli._resolve_attach_server", return_value=_BASE):
            result = CliRunner().invoke(
                cli,
                ["import", "--harness", "claude", *args],
                env={"HOME": str(home)},
                standalone_mode=False,
            )
    assert result.exception is not None, result.output
    return result.exception


_NOT_HOST_FAILURES = [
    _error(413, message="no", import_code=ImportErrorCode.SESSION_TOO_LARGE),
    _error(503, message="no", import_code=ImportErrorCode.ENCRYPTION_UNAVAILABLE),
    _error(500, message="no", import_code=ImportErrorCode.INTERNAL),
    httpx.Response(422, json={"detail": [{"loc": ["body", "items"], "msg": "too many"}]}),
]


@pytest.mark.parametrize("reply", _NOT_HOST_FAILURES, ids=["413", "503", "500", "422"])
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_non_host_failures_get_no_stale_host_hint(
    home: Path, reply: httpx.Response, batch: bool
) -> None:
    """Storage and validation failures don't get the ``omnigent stop`` hint."""
    target = _IDS[2] if batch else _IDS[0]
    args = ("--last", "1") if batch else ("--session", _IDS[0])
    assert suppresses_recovery_hint(_raised(home, {target: reply}, *args))


def test_missing_local_session_gets_no_stale_host_hint(home: Path) -> None:
    """A session id with no local transcript doesn't get the stale-host hint."""
    assert suppresses_recovery_hint(_raised(home, {}, "--session", "no-such-session"))


@pytest.mark.parametrize(
    "reply",
    [
        _error(409, message="no", import_code=ImportErrorCode.HOST_OFFLINE),
        _error(401, message="no"),
    ],
    ids=["host-offline", "401"],
)
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_host_tunnel_failures_keep_the_stale_host_hint(
    home: Path, reply: httpx.Response, batch: bool
) -> None:
    """Host-tunnel failures and 401s keep the ``omnigent stop`` hint."""
    target = _IDS[2] if batch else _IDS[0]
    args = ("--last", "1") if batch else ("--session", _IDS[0])
    assert not suppresses_recovery_hint(_raised(home, {target: reply}, *args))
