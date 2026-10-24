"""Tests for :mod:`omnigent.cli_invocation`.

Followup hints must name the configured wrapper (``isaac omni stop``) when
``OMNIGENT_WRAPPER_COMMAND`` is set, and fall back to the naked binary token
otherwise so default output is unchanged. A hint's argument is quoted for
POSIX shells only: Windows shells do not glob, and cmd.exe would pass the
quotes on to the CLI.
"""

from __future__ import annotations

import pytest

import omnigent.cli_invocation as cli_invocation_module
from omnigent.cli_invocation import (
    DEFAULT_CLI_NAME,
    WRAPPER_COMMAND_ENV,
    cli_invocation,
    quote_hint_argument,
)


def test_defaults_to_omnigent_when_wrapper_unset() -> None:
    assert cli_invocation(env={}) == "omnigent"
    assert DEFAULT_CLI_NAME == "omnigent"


def test_preserves_omni_alias_when_wrapper_unset() -> None:
    # A hint that spells the short ``omni`` alias keeps it verbatim.
    assert cli_invocation(name="omni", env={}) == "omni"


def test_wrapper_command_overrides_both_names() -> None:
    env = {WRAPPER_COMMAND_ENV: "isaac omni"}
    assert cli_invocation(env=env) == "isaac omni"
    assert cli_invocation(name="omni", env=env) == "isaac omni"


def test_blank_wrapper_command_is_ignored() -> None:
    assert cli_invocation(env={WRAPPER_COMMAND_ENV: "   "}) == "omnigent"
    assert cli_invocation(env={WRAPPER_COMMAND_ENV: ""}) == "omnigent"


def test_wrapper_command_is_stripped() -> None:
    assert cli_invocation(env={WRAPPER_COMMAND_ENV: "  isaac omni  "}) == "isaac omni"


def test_reads_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WRAPPER_COMMAND_ENV, "isaac omni")
    assert cli_invocation() == "isaac omni"
    monkeypatch.delenv(WRAPPER_COMMAND_ENV, raising=False)
    assert cli_invocation() == "omnigent"


def test_rendered_hint_names_the_wrapper() -> None:
    env = {WRAPPER_COMMAND_ENV: "isaac omni"}
    assert f"Run `{cli_invocation(env=env)} stop`" == "Run `isaac omni stop`"
    assert f"Run `{cli_invocation(env={})} stop`" == "Run `omnigent stop`"


def test_quote_hint_argument_quotes_glob_characters_for_posix_shells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_invocation_module, "IS_WINDOWS", False)
    # zsh (and bash -O failglob) refuse an unmatched `?` glob, so the URL is quoted.
    assert (
        quote_hint_argument("https://ws.example.com/omnigent?o=123")
        == "'https://ws.example.com/omnigent?o=123'"
    )
    # Arguments without shell metacharacters stay bare so hints read naturally.
    assert quote_hint_argument("http://127.0.0.1:6767") == "http://127.0.0.1:6767"


def test_quote_hint_argument_keeps_windows_arguments_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cmd.exe and PowerShell do not glob `?`, and cmd.exe would pass the quotes on.
    monkeypatch.setattr(cli_invocation_module, "IS_WINDOWS", True)
    assert (
        quote_hint_argument("https://ws.example.com/omnigent?o=123")
        == "https://ws.example.com/omnigent?o=123"
    )
