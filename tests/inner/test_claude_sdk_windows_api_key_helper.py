"""Regression tests for Windows-safe Claude SDK static-key apiKeyHelper.

Tests are offline: no network, real credentials, or Claude/Omnigent inference.
Every token below is a dummy string. Actual execution results belong in the
pull request test report rather than this module's source text.

What is covered:

* Recognition of Omnigent's exact ``printf %s <shlex.quote(key)>`` form,
  including tokens with shell-special characters, and rejection of anything
  else (dynamic commands, shell-expanding look-alikes, trailing syntax).
* POSIX: helper command and child env are returned unchanged.
* Windows (simulated via ``is_windows=True``): the helper is rewritten to an
  interpreter-based command that contains no key, the key moves into the
  child env, the ``--settings`` JSON carries no key, dynamic commands are
  untouched (refresh preserved), unsafe interpreter paths fall back.
* Real execution of the rewritten helper under POSIX ``sh`` (runs on POSIX
  only) and under ``cmd.exe /d /s /c`` (runs on Windows only).

Validation limit: the cmd.exe execution test only runs on a Windows host, and
none of these tests exercise the actual Claude Code CLI's apiKeyHelper
launcher (its shell selection on Windows is not verifiable from this repo).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

import pytest

from omnigent.inner import claude_sdk_executor as ex

_ENV = ex._STATIC_API_KEY_HELPER_SECRET_ENV
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

# Tokens safe to round-trip through a real child process's stdout on any
# platform (no newline -> no CRLF translation; ASCII -> no codepage issues).
EXEC_TOKENS = [t for t in DUMMY_TOKENS if "\n" not in t and t.isascii()]


def _static_cmd(token: str) -> str:
    """Build the command exactly as Omnigent's producers do."""
    return f"printf %s {shlex.quote(token)}"


# ── recognition ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", DUMMY_TOKENS)
def test_static_printf_secret_round_trips(token: str) -> None:
    assert ex._static_printf_secret(_static_cmd(token)) == token


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


@pytest.mark.parametrize("token", DUMMY_TOKENS)
def test_posix_static_helper_unchanged(token: str) -> None:
    env: dict[str, str] = {}
    command = _static_cmd(token)
    helper, static_key = ex._platform_safe_api_key_helper(
        command, env, is_windows=False, python_executable="/usr/bin/python3"
    )
    assert helper == command
    assert static_key is None
    assert env == {}


def test_posix_none_helper_unchanged() -> None:
    env: dict[str, str] = {}
    assert ex._platform_safe_api_key_helper(None, env, is_windows=False) == (None, None)
    assert env == {}


# ── Windows (simulated): rewrite ─────────────────────────────────────────


