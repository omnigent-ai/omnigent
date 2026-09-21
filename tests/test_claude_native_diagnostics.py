"""Bounded, opt-in forwarding of owned Claude diagnostic files."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from omnigent._platform import IS_POSIX
from omnigent.debug_logging import record_to_row
from omnigent.harnesses.claude_native import bridge, diagnostics
from omnigent.process_logging import HARNESS_STDERR_ENABLED_ENV_VAR

pytestmark = pytest.mark.skipif(not IS_POSIX, reason="Native diagnostic files use POSIX bridges")


@pytest.fixture(autouse=True)
def clear_capture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HARNESS_STDERR_ENABLED_ENV_VAR, raising=False)


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    path = tmp_path / "bridge"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def capture_file(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Path:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")
    caplog.set_level(logging.INFO, logger=diagnostics.__name__)
    args = diagnostics.augment_claude_debug_args([], bridge_dir)
    assert args[0] == "--debug-file"
    return Path(args[1])


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        record.attributes
        for record in caplog.records
        if getattr(record, "event_name", None) == "harness_diagnostic_output"
    ]


def test_disabled_does_not_read_or_create_files(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(diagnostics, "_open_directory", unexpected)
    monkeypatch.setattr(bridge, "_ensure_secure_dir", unexpected)
    args = ["--resume", "session"]
    assert diagnostics.augment_claude_debug_args(args, bridge_dir) is args
    follower = diagnostics.ClaudeDebugLogFollower(bridge_dir)
    follower.poll("conv_test")
    follower.close("conv_test")
    assert list(bridge_dir.iterdir()) == []


def test_fresh_launch_files_are_private_and_replace_previous_generation(
    capture_file: Path,
) -> None:
    bridge_dir = capture_file.parent
    marker = bridge_dir / diagnostics.CLAUDE_DEBUG_LOG_MARKER
    first = json.loads(marker.read_text())
    assert first == {
        "filename": capture_file.name,
        "launch_id": capture_file.name.removeprefix("claude-debug-").removesuffix(".log"),
    }
    assert stat.S_IMODE(capture_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    rotated = capture_file.with_name(capture_file.name + ".1")
    rotated.write_text("old diagnostic data")

    args = diagnostics.augment_claude_debug_args(["--model", "sonnet"], bridge_dir)

    assert args[:2] == ["--model", "sonnet"]
    assert Path(args[-1]).exists()
    assert Path(args[-1]) != capture_file
    assert not capture_file.exists()
    assert not rotated.exists()
    assert json.loads(marker.read_text())["launch_id"] != first["launch_id"]


@pytest.mark.parametrize("args", [["--debug-file", "custom.log"], ["--debug-file=custom.log"]])
def test_explicit_debug_file_preserved_and_stale_capture_cleared(
    capture_file: Path, args: list[str]
) -> None:
    custom = capture_file.parent / "custom.log"
    custom.write_text("user-owned file")

    assert diagnostics.augment_claude_debug_args(args, capture_file.parent) is args
    assert custom.read_text() == "user-owned file"
    assert not (capture_file.parent / diagnostics.CLAUDE_DEBUG_LOG_MARKER).exists()
    assert not capture_file.exists()


@pytest.mark.parametrize("prompt", ["literal prompt", "--debug-file=user prompt text"])
def test_debug_flag_is_inserted_before_argument_separator(capture_file: Path, prompt: str) -> None:
    args = ["--model", "sonnet", "--", prompt]
    result = diagnostics.augment_claude_debug_args(args, capture_file.parent)

    assert result[:3] == ["--model", "sonnet", "--debug-file"]
    assert Path(result[3]).is_file()
    assert result[4:] == ["--", prompt]
    assert args == ["--model", "sonnet", "--", prompt]


def test_setup_failure_never_changes_launch_or_logs_exception_content(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(HARNESS_STDERR_ENABLED_ENV_VAR, "1")

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("synthetic private failure body")

    monkeypatch.setattr(bridge, "_write_json_file", fail)
    args = ["--model", "sonnet"]
    assert diagnostics.augment_claude_debug_args(args, bridge_dir) is args
    assert list(bridge_dir.iterdir()) == []
    assert "private failure body" not in caplog.text


def test_poll_exports_new_records_once_with_session_and_launch_identity(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    capture_file.write_text("first diagnostic\n")
    follower.poll("conv_first")
    follower.poll("conv_first")
    with capture_file.open("a") as handle:
        handle.write("second diagnostic\n")
    follower.poll("conv_rotated_session")
    follower.close("conv_rotated_session")

    events = _events(caplog)
    assert [event["text"] for event in events] == ["first diagnostic", "second diagnostic"]
    assert len({event["launch_id"] for event in events}) == 1
    assert all(event["source_kind"] == "claude_debug_log" for event in events)
    assert all(event["harness"] == "claude-native" for event in events)
    assert [record.session_id for record in caplog.records] == [
        "conv_first",
        "conv_rotated_session",
    ]
    assert "first diagnostic" in caplog.records[0].getMessage()
    assert events[-1]["offset"] == capture_file.stat().st_size


@pytest.mark.parametrize(
    ("diagnostic", "secret", "expected"),
    [
        (
            "failed token=synthetic-secret-value",
            "synthetic-secret-value",
            "failed token=[REDACTED]",
        ),
        ("ERROR failed: password hunter2", "hunter2", "ERROR failed: password [REDACTED]"),
        (
            "ERROR authentication failed: invalid api key a1b2c3d4e5f60718293a4b5c6d7e8f90",
            "a1b2c3d4e5f60718293a4b5c6d7e8f90",
            "ERROR authentication failed: invalid api key [REDACTED]",
        ),
    ],
)
def test_split_utf8_and_credentials_are_redacted_only_after_complete_record(
    capture_file: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: str,
    secret: str,
    expected: str,
) -> None:
    monkeypatch.setattr(diagnostics, "_READ_BYTES", 9)
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    raw = f"é🙂\x1b[31m {diagnostic}\x1b[0m\n".encode()
    capture_file.write_bytes(raw)
    follower.poll("conv_test")
    assert not _events(caplog)
    for _ in range(len(raw) // 9 + 1):
        follower.poll("conv_test")
    follower.close("conv_test")

    assert [event["text"] for event in _events(caplog)] == [f"é🙂 {expected}"]
    assert secret not in caplog.text
    for record in caplog.records:
        row = record_to_row(record, source="runner")
        assert secret not in json.dumps(row)
        assert row["attributes"]["text"] == f"é🙂 {expected}"


def test_credential_redaction_precedes_export_clipping(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    text = "context " * 10_000 + " Bearer " + "synthetic-token-marker" * 5_000 + "\n"
    capture_file.write_text(text)
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    for _ in range(4):
        follower.poll("conv_test")
    follower.close("conv_test")

    event = _events(caplog)[0]
    assert event["text"].endswith("Bearer [REDACTED]")
    assert len(event["text"].encode()) <= diagnostics.DIAGNOSTIC_TAIL_BYTES
    assert event["truncated"] is True
    assert event["bytes_omitted"] > 0
    assert "synthetic-token-marker" not in caplog.text


def test_poll_reads_at_most_one_64k_chunk(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    capture_file.write_bytes(b"ordinary record\n" * 20_000)
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")

    assert follower._offset == 64 * 1024
    assert len(_events(caplog)) == 1
    assert len(_events(caplog)[0]["text"].encode()) <= 64 * 1024
    follower.close("conv_test")


def test_oversized_record_is_dropped_with_counts_then_forwarding_recovers(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    oversized = b"x" * (diagnostics._MAX_RECORD_BYTES + 73) + b"\n"
    capture_file.write_bytes(oversized + b"recovered\n")
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    for _ in range(18):
        follower.poll("conv_test")
        assert len(follower._pending) <= diagnostics._MAX_RECORD_BYTES
    follower.close("conv_test")

    events = _events(caplog)
    assert sum(event["lines_omitted"] for event in events) == 1
    assert sum(event["bytes_omitted"] for event in events) == len(oversized)
    assert [event["text"] for event in events if event["text"]] == ["recovered"]


def test_rotation_drains_old_inode_before_new_file(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    capture_file.write_text("first\nold partial")
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    rotated = capture_file.with_name(capture_file.name + ".1")
    capture_file.rename(rotated)
    with rotated.open("a") as handle:
        handle.write(" completed\n")
    capture_file.write_text("replacement file with a larger size than the old cursor\n")
    follower.poll("conv_test")
    follower.poll("conv_test")
    follower.close("conv_test")

    assert [event["text"] for event in _events(caplog)] == [
        "first",
        "old partial completed",
        "replacement file with a larger size than the old cursor",
    ]
    assert sum(event["bytes_omitted"] for event in _events(caplog)) == 0


def test_truncation_discards_incomplete_old_record(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    capture_file.write_text("first\nincomplete old record")
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    capture_file.write_text("new\n")
    follower.poll("conv_test")
    follower.close("conv_test")

    assert [event["text"] for event in _events(caplog)] == ["first", "new"]


def test_new_launch_never_combines_or_exports_previous_pending_record(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    capture_file.write_text("old incomplete")
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    args = diagnostics.augment_claude_debug_args([], capture_file.parent)
    Path(args[-1]).write_text("new generation\n")
    follower.poll("conv_test")
    follower.close("conv_test")

    assert [event["text"] for event in _events(caplog)] == ["new generation"]


def test_missing_file_can_appear_later(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    capture_file.unlink()
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    capture_file.write_text("created later\n")
    follower.poll("conv_test")
    follower.close("conv_test")

    assert [event["text"] for event in _events(caplog)] == ["created later"]


def test_close_flushes_partial_record_once(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    capture_file.write_text("startup failed password=synthetic-password")
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    assert not _events(caplog)
    follower.close("conv_test")
    follower.close("conv_test")
    follower.poll("conv_test")

    assert [event["text"] for event in _events(caplog)] == ["startup failed password=[REDACTED]"]
    assert follower._fd is None


def test_close_bounds_final_drain_and_reports_unread_bytes(
    capture_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    capture_file.write_bytes(b"record\n" * 100_000)
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.close("conv_test")

    events = _events(caplog)
    assert len(events) == diagnostics._CLOSE_READS + 1
    assert events[-1]["offset"] == diagnostics._READ_BYTES * diagnostics._CLOSE_READS
    assert events[-1]["text"] == ""
    assert events[-1]["bytes_omitted"] > 0
    assert follower._fd is None


@pytest.mark.parametrize("polls_before_close", [0, 3])
@pytest.mark.parametrize("current_records", [1, 8])
def test_cold_follower_accounts_predecessor_without_reading_it(
    capture_file: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    polls_before_close: int,
    current_records: int,
) -> None:
    monkeypatch.setattr(diagnostics, "_READ_BYTES", 16)
    record = b"latest failure!\n"
    assert len(record) == diagnostics._READ_BYTES
    capture_file.write_bytes(record * current_records)
    predecessor = capture_file.with_name(capture_file.name + ".1")
    with predecessor.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024 + 73)
    predecessor_size = predecessor.stat().st_size
    current_inode = capture_file.stat().st_ino
    marker_inode = (capture_file.parent / diagnostics.CLAUDE_DEBUG_LOG_MARKER).stat().st_ino
    original_read = diagnostics.os.read
    reads: list[tuple[int, int]] = []

    def tracked_read(fd: int, size: int) -> bytes:
        inode = os.fstat(fd).st_ino
        if inode != marker_inode:
            reads.append((inode, size))
        return original_read(fd, size)

    monkeypatch.setattr(diagnostics.os, "read", tracked_read)
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    for _ in range(polls_before_close):
        follower.poll("conv_test")
    follower.close("conv_test")
    follower.close("conv_test")
    follower.poll("conv_test")

    read_budget = polls_before_close + diagnostics._CLOSE_READS
    events = _events(caplog)
    assert len(reads) == read_budget
    assert all(read == (current_inode, diagnostics._READ_BYTES) for read in reads)
    assert [event["text"] for event in events if event["text"]] == [
        record.decode().rstrip("\n")
    ] * min(current_records, read_budget)
    assert sum(event["bytes_omitted"] for event in events) == predecessor_size + max(
        0, current_records - read_budget
    ) * len(record)
    assert all(event["truncated"] for event in events if event["bytes_omitted"])
    assert follower._fd is None


def test_close_prioritizes_rotated_replacement_and_reports_skipped_old_bytes(
    capture_file: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "_READ_BYTES", 16)
    capture_file.write_bytes((b"x" * 15 + b"\n") * 5)
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    capture_file.rename(capture_file.with_name(capture_file.name + ".1"))
    capture_file.write_text("final failure\n")
    follower.close("conv_test")

    events = _events(caplog)
    assert events[-1]["text"] == "final failure"
    assert sum(event["bytes_omitted"] for event in events) == 64


def test_rotation_during_final_read_is_included_in_omission_counts(
    capture_file: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "_READ_BYTES", 16)
    capture_file.write_bytes((b"x" * 15 + b"\n") * 4)
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    original_read = diagnostics.os.read
    log_reads = 0

    def rotating_read(fd: int, size: int) -> bytes:
        nonlocal log_reads
        raw = original_read(fd, size)
        if size == 16:
            log_reads += 1
            if log_reads == diagnostics._CLOSE_READS:
                capture_file.rename(capture_file.with_name(capture_file.name + ".1"))
                capture_file.write_text("late failure\n")
        return raw

    monkeypatch.setattr(diagnostics.os, "read", rotating_read)
    follower.close("conv_test")

    assert log_reads == diagnostics._CLOSE_READS
    assert _events(caplog)[-1]["bytes_omitted"] == len("late failure\n")
    assert _events(caplog)[-1]["truncated"] is True


def test_disabling_capture_prevents_further_reads_and_closes_open_descriptor(
    capture_file: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture_file.write_text("pending record")
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    assert follower._fd is not None
    monkeypatch.delenv(HARNESS_STDERR_ENABLED_ENV_VAR)

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(diagnostics, "_open_directory", unexpected)
    follower.poll("conv_test")
    follower.close("conv_test")
    assert follower._fd is None
    assert not _events(caplog)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "hardlink"])
def test_non_regular_or_aliased_log_is_never_read(
    capture_file: Path, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    unrelated = capture_file.parent / "unrelated.log"
    unrelated.write_text("unrelated private contents\n")
    capture_file.unlink()
    if kind == "symlink":
        capture_file.symlink_to(unrelated)
    elif kind == "fifo":
        os.mkfifo(capture_file)
    elif kind == "hardlink":
        os.link(unrelated, capture_file)
    else:
        capture_file.mkdir()
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    follower.close("conv_test")
    assert not _events(caplog)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "hardlink", "foreign_owner"])
def test_unsafe_predecessor_is_ignored_without_losing_current_evidence(
    capture_file: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    capture_file.write_text("final failure\n")
    unrelated = capture_file.parent / "unrelated.log"
    unrelated.write_text("unrelated private contents\n")
    predecessor = capture_file.with_name(capture_file.name + ".1")
    if kind == "symlink":
        predecessor.symlink_to(unrelated)
    elif kind == "fifo":
        os.mkfifo(predecessor)
    elif kind == "hardlink":
        os.link(unrelated, predecessor)
    elif kind == "directory":
        predecessor.mkdir()
    else:
        predecessor.write_text("other owner's contents\n")
        predecessor_inode = predecessor.stat().st_ino
        original_fstat = diagnostics.os.fstat

        def foreign_owner_fstat(fd: int) -> os.stat_result:
            info = original_fstat(fd)
            if info.st_ino == predecessor_inode:
                values = list(info)
                values[4] = info.st_uid + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(diagnostics.os, "fstat", foreign_owner_fstat)

    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    follower.close("conv_test")

    events = _events(caplog)
    assert [event["text"] for event in events] == ["final failure"]
    assert sum(event["bytes_omitted"] for event in events) == 0


@pytest.mark.parametrize("kind", ["symlink", "traversal", "oversized", "malformed"])
def test_invalid_marker_cannot_select_other_files(
    capture_file: Path, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    capture_file.write_text("should not be exported\n")
    marker = capture_file.parent / diagnostics.CLAUDE_DEBUG_LOG_MARKER
    payload = marker.read_text()
    marker.unlink()
    if kind == "symlink":
        other = capture_file.parent / "other.json"
        other.write_text(payload)
        marker.symlink_to(other)
    elif kind == "traversal":
        marker.write_text(json.dumps({"launch_id": "a" * 32, "filename": "../unrelated.log"}))
    elif kind == "oversized":
        marker.write_text(" " * diagnostics._MARKER_BYTES + payload)
    else:
        marker.write_text("{")
    follower = diagnostics.ClaudeDebugLogFollower(capture_file.parent)
    follower.poll("conv_test")
    follower.close("conv_test")
    assert not _events(caplog)
