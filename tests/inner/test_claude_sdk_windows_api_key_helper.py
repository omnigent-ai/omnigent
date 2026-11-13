"""Regression tests for Windows-safe Claude SDK static-key apiKeyHelper.

Tests are offline: no network, real credentials, or Claude/Omnigent inference.
Every token below is a dummy string.

What is covered:

* Recognition of Omnigent's exact ``printf %s <shlex.quote(key)>`` form,
  including tokens with shell-special characters, and rejection of anything
  else (dynamic commands, shell-expanding look-alikes, trailing syntax).
* POSIX: helper command is returned unchanged.
* Windows (simulated via ``is_windows=True``): the helper is rewritten to an
  interpreter-based command with a shell-safe encoded key operand. The
  credential stays in settings instead of entering the child environment.
  Dynamic commands are untouched; unsafe interpreter paths fall back.
* Real execution of the rewritten helper under POSIX ``sh`` (runs on POSIX
  only) and under ``cmd.exe /d /s /c`` (runs on Windows only).

Validation limit: the cmd.exe execution test only runs on a Windows host, and
none of these tests exercise the actual Claude Code CLI's apiKeyHelper
launcher (its shell selection on Windows is not verifiable from this repo).
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.inner import claude_sdk_executor as ex

_FAKE_WIN_PYTHON = r"C:\Program Files\Python312\python.exe"

# Dummy tokens only. Each one exercises characters that are special to POSIX
# sh, cmd.exe, or both.
DUMMY_TOKENS = [
    "sk-ant-dummy-0000000000",
    "dummy'single'quote",
    'dummy"double"quote',
    "dummy $HOME `id` $(id) ${X}",
    "dummy %PATH% %%x !VAR! ^caret &amp |pipe <lt >gt (paren)",
    "dummy back\\slash and \\\\double",
    "dummy space ; semicolon * glob ? q ~ tilde # hash",
    "dummy=equals+plus/slash:colon,comma@at",
    "dummy\ttab",
    "dummy\nnewline",
    "dummy-\u00fcn\u00efcode",
]


def _static_cmd(token: str) -> str:
    """Build the command exactly as Omnigent's producers do."""
    return f"printf %s {shlex.quote(token)}"


# ── recognition ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", DUMMY_TOKENS)
def test_static_printf_secret_matches_provider_producer(token: str) -> None:
    from omnigent.onboarding.provider_config import FamilyConfig
    from omnigent.runtime.workflow import _provider_auth_command

    family = FamilyConfig(base_url="https://gateway.invalid/anthropic", api_key=token)
    assert ex._static_printf_secret(_provider_auth_command(family)) == token


@pytest.mark.parametrize(
    "command",
    [
        None,
        "",
        "databricks auth token --host https://example.invalid",
        'printf %s "$DUMMY_TOKEN"',  # a shell would expand this
        "printf %s $DUMMY_TOKEN",
        "printf %s dummy; echo extra",
        "printf %s dummy && echo extra",
        "printf '%s' dummy",
        "printf %s",
        "printf %s ''",
        "printf %s 'unterminated",
        "echo dummy",
        "  printf %s dummy",
    ],
)
def test_static_printf_secret_rejects_other_commands(command: str | None) -> None:
    assert ex._static_printf_secret(command) is None


# ── POSIX: unchanged ─────────────────────────────────────────────────────


@pytest.mark.parametrize("command", [None, _static_cmd("dummy'quoted %value")])
def test_posix_helper_unchanged(command: str | None) -> None:
    helper, static_key = ex._platform_safe_api_key_helper(
        command, is_windows=False, python_executable="/usr/bin/python3"
    )
    assert helper == command
    assert static_key is None


# ── Windows (simulated): rewrite ─────────────────────────────────────────


