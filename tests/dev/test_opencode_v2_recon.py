"""Tests for the pure (network-free) helpers in dev/opencode_v2_recon.py."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from dev.opencode_v2_recon import (
    ASK_ALL_PERMISSIONS,
    build_arg_parser,
    build_recon_opencode_config,
    credential_values_from_env,
    is_terminal_session_event,
    progress_has_incremental_output,
    read_stored_key,
    redact_secrets,
)


def test_arg_parser_requires_model() -> None:
    parser = build_arg_parser()
    with __import__("pytest").raises(SystemExit):
        parser.parse_args([])


def test_arg_parser_defaults() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--model", "anthropic/claude-sonnet-4-5"])
    assert args.model == "anthropic/claude-sonnet-4-5"
    assert args.opencode_path is None
    assert args.keep_workdir is False
    assert args.seed_credentials_from is None
    assert args.seed_provider is None
    assert (
        args.out_dir == Path("tests/fixtures/opencode_v2").resolve()
        or args.out_dir.name == "opencode_v2"
    )


def test_redact_secrets_replaces_every_occurrence() -> None:
    text = "password=hunter2 path=/tmp/hunter2/foo secret=hunter2"
    redacted = redact_secrets(text, ["hunter2"])
    assert "hunter2" not in redacted
    assert redacted.count("<redacted>") == 3


def test_redact_secrets_prefers_longer_matches_first() -> None:
    text = "/tmp/opencode-v2-recon-abc123/nested"
    redacted = redact_secrets(text, ["/tmp/opencode-v2-recon-abc123", "abc123"])
    assert redacted == "<redacted>/nested"


def test_redact_secrets_ignores_blank_entries() -> None:
    assert redact_secrets("hello world", ["", None]) == "hello world"  # type: ignore[list-item]


def test_build_recon_opencode_config_shape(tmp_path: Path) -> None:
    instructions_path = tmp_path / "instructions.txt"
    plugin_path = tmp_path / "omnigent-recon-plugin"
    config = build_recon_opencode_config(
        instructions_path=instructions_path,
        mcp_server_command=["python3", "server.py"],
        plugin_path=plugin_path,
    )
    assert config["permissions"] == ASK_ALL_PERMISSIONS
    assert config["instructions"] == [str(instructions_path)]
    assert config["mcp"]["servers"]["recon-echo"] == {
        "type": "local",
        "command": ["python3", "server.py"],
        "codemode": False,
    }
    assert config["plugins"] == [str(plugin_path)]


def test_credential_values_from_env_matches_credential_like_names() -> None:
    env = {
        "ANTHROPIC_API_KEY": "sk-secret1",
        "GITHUB_TOKEN": "ghp-secret2",
        "MY_SECRET": "shh",
        "OPENCODE_PASSWORD": "pw123",
        "SOME_PASSWORD_THING": "pw456",
        "PATH": "/usr/bin",
        "EMPTY_TOKEN": "",
    }
    assert set(credential_values_from_env(env)) == {
        "sk-secret1",
        "ghp-secret2",
        "shh",
        "pw123",
        "pw456",
    }


def test_credential_values_from_env_empty_for_no_matches() -> None:
    assert credential_values_from_env({"PATH": "/usr/bin", "HOME": "/home/x"}) == []


def test_main_help_exits_zero_and_documents_model_flag(capsys: object) -> None:
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        from dev.opencode_v2_recon import main

        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "--model" in captured.out


def test_run_recon_is_importable_and_callable() -> None:
    from dev.opencode_v2_recon import run_recon

    assert callable(run_recon)


def test_is_terminal_session_event_matches_legacy_session_idle() -> None:
    event = {"type": "session.idle", "data": {"sessionID": "ses_1"}}
    assert is_terminal_session_event(event, "ses_1") is True


def test_is_terminal_session_event_matches_status_idle_nested_type() -> None:
    event = {"type": "session.status", "data": {"sessionID": "ses_1", "status": {"type": "idle"}}}
    assert is_terminal_session_event(event, "ses_1") is True


def test_is_terminal_session_event_matches_status_idle_flat_string() -> None:
    event = {"type": "session.status", "data": {"sessionID": "ses_1", "status": "idle"}}
    assert is_terminal_session_event(event, "ses_1") is True


def test_is_terminal_session_event_ignores_busy_and_retry_status() -> None:
    busy = {"type": "session.status", "data": {"sessionID": "ses_1", "status": {"type": "busy"}}}
    retry = {"type": "session.status", "data": {"sessionID": "ses_1", "status": {"type": "retry"}}}
    assert is_terminal_session_event(busy, "ses_1") is False
    assert is_terminal_session_event(retry, "ses_1") is False


def test_is_terminal_session_event_matches_execution_terminal_types() -> None:
    for event_type in (
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
    ):
        event = {"type": event_type, "data": {"sessionID": "ses_1"}}
        assert is_terminal_session_event(event, "ses_1") is True


def test_is_terminal_session_event_ignores_other_session_ids() -> None:
    event = {"type": "session.idle", "data": {"sessionID": "ses_other"}}
    assert is_terminal_session_event(event, "ses_1") is False


def test_is_terminal_session_event_ignores_unrelated_event_types() -> None:
    event = {"type": "session.tool.progress", "data": {"sessionID": "ses_1"}}
    assert is_terminal_session_event(event, "ses_1") is False


def _make_credential_db(tmp_path: Path, rows: list[tuple[object, ...]]) -> Path:
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE credential (
            id TEXT PRIMARY KEY,
            integration_id TEXT,
            label TEXT NOT NULL,
            value TEXT NOT NULL,
            connector_id TEXT,
            method_id TEXT,
            active INTEGER,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        )
        """
    )
    conn.executemany("INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


def test_read_stored_key_returns_newest_active_key_credential(tmp_path: Path) -> None:
    db_path = _make_credential_db(
        tmp_path,
        [
            (
                "cred_1",
                "anthropic",
                "default",
                json.dumps({"type": "key", "key": "sk-ant-old"}),
                "connector_1",
                "method_1",
                0,
                1,
                1,
            ),
            (
                "cred_2",
                "anthropic",
                "default",
                json.dumps({"type": "key", "key": "sk-ant-new"}),
                "connector_2",
                "method_1",
                1,
                2,
                2,
            ),
        ],
    )
    assert read_stored_key(db_path, "anthropic") == "sk-ant-new"


def test_read_stored_key_returns_none_for_missing_provider(tmp_path: Path) -> None:
    db_path = _make_credential_db(tmp_path, [])
    assert read_stored_key(db_path, "anthropic") is None


def test_read_stored_key_skips_oauth_credential(tmp_path: Path, capsys: object) -> None:
    db_path = _make_credential_db(
        tmp_path,
        [
            (
                "cred_1",
                "github",
                "default",
                json.dumps(
                    {
                        "type": "oauth",
                        "methodID": "m1",
                        "refresh": "r",
                        "access": "a",
                        "expires": 0,
                    }
                ),
                "connector_1",
                "method_1",
                1,
                1,
                1,
            ),
        ],
    )
    assert read_stored_key(db_path, "github") is None
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "oauth" in captured.err


def _progress_event(tool_call_id: str, metadata: dict[str, object]) -> dict[str, object]:
    return {"type": "session.tool.progress", "data": {"id": tool_call_id, "metadata": metadata}}


def test_progress_has_incremental_output_detects_growing_string_field() -> None:
    events = [
        _progress_event("call_1", {"output": "Hel"}),
        _progress_event("call_1", {"output": "Hello"}),
        _progress_event("call_1", {"output": "Hello, world"}),
    ]
    has_incremental, keys = progress_has_incremental_output(events)
    assert has_incremental is True
    assert keys == ["output"]


def test_progress_has_incremental_output_false_for_static_metadata() -> None:
    events = [
        _progress_event("call_1", {"status": "running"}),
        _progress_event("call_1", {"status": "running"}),
    ]
    has_incremental, keys = progress_has_incremental_output(events)
    assert has_incremental is False
    assert keys == ["status"]


def test_progress_has_incremental_output_false_for_replaced_not_grown_value() -> None:
    events = [
        _progress_event("call_1", {"output": "abc"}),
        _progress_event("call_1", {"output": "xyz"}),
    ]
    has_incremental, keys = progress_has_incremental_output(events)
    assert has_incremental is False
    assert keys == ["output"]


def test_progress_has_incremental_output_does_not_mix_different_tool_calls() -> None:
    events = [
        _progress_event("call_1", {"output": "Hello"}),
        _progress_event("call_2", {"output": "H"}),
    ]
    has_incremental, keys = progress_has_incremental_output(events)
    assert has_incremental is False
    assert keys == ["output"]


def test_progress_has_incremental_output_empty_for_no_progress_events() -> None:
    events = [{"type": "session.status", "data": {"sessionID": "ses_1"}}]
    has_incremental, keys = progress_has_incremental_output(events)
    assert has_incremental is False
    assert keys == []


def test_progress_has_incremental_output_collects_all_metadata_keys() -> None:
    events = [
        _progress_event("call_1", {"output": "a", "phase": "start"}),
        _progress_event("call_1", {"output": "ab", "phase": "start"}),
    ]
    has_incremental, keys = progress_has_incremental_output(events)
    assert has_incremental is True
    assert keys == ["output", "phase"]
