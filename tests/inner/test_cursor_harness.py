"""Tests for the Cursor harness wrapper."""

from __future__ import annotations

import json

from omnigent.inner import cursor_harness


def test_cursor_harness_builds_executor_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen = {}

    class FakeCursorExecutor:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(cursor_harness, "CursorExecutor", FakeCursorExecutor)
    monkeypatch.setenv("HARNESS_CURSOR_MODEL", "gpt-5")
    monkeypatch.setenv("HARNESS_CURSOR_CWD", "/repo")
    monkeypatch.setenv("HARNESS_CURSOR_PATH", "/bin/cursor-agent")
    monkeypatch.setenv("HARNESS_CURSOR_AGENT_NAME", "cursor-coder")
    monkeypatch.setenv(
        "HARNESS_CURSOR_OS_ENV",
        json.dumps({"type": "caller_process", "sandbox": {"type": "none"}}),
    )

    executor = cursor_harness._build_cursor_executor()

    assert isinstance(executor, FakeCursorExecutor)
    assert seen["model"] == "gpt-5"
    assert seen["cwd"] == "/repo"
    assert seen["cursor_path"] == "/bin/cursor-agent"
    assert seen["agent_name"] == "cursor-coder"
    assert seen["os_env"].type == "caller_process"
