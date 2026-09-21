"""Unit tests for the small tmux helpers in ``omnigent.repl._tmux_pane``.

Complements ``test_tmux_pane.py``: version gating, launcher-argv resolution,
prefix-key listing, wrapper unwrapping, and the pane-option accessors. Every
``tmux`` invocation is stubbed, so no tmux binary is needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from omnigent.repl import _tmux_pane
from omnigent.repl._tmux_pane import (
    OPT_CONV_ID,
    _classify,
    _discover_split_bindings,
    _list_prefix_keys,
    _parse_bind_line,
    _resolve_omnigent_argv,
    _tmux_version_ok,
    _unwrap_existing_wrapper,
    _user_args_after_launcher,
    read_pane_option,
    update_conv_id,
)


def _stub_run(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", exc: Exception | None = None):
    """Replace ``subprocess.run``; returns the list of recorded argv lists."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if exc is not None:
            raise exc
        return SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ── tmux version gate ────────────────────────────────────────


@pytest.mark.parametrize(
    ("version_output", "expected"),
    [
        ("tmux 3.4\n", True),
        ("tmux 3.2a\n", True),
        ("tmux 3.2\n", True),
        ("tmux 3.1c\n", False),
        ("tmux 2.9\n", False),
        ("tmux 10.0\n", True),
        # Development builds carry a prefix or suffix on the number.
        ("tmux next-3.5\n", True),
        ("tmux 3.3-rc\n", True),
        # No parseable number, or an unexpected word count.
        ("tmux master\n", False),
        ("tmux\n", False),
        ("tmux 3.4 extra\n", False),
        ("", False),
    ],
)
def test_tmux_version_ok_parses_version_output(
    monkeypatch: pytest.MonkeyPatch, version_output: str, expected: bool
) -> None:
    calls = _stub_run(monkeypatch, stdout=version_output)

    assert _tmux_version_ok() is expected
    assert calls == [["tmux", "-V"]]


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "tmux"),
        FileNotFoundError("tmux"),
        PermissionError("tmux"),
    ],
)
def test_tmux_version_ok_is_false_when_tmux_cannot_run(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    _stub_run(monkeypatch, exc=exc)

    assert _tmux_version_ok() is False


# ── launcher argv resolution ─────────────────────────────────


def test_resolve_argv_reroutes_script_path_through_python_m(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["/repo/omnigent/cli.py", "run"])
    monkeypatch.setattr(sys, "executable", "/venv/bin/python")

    # A ``.py`` argv0 isn't executable; re-enter through ``python -m`` instead.
    assert _resolve_omnigent_argv() == ["/venv/bin/python", "-m", "omnigent.cli"]


def test_resolve_argv_falls_back_when_argv_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", [])
    monkeypatch.setattr(sys, "executable", "/venv/bin/python")

    assert _resolve_omnigent_argv() == ["/venv/bin/python", "-m", "omnigent.cli"]


def test_resolve_argv_makes_relative_path_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["./bin/omnigent", "run"])

    assert _resolve_omnigent_argv() == [os.path.abspath("./bin/omnigent")]


# ── launch argv normalization ────────────────────────────────


@pytest.mark.parametrize(
    ("launch_argv", "expected"),
    [
        (["/bin/omnigent", "run", "agent.yaml"], ["run", "agent.yaml"]),
        (["python", "-m", "omnigent.cli", "attach", "conv_1"], ["attach", "conv_1"]),
        (["python", "-m", "omnigent.cli", "-m", "omnigent.cli", "run"], ["run"]),
        (["omnigent", "--version"], []),
        ([], []),
    ],
)
def test_user_args_after_launcher(launch_argv: list[str], expected: list[str]) -> None:
    assert _user_args_after_launcher(launch_argv) == expected


# ── tmux key discovery ───────────────────────────────────────


