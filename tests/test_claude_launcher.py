"""Unit tests for :mod:`omnigent.claude_launcher`."""

from __future__ import annotations

import sys
import types

import pytest

from omnigent.claude_launcher import CLAUDE_LAUNCHER_ENV_VAR, resolve_claude_launch


def _register_plugin(monkeypatch, value, *, attr="launch", module="fake_launcher_mod"):
    """Inject a fake plugin module and point the env var at it."""
    mod = types.ModuleType(module)
    setattr(mod, attr, value)
    monkeypatch.setitem(sys.modules, module, mod)
    monkeypatch.setenv(CLAUDE_LAUNCHER_ENV_VAR, f"{module}:{attr}")


def test_identity_when_env_unset(monkeypatch):
    monkeypatch.delenv(CLAUDE_LAUNCHER_ENV_VAR, raising=False)
    assert resolve_claude_launch("claude", ["--foo", "bar"]) == ("claude", ["--foo", "bar"])


def test_identity_returns_fresh_list(monkeypatch):
    monkeypatch.delenv(CLAUDE_LAUNCHER_ENV_VAR, raising=False)
    original = ["--foo"]
    _, args = resolve_claude_launch("claude", original)
    assert args == original
    assert args is not original


def test_plugin_wraps_command(monkeypatch):
    def wrap(command, args):
        return "isaac", ["claude", "--omni-internal", "--", *args]

    _register_plugin(monkeypatch, wrap)
    command, args = resolve_claude_launch("claude", ["--mcp-config", "{}"])
    assert command == "isaac"
    assert args == ["claude", "--omni-internal", "--", "--mcp-config", "{}"]


def test_plugin_receives_default_command_and_args(monkeypatch):
    seen = {}

    def wrap(command, args):
        seen["command"], seen["args"] = command, args
        return command, args

    _register_plugin(monkeypatch, wrap)
    resolve_claude_launch("claude", ["--x"])
    assert seen == {"command": "claude", "args": ["--x"]}


@pytest.mark.parametrize("spec", ["nocolon", ":nomod", "mod:", "", "   "])
def test_malformed_spec_falls_back(monkeypatch, spec):
    monkeypatch.setenv(CLAUDE_LAUNCHER_ENV_VAR, spec)
    assert resolve_claude_launch("claude", ["--x"]) == ("claude", ["--x"])


def test_import_error_falls_back(monkeypatch):
    monkeypatch.setenv(CLAUDE_LAUNCHER_ENV_VAR, "no_such_module_xyz:launch")
    assert resolve_claude_launch("claude", ["--x"]) == ("claude", ["--x"])


def test_missing_attr_falls_back(monkeypatch):
    mod = types.ModuleType("fake_launcher_mod2")
    monkeypatch.setitem(sys.modules, "fake_launcher_mod2", mod)
    monkeypatch.setenv(CLAUDE_LAUNCHER_ENV_VAR, "fake_launcher_mod2:missing")
    assert resolve_claude_launch("claude", ["--x"]) == ("claude", ["--x"])


def test_not_callable_falls_back(monkeypatch):
    _register_plugin(monkeypatch, "not-callable")
    assert resolve_claude_launch("claude", ["--x"]) == ("claude", ["--x"])


def test_plugin_raises_falls_back(monkeypatch):
    def boom(command, args):
        raise RuntimeError("boom")

    _register_plugin(monkeypatch, boom)
    assert resolve_claude_launch("claude", ["--x"]) == ("claude", ["--x"])


@pytest.mark.parametrize(
    "bad",
    [
        "notatuple",
        ("only-one",),
        ("", ["x"]),
        ("cmd", "notalist"),
        ("cmd", [1, 2]),
        (123, ["x"]),
    ],
)
def test_malformed_return_falls_back(monkeypatch, bad):
    _register_plugin(monkeypatch, lambda command, args: bad)
    assert resolve_claude_launch("claude", ["--x"]) == ("claude", ["--x"])