@pytest.mark.parametrize("token", DUMMY_TOKENS)
def test_windows_static_helper_uses_shell_safe_settings_operand(token: str) -> None:
    helper, static_key = ex._platform_safe_api_key_helper(
        _static_cmd(token), is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    assert helper is not None
    assert static_key == token
    encoded = helper.rsplit(" ", 1)[1]
    assert base64.b64decode(encoded, validate=True).decode() == token
    # Encoding only protects shell syntax. Settings still contain the credential.
    for ch in ("'", "%", "!", "$", "`", "^", "&", "|", "<", ">"):
        assert ch not in helper
    payload = ex._claude_settings_payload(helper, {})
    assert payload is not None
    assert encoded in payload


@pytest.mark.parametrize(
    "command",
    [
        "databricks auth token --host https://example.invalid --profile dummy",
        "my-cli print-token",
        'printf %s "$DUMMY_TOKEN"',
    ],
)
def test_windows_dynamic_helper_unchanged(command: str) -> None:
    """Dynamic commands keep running each TTL (refresh behavior preserved)."""
    helper, static_key = ex._platform_safe_api_key_helper(
        command, is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    assert helper == command
    assert static_key is None


@pytest.mark.parametrize(
    "python_path",
    [
        "",
        r"C:\Users\a%TEMP%b\python.exe",
        r"C:\Users\a!b\python.exe",
        r"C:\Users\a$b\python.exe",
        'C:\\Users\\a"b\\python.exe',
        r"\\server\share\python.exe",
        "C:\\Users\\a`b\\python.exe",
    ],
)
def test_windows_unsafe_python_path_falls_back(
    python_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    token = "dummy-fallback-token-1234"
    command = _static_cmd(token)
    with caplog.at_level("WARNING"):
        helper, static_key = ex._platform_safe_api_key_helper(
            command, is_windows=True, python_executable=python_path
        )
    assert helper == command
    assert static_key is None
    assert token not in caplog.text


# ── real execution of the rewritten helper (offline, local interpreter) ──


def _helper_for_this_interpreter(token: str) -> tuple[str, dict[str, str]]:
    helper = ex._windows_static_key_helper_command(sys.executable, token)
    if helper is None:
        pytest.skip("sys.executable path contains characters the helper rejects")
    child_env = dict(os.environ)
    return helper, child_env


@pytest.mark.skipif(os.name == "nt", reason="POSIX sh execution test")
@pytest.mark.parametrize("token", DUMMY_TOKENS)
def test_rewritten_helper_prints_exact_key_under_posix_sh(token: str) -> None:
    helper, child_env = _helper_for_this_interpreter(token)
    sh = shutil.which("sh") or "/bin/sh"
    result = subprocess.run(
        [sh, "-c", helper], env=child_env, capture_output=True, timeout=30, check=True
    )
    assert result.stdout.decode("utf-8") == token


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe execution test (Windows only)")
@pytest.mark.parametrize("token", DUMMY_TOKENS)
def test_rewritten_helper_prints_exact_key_under_cmd_exe(token: str) -> None:
    helper, child_env = _helper_for_this_interpreter(token)
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    # A raw command-line string (not a list) so Python does not re-quote it;
    # mirrors Node's ``cmd.exe /d /s /c "<command>"`` shell invocation.
    result = subprocess.run(
        f'"{comspec}" /d /s /c "{helper}"',
        env=child_env,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.decode("utf-8") == token


@pytest.mark.skipif(os.name != "nt", reason="native Windows interpreter-path test")
def test_windows_helper_uses_interpreter_path_with_spaces(tmp_path: Path) -> None:
    """Execute through cmd.exe with a real interpreter in a spaced path."""
    venv = tmp_path / "helper interpreter"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    helper = ex._windows_static_key_helper_command(
        str(venv / "Scripts" / "python.exe"), "dummy-quoted-path-key"
    )
    assert helper is not None
    child_env = dict(os.environ)
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    result = subprocess.run(
        f'"{comspec}" /d /s /c "{helper}"',
        env=child_env,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert result.stdout == b"dummy-quoted-path-key"