def test_list_prefix_keys_returns_output_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, stdout="bind-key -T prefix a new-window\nbind-key x kill\n")

    assert _list_prefix_keys() == ["bind-key -T prefix a new-window", "bind-key x kill"]
    assert calls == [["tmux", "list-keys", "-T", "prefix"]]


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, "tmux"), FileNotFoundError()])
def test_list_prefix_keys_is_empty_when_tmux_fails(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    _stub_run(monkeypatch, exc=exc)

    assert _list_prefix_keys() == []


def test_parse_bind_line_skips_two_arg_and_unknown_flags() -> None:
    parsed = _parse_bind_line("bind-key -r -T prefix -N 'split it' -n % split-window -h")

    assert parsed == ("%", ["split-window", "-h"])


@pytest.mark.parametrize("line", ["", "   ", "bind-key", "bind-key -T prefix"])
def test_parse_bind_line_returns_none_without_key(line: str) -> None:
    assert _parse_bind_line(line) is None


def test_classify_returns_none_for_empty_command() -> None:
    assert _classify([]) is None


def test_discover_ignores_unparseable_and_unrelated_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        "not a binding",
        "bind-key -T prefix z resize-pane -Z",
        "bind-key -T prefix '\"' split-window",
    ]
    monkeypatch.setattr(_tmux_pane, "_list_prefix_keys", lambda: lines)

    bindings = _discover_split_bindings()

    assert [b.key for b in bindings] == ['"']


# ── wrapper unwrapping ───────────────────────────────────────


def _wrapper(original: str = "split-window -h") -> list[str]:
    return ["if-shell", "-F", _tmux_pane._WRAPPER_MARKER_FORMAT, "run-shell chooser", original]


def test_unwrap_recovers_original_command_tokens() -> None:
    assert _unwrap_existing_wrapper(_wrapper("split-window -h -c '#{pane_current_path}'")) == [
        "split-window",
        "-h",
        "-c",
        "#{pane_current_path}",
    ]


@pytest.mark.parametrize(
    "cmd_tokens",
    [
        # Wrong token count.
        _wrapper()[:-1],
        [*_wrapper(), "extra"],
        # Not an ``if-shell -F`` conditional.
        ["if-shell", "true", *_wrapper()[2:]],
        ["run-shell", "-F", *_wrapper()[2:]],
        # A user's own conditional, not ours.
        ["if-shell", "-F", "#{pane_in_mode}", "copy-mode", "split-window"],
        # Original command that cannot be re-tokenized.
        _wrapper("split-window -c 'unterminated"),
    ],
)
def test_unwrap_returns_none_for_foreign_or_malformed_commands(cmd_tokens: list[str]) -> None:
    assert _unwrap_existing_wrapper(cmd_tokens) is None


# ── pane option accessors ────────────────────────────────────


def test_update_conv_id_sets_option_on_current_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1,1,0")
    monkeypatch.setenv("TMUX_PANE", "%3")
    calls = _stub_run(monkeypatch)

    update_conv_id("conv_new")

    assert calls == [["tmux", "set-option", "-p", "-t", "%3", OPT_CONV_ID, "conv_new"]]


def test_update_conv_id_is_a_noop_outside_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    calls = _stub_run(monkeypatch)

    update_conv_id("conv_new")

    assert calls == []


def test_update_conv_id_is_a_noop_without_pane_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1,1,0")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    calls = _stub_run(monkeypatch)

    update_conv_id("conv_new")

    assert calls == []


def test_read_pane_option_returns_stripped_value(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, stdout="  conv_abc \n")

    assert read_pane_option("%1", OPT_CONV_ID) == "conv_abc"
    assert calls == [["tmux", "show-options", "-p", "-q", "-v", "-t", "%1", OPT_CONV_ID]]


def test_read_pane_option_returns_none_for_unset_option(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, stdout="\n")

    assert read_pane_option("%1", OPT_CONV_ID) is None


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, "tmux"), FileNotFoundError()])
def test_read_pane_option_returns_none_when_tmux_fails(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    _stub_run(monkeypatch, exc=exc)

    assert read_pane_option("%1", OPT_CONV_ID) is None
