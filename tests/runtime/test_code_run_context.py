"""The code-run notice the agent receives with the user's next message."""

from __future__ import annotations

from typing import Any

from omnigent.runtime.code_run_context import OUTPUT_LIMIT_CHARS, code_run_notice


def _user(text: str = "hi", **extra: Any) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
        **extra,
    }


def _assistant(text: str = "ok") -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _started(run_id: str, *, block: str = "h1", command: str = "echo hi") -> dict[str, Any]:
    return {
        "type": "code_run",
        "run_id": run_id,
        "phase": "started",
        "message_id": "msg_a",
        "block_hash": block,
        "language": "shell",
        "command": command,
        "timeout": 120,
    }


def _finished(
    run_id: str,
    *,
    output: str = "hi\n",
    status: str = "exited",
    exit_code: int | None = 0,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "type": "code_run",
        "run_id": run_id,
        "phase": "finished",
        "status": status,
        "exit_code": exit_code,
        "duration_ms": 1500,
        "output": [{"stream": "stdout", "text": output}],
        "truncated": truncated,
    }


def test_first_message_reports_every_finished_run() -> None:
    notice = code_run_notice([_assistant(), _started("r1"), _finished("r1", output="hello\n")])

    assert "shell" in notice
    assert "echo hi" in notice
    assert "exited" in notice
    assert "exit code 0" in notice
    assert "1.5 s" in notice
    assert "hello" in notice


def test_run_finishing_during_the_reply_is_reported_on_the_next_message() -> None:
    items = [
        _assistant(),
        _started("r1"),
        _user("first"),
        _finished("r1", output="late\n"),
        _assistant(),
    ]

    assert "late" in code_run_notice(items)


def test_run_reported_after_the_message_it_preceded_is_not_repeated() -> None:
    reported_with_first = [_assistant(), _started("r1"), _finished("r1"), _user("first")]
    assert code_run_notice(reported_with_first[:-1]) != ""

    # The user sends a second message before the agent replies.
    assert code_run_notice(reported_with_first) == ""
    assert code_run_notice([*reported_with_first, _user("second")]) == ""


def test_meta_user_messages_do_not_close_the_window() -> None:
    items = [_user("first"), _started("r1"), _finished("r1"), _user("skill", is_meta=True)]

    assert "echo hi" in code_run_notice(items)


def test_only_the_latest_run_of_a_block_is_reported() -> None:
    items = [
        _started("r1", command="echo old"),
        _finished("r1", output="old output\n"),
        _started("r2", command="echo old"),
        _finished("r2", output="new output\n"),
        _started("r3", block="h2", command="pwd"),
        _finished("r3", output="/tmp\n"),
    ]

    notice = code_run_notice(items)

    assert "new output" in notice
    assert "old output" not in notice
    assert "/tmp" in notice


def test_long_output_keeps_start_and_end_with_a_size_note() -> None:
    output = "S" * 6_000 + "M" * 3_000 + "E" * 6_000

    notice = code_run_notice([_started("r1"), _finished("r1", output=output)])

    assert "S" * (OUTPUT_LIMIT_CHARS // 2) in notice
    assert "E" * (OUTPUT_LIMIT_CHARS // 2) in notice
    assert "M" not in notice
    assert "10,000 of 15,000 characters" in notice


def test_output_cut_by_the_runner_is_noted() -> None:
    notice = code_run_notice([_started("r1"), _finished("r1", truncated=True)])

    assert "cut" in notice


def test_started_run_the_runner_reports_active_is_still_running() -> None:
    notice = code_run_notice([_user(), _started("r1", command="sleep 60")], active_run_ids={"r1"})

    assert "sleep 60" in notice
    assert "still running" in notice
    assert "exit code" not in notice


def test_started_run_the_runner_does_not_report_is_left_out() -> None:
    items = [_user(), _started("r1", command="sleep 60")]

    assert code_run_notice(items) == ""
    assert code_run_notice(items, active_run_ids={"other"}) == ""


def test_finished_run_without_a_started_item_is_skipped() -> None:
    assert code_run_notice([_finished("r1")]) == ""


def test_nothing_to_report_gives_empty_text() -> None:
    assert code_run_notice([]) == ""
    assert code_run_notice([_user(), _assistant()]) == ""


def test_command_with_backtick_fences_stays_inside_one_fence() -> None:
    command = "cat <<'EOF'\n```\nx\n```\nEOF"

    notice = code_run_notice([_started("r1", command=command), _finished("r1")])

    assert f"````\n{command}\n````" in notice


def test_cancellation_marker_does_not_close_the_window() -> None:
    from omnigent.entities.conversation import CANCELLATION_MARKER_TEXT

    items = [
        _user("first"),
        _started("r1"),
        _finished("r1", output="during the cancelled reply\n"),
        _user(CANCELLATION_MARKER_TEXT),
    ]

    assert "during the cancelled reply" in code_run_notice(items)


def test_undelivered_user_message_does_not_close_the_window() -> None:
    items = [
        _started("r1"),
        _finished("r1", output="before the lost message\n"),
        _user("never delivered"),
        {"type": "error", "code": "native_prompt_not_recorded", "message": "lost"},
    ]

    assert "before the lost message" in code_run_notice(items)
