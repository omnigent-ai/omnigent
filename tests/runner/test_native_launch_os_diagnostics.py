"""Launch errors retain queryable OS facts without adding private paths to attributes."""

import errno
import json
import logging

import pytest

from omnigent.runner.native import orchestration


@pytest.mark.parametrize("wrapped", [False, True])
def test_native_launch_error_has_os_facts(wrapped: bool, caplog: pytest.LogCaptureFixture) -> None:
    cause = FileNotFoundError(errno.ENOENT, "synthetic-private-detail", "/synthetic-private-path")
    error: BaseException = cause
    if wrapped:
        error = RuntimeError("synthetic wrapper")
        error.__context__ = cause
    with caplog.at_level(logging.WARNING, logger=orchestration.__name__):
        payload = orchestration._native_terminal_start_error_payload(
            error, "Codex", session_id="session-test"
        )
    records = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "native_terminal_start_failed"
    ]
    assert len(records) == 1
    attrs = records[0].attributes
    assert attrs["native_launch_os_errno"] == errno.ENOENT
    assert attrs["native_launch_os_errno_name"] == "ENOENT"
    assert attrs["exception_context_type"] == ("FileNotFoundError" if wrapped else None)
    assert attrs["error_id"] == payload["error_id"]
    assert "synthetic-private" not in json.dumps(attrs)
