"""Tests for the Command Code (``cmd``) harness wrapper."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.inner import cmd_harness


def test_cmd_harness_builds_executor_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen = {}

    class FakeCmdExecutor:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(cmd_harness, "CmdExecutor", FakeCmdExecutor)
    monkeypatch.setenv("HARNESS_CMD_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("HARNESS_CMD_PATH", "/bin/cmd")
    monkeypatch.setenv("HARNESS_CMD_CWD", "/work")
    monkeypatch.setenv("HARNESS_CMD_AGENT_NAME", "cmd-coder")
    monkeypatch.setenv("HARNESS_CMD_SKILLS_FILTER", '["alpha"]')

    executor = cmd_harness._build_cmd_executor()

    assert isinstance(executor, FakeCmdExecutor)
    assert seen["model"] == "claude-sonnet-4-6"
    assert seen["cmd_path"] == "/bin/cmd"
    assert seen["cwd"] == "/work"
    assert seen["agent_name"] == "cmd-coder"
    assert seen["skills_filter"] == ["alpha"]
    # No HARNESS_CMD_MAX_TURNS set → the executor falls back to the default.
    assert seen["max_turns"] == cmd_harness._DEFAULT_MAX_TURNS


def test_cmd_harness_reads_max_turns_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``HARNESS_CMD_MAX_TURNS`` is wired to the CmdExecutor ``max_turns`` arg."""
    seen = {}

    class FakeCmdExecutor:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(cmd_harness, "CmdExecutor", FakeCmdExecutor)
    monkeypatch.setenv("HARNESS_CMD_PATH", "/bin/cmd")
    monkeypatch.setenv("HARNESS_CMD_MAX_TURNS", "25")

    cmd_harness._build_cmd_executor()

    assert seen["max_turns"] == 25


def test_cmd_harness_max_turns_bad_value_falls_back(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A non-integer / non-positive cap falls back to the default, never disables it."""
    seen = {}

    class FakeCmdExecutor:
        def __init__(self, **kwargs):
            seen.clear()
            seen.update(kwargs)

    monkeypatch.setattr(cmd_harness, "CmdExecutor", FakeCmdExecutor)
    monkeypatch.setenv("HARNESS_CMD_PATH", "/bin/cmd")

    for bad in ("not-a-number", "0", "-3", ""):
        monkeypatch.setenv("HARNESS_CMD_MAX_TURNS", bad)
        cmd_harness._build_cmd_executor()
        assert seen["max_turns"] == cmd_harness._DEFAULT_MAX_TURNS


def test_cmd_harness_emits_no_api_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Command Code owns its own auth (``cmd login``); no API key is threaded.

    Documents the gap vs. API-key-threading harnesses: there is no
    ``HARNESS_CMD_API_KEY`` env var, and the CmdExecutor constructor
    takes no ``api_key`` kwarg. The harness wrap must not invent one.
    """
    seen = {}

    class FakeCmdExecutor:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(cmd_harness, "CmdExecutor", FakeCmdExecutor)
    monkeypatch.setenv("HARNESS_CMD_API_KEY", "should-be-ignored")

    cmd_harness._build_cmd_executor()

    assert "api_key" not in seen
    assert "HARNESS_CMD_API_KEY" not in seen


def test_cmd_harness_create_app_returns_fastapi(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """``create_app()`` returns a FastAPI app (the runner's required entry point)."""

    class FakeCmdExecutor:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(cmd_harness, "CmdExecutor", FakeCmdExecutor)

    app = cmd_harness.create_app()

    assert isinstance(app, FastAPI)
