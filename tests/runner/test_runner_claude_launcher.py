"""Tests that the runner's Claude terminal launch honours the launcher plugin hook.

The local-CLI launch already routes ``(command, args)`` through
:func:`omnigent.claude_launcher.resolve_claude_launch`. The runner's
auto-created Claude terminal must do the same, otherwise a wrapper selected via
``OMNIGENT_CLAUDE_LAUNCHER`` is silently bypassed for daemon/server-created
sessions.
"""

from __future__ import annotations

import importlib.metadata

from omnigent.claude_launcher import (
    CLAUDE_LAUNCHER_ENTRY_POINT_GROUP,
    CLAUDE_LAUNCHER_ENV_VAR,
    ClaudeLauncher,
)
from omnigent.runner.native.orchestration import _apply_claude_launcher


class _FakeEntryPoint:
    def __init__(self, name, cls):
        self.name = name
        self._cls = cls

    def load(self):
        return self._cls


class _WrapLauncher(ClaudeLauncher):
    def launch(self, command, args):
        return "wrapper", ["claude", "--", *args]


def _register(monkeypatch, *entry_points):
    real = importlib.metadata.entry_points

    def fake(*args, **kwargs):
        # Only the launcher group is faked; any other lookup keeps working.
        if kwargs.get("group") == CLAUDE_LAUNCHER_ENTRY_POINT_GROUP:
            return list(entry_points)
        return real(*args, **kwargs)

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)


def test_identity_without_launcher(monkeypatch):
    monkeypatch.delenv(CLAUDE_LAUNCHER_ENV_VAR, raising=False)
    assert _apply_claude_launcher("claude", ("--model", "x")) == ("claude", ["--model", "x"])


def test_runner_launch_uses_selected_launcher(monkeypatch):
    _register(monkeypatch, _FakeEntryPoint("wrap", _WrapLauncher))
    monkeypatch.setenv(CLAUDE_LAUNCHER_ENV_VAR, "wrap")
    assert _apply_claude_launcher("claude", ("--mcp-config", "{}")) == (
        "wrapper",
        ["claude", "--", "--mcp-config", "{}"],
    )


def test_config_resolved_command_is_passed_to_launcher(monkeypatch):
    seen = {}

    class _Record(ClaudeLauncher):
        def launch(self, command, args):
            seen["command"] = command
            return command, args

    _register(monkeypatch, _FakeEntryPoint("rec", _Record))
    monkeypatch.setenv(CLAUDE_LAUNCHER_ENV_VAR, "rec")
    _apply_claude_launcher("/opt/custom/claude", ())
    assert seen["command"] == "/opt/custom/claude"


def test_unknown_launcher_falls_back_to_resolved_command(monkeypatch):
    _register(monkeypatch)
    monkeypatch.setenv(CLAUDE_LAUNCHER_ENV_VAR, "missing")
    assert _apply_claude_launcher("/opt/custom/claude", ("-p",)) == ("/opt/custom/claude", ["-p"])


def test_missing_wrapper_is_logged(monkeypatch, caplog):
    class _Missing(ClaudeLauncher):
        def launch(self, command, args):
            return "definitely-not-on-path-wrapper", [command, *args]

    _register(monkeypatch, _FakeEntryPoint("missing", _Missing))
    monkeypatch.setenv(CLAUDE_LAUNCHER_ENV_VAR, "missing")
    with caplog.at_level("ERROR"):
        command, _ = _apply_claude_launcher("claude", ())
    assert command == "definitely-not-on-path-wrapper"
    assert "not on PATH" in caplog.text