@pytest.mark.parametrize("token", DUMMY_TOKENS)
def test_windows_static_helper_rewritten_without_key(token: str) -> None:
    env: dict[str, str] = {}
    helper, static_key = ex._platform_safe_api_key_helper(
        _static_cmd(token), env, is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    assert helper is not None
    assert static_key == token
    assert env == {_ENV: token}
    # The key never appears in the command (which becomes CLI argv via --settings).
    assert token not in helper
    assert "dummy" not in helper
    assert helper == (
        f'"{_FAKE_WIN_PYTHON}" -I -S -c '
        '"import os,sys;sys.stdout.write(os.environ[sys.argv[1]])" '
        f"{_ENV}"
    )
    # Nothing cmd.exe or POSIX sh would reinterpret inside the double quotes.
    for ch in ("'", "%", "!", "$", "`", "^", "&", "|", "<", ">"):
        assert ch not in helper


@pytest.mark.parametrize("token", DUMMY_TOKENS)
def test_windows_settings_payload_has_no_key(token: str) -> None:
    env: dict[str, str] = {}
    helper, _ = ex._platform_safe_api_key_helper(
        _static_cmd(token), env, is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    payload = ex._claude_settings_payload(helper, {})
    assert payload is not None
    assert "dummy" not in payload
    assert "apiKeyHelper" in payload


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
    env: dict[str, str] = {}
    helper, static_key = ex._platform_safe_api_key_helper(
        command, env, is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    assert helper == command
    assert static_key is None
    assert env == {}


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
    env: dict[str, str] = {}
    command = _static_cmd(token)
    with caplog.at_level("WARNING"):
        helper, static_key = ex._platform_safe_api_key_helper(
            command, env, is_windows=True, python_executable=python_path
        )
    assert helper == command
    assert static_key is None
    assert env == {}
    assert token not in caplog.text


def test_gateway_listing_uses_static_key_not_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the Windows path the listing must not shell out to the env-reading helper."""
    seen = {}

    def _fake_listing(provider: object) -> object:
        seen["api_key"] = getattr(provider, "api_key", None)
        seen["auth_command"] = getattr(provider, "auth_command", None)
        raise RuntimeError("offline test: no listing")

    monkeypatch.setattr(ex.model_catalog, "listing_for_provider", _fake_listing)
    vocab = ex._gateway_model_vocabulary(
        "https://gateway.invalid/anthropic", "unused-helper", "dummy-listing-key"
    )
    assert vocab == ex._EMPTY_GATEWAY_VOCABULARY
    assert seen == {"api_key": "dummy-listing-key", "auth_command": None}


def test_gateway_listing_default_still_uses_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def _fake_listing(provider: object) -> object:
        seen["api_key"] = getattr(provider, "api_key", None)
        seen["auth_command"] = getattr(provider, "auth_command", None)
        raise RuntimeError("offline test: no listing")

    monkeypatch.setattr(ex.model_catalog, "listing_for_provider", _fake_listing)
    ex._gateway_model_vocabulary("https://gateway.invalid/anthropic", "my-cli print-token")
    assert seen == {"api_key": None, "auth_command": "my-cli print-token"}


# ── real execution of the rewritten helper (offline, local interpreter) ──


def _helper_for_this_interpreter(token: str) -> tuple[str, dict[str, str]]:
    helper = ex._windows_static_key_helper_command(sys.executable)
    if helper is None:
        pytest.skip("sys.executable path contains characters the helper rejects")
    child_env = dict(os.environ)
    child_env[_ENV] = token
    return helper, child_env


@pytest.mark.skipif(os.name == "nt", reason="POSIX sh execution test")
@pytest.mark.parametrize("token", EXEC_TOKENS)
def test_rewritten_helper_prints_exact_key_under_posix_sh(token: str) -> None:
    helper, child_env = _helper_for_this_interpreter(token)
    sh = shutil.which("sh") or "/bin/sh"
    result = subprocess.run(
        [sh, "-c", helper], env=child_env, capture_output=True, timeout=30, check=True
    )
    assert result.stdout.decode("utf-8") == token


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe execution test (Windows only)")
@pytest.mark.parametrize("token", EXEC_TOKENS)
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


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe execution test (Windows only)")
def test_original_printf_helper_is_not_cmd_exe_safe() -> None:
    """Documents the pre-fix hazard: cmd.exe cannot reproduce the key from printf."""
    token = "dummy'quoted token"
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    result = subprocess.run(
        f'"{comspec}" /d /s /c "{_static_cmd(token)}"',
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0 or result.stdout.decode("utf-8", "replace") != token


@pytest.mark.skipif(os.name == "nt", reason="POSIX sh execution test")
def test_rewritten_helper_fails_loudly_without_env_under_posix_sh() -> None:
    helper, child_env = _helper_for_this_interpreter("unused")
    child_env.pop(_ENV, None)
    sh = shutil.which("sh") or "/bin/sh"
    result = subprocess.run([sh, "-c", helper], env=child_env, capture_output=True, timeout=30)
    assert result.returncode != 0
    assert result.stdout == b""


# -- security boundaries: reserved env, malformed tokens, per-call isolation --


def test_windows_reserved_env_collision_is_not_overwritten(caplog: pytest.LogCaptureFixture) -> None:
    env = {_ENV: "different-dummy-token"}
    command = _static_cmd("desired-dummy-token")
    with caplog.at_level("WARNING"):
        helper, static_key = ex._platform_safe_api_key_helper(
            command, env, is_windows=True, python_executable=_FAKE_WIN_PYTHON
        )
    assert (helper, static_key) == (command, None)
    assert env == {_ENV: "different-dummy-token"}
    assert "desired-dummy-token" not in caplog.text
    assert "different-dummy-token" not in caplog.text


def test_windows_reserved_env_empty_collision_is_not_overwritten() -> None:
    env = {_ENV: ""}
    command = _static_cmd("desired-dummy-token")
    helper, static_key = ex._platform_safe_api_key_helper(
        command, env, is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    assert (helper, static_key) == (command, None)
    assert env == {_ENV: ""}


def test_windows_reserved_env_same_key_is_idempotent() -> None:
    token = "same-dummy-token"
    env = {_ENV: token}
    helper, static_key = ex._platform_safe_api_key_helper(
        _static_cmd(token), env, is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    assert helper is not None and helper != _static_cmd(token)
    assert static_key == token
    assert env == {_ENV: token}


def test_windows_nul_key_falls_back_without_env_mutation(caplog: pytest.LogCaptureFixture) -> None:
    command = _static_cmd("dummy\x00value")
    env = {}
    with caplog.at_level("WARNING"):
        helper, static_key = ex._platform_safe_api_key_helper(
            command, env, is_windows=True, python_executable=_FAKE_WIN_PYTHON
        )
    assert (helper, static_key) == (command, None)
    assert env == {}
    assert "dummy" not in caplog.text


def test_windows_two_calls_do_not_share_child_env() -> None:
    first, second = {}, {}
    helper1, key1 = ex._platform_safe_api_key_helper(
        _static_cmd("first-dummy"), first, is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    helper2, key2 = ex._platform_safe_api_key_helper(
        _static_cmd("second-dummy"), second, is_windows=True, python_executable=_FAKE_WIN_PYTHON
    )
    assert helper1 == helper2
    assert key1 == first[_ENV] == "first-dummy"
    assert key2 == second[_ENV] == "second-dummy"
    assert first != second
