"""Offline helper tests; shell cases do not exercise Claude Code's launcher."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner import claude_sdk_executor as ex
from omnigent.inner import claude_sdk_key_file as key_files
from omnigent.inner.claude_sdk_key_file import PrivateStaticKeyFile

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


def test_windows_static_helper_is_deferred_until_client_creation() -> None:
    token = "dummy-key"
    assert ex._platform_safe_api_key_helper(
        _static_cmd(token), is_windows=True, python_executable=_FAKE_WIN_PYTHON
    ) == (None, token)


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


@pytest.mark.asyncio
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
async def test_windows_unsafe_python_path_fails_before_settings_or_client_creation(
    python_path: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "dummy-unsafe-interpreter-token"
    executor = ex.ClaudeSDKExecutor(api_key_helper=_static_cmd(token), model="dummy-model")
    sdk = SimpleNamespace(ClaudeAgentOptions=Mock(), ClaudeSDKClient=Mock())
    create_key = Mock()
    monkeypatch.setattr(ex, "_ensure_sdk", lambda: sdk)
    monkeypatch.setattr(ex, "IS_WINDOWS", True)
    monkeypatch.setattr(ex.sys, "executable", python_path)
    monkeypatch.setattr(ex, "PrivateStaticKeyFile", create_key)
    try:
        with pytest.raises(RuntimeError, match="safe Python interpreter") as error:
            [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "hello"}], [], ""
                )
            ]
    finally:
        await executor.close()
    sdk.ClaudeAgentOptions.assert_not_called()
    sdk.ClaudeSDKClient.assert_not_called()
    create_key.assert_not_called()
    assert token not in str(error.value)
    assert token not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["constructor", "connect", "timeout", "cancel"])
async def test_failed_client_creation_removes_its_static_key_file(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(key_files.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ex, "_CONNECT_TIMEOUT_SECONDS", 0.1)
    paths: list[Path] = []
    connecting = asyncio.Event()

    class Client:
        def __init__(self, options: SimpleNamespace) -> None:
            operand = json.loads(options.settings)["apiKeyHelper"].rsplit(" ", 1)[1]
            path = Path(base64.b64decode(operand, validate=True).decode("utf-8"))
            assert path.read_bytes() == b"dummy-lifecycle-key"
            paths.append(path)
            if outcome == "constructor":
                raise RuntimeError("constructor failed")

        async def connect(self) -> None:
            connecting.set()
            if outcome == "connect":
                raise RuntimeError("connect failed")
            await asyncio.Event().wait()

    executor = ex.ClaudeSDKExecutor()

    async def force_close(_client: Client) -> None:
        assert paths[0].exists(), "keep the key until the failed CLI has stopped"

    close = AsyncMock(side_effect=force_close)
    monkeypatch.setattr(executor, "_force_close_client", close)
    options = SimpleNamespace(stderr=None, settings=None)
    create = asyncio.create_task(
        executor._get_or_create_client(
            SimpleNamespace(ClaudeSDKClient=Client),
            session_key="failed",
            options=options,
            model=None,
            static_api_key="dummy-lifecycle-key",
        )
    )
    if outcome == "cancel":
        await asyncio.wait_for(connecting.wait(), timeout=5)
        create.cancel()
    expected = (
        asyncio.CancelledError
        if outcome == "cancel"
        else TimeoutError
        if outcome == "timeout"
        else RuntimeError
    )
    with pytest.raises(expected):
        await create
    assert paths and not paths[0].parent.exists()
    assert not list(tmp_path.glob("omnigent-claude-key-*"))
    assert not executor._clients
    assert options.stderr is None
    assert close.await_count == int(outcome != "constructor")


@pytest.mark.asyncio
@pytest.mark.parametrize("close_mode", ["disconnect", "force", "evict"])
async def test_static_key_file_lives_until_its_cached_client_closes(
    close_mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(key_files.tempfile, "gettempdir", lambda: str(tmp_path))
    constructed: list[object] = []

    class Client:
        def __init__(self, _options: SimpleNamespace) -> None:
            constructed.append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            assert key_file.path.exists()

    executor = ex.ClaudeSDKExecutor()
    sdk = SimpleNamespace(ClaudeSDKClient=Client)
    kwargs = {"session_key": "cached", "model": None, "static_api_key": "dummy-cached-key"}
    first = await executor._get_or_create_client(
        sdk, options=SimpleNamespace(stderr=None, settings='{"existing":true}'), **kwargs
    )
    state = executor._clients["cached"]
    key_file = state.static_key_file
    assert key_file is not None
    try:
        reused = await executor._get_or_create_client(
            sdk, options=SimpleNamespace(stderr=None, settings=None), **kwargs
        )
        assert reused is first and constructed == [first]
        assert key_file.path.read_bytes() == b"dummy-cached-key"
        assert len(list(tmp_path.glob("omnigent-claude-key-*"))) == 1

        async def force_close(_client: Client) -> None:
            assert key_file.path.exists()

        close = AsyncMock(side_effect=force_close)
        monkeypatch.setattr(executor, "_force_close_client", close)
        if close_mode == "evict":
            executor._evict_client_on_cancel("cached")
            await asyncio.gather(*executor._cancel_close_tasks)
        else:
            if close_mode == "force":
                state.loop = object()
            await executor.close_session("cached")
        assert close.await_count == 1
        assert not executor._clients
        assert not key_file.directory.exists()
        assert state.static_key_file is None
    finally:
        await executor.close()
        key_file.close()


# ── real execution of the rewritten helper (offline, local interpreter) ──


@contextmanager
def _helper_for_this_interpreter(token: str) -> Iterator[str]:
    key_file = PrivateStaticKeyFile(token)
    try:
        helper = ex._windows_static_key_helper_command(sys.executable, key_file.path)
        if helper is None:
            pytest.skip("sys.executable path contains characters the helper rejects")
        yield helper
    finally:
        key_file.close()


_EXECUTION_TOKENS = [DUMMY_TOKENS[0], " | ".join(DUMMY_TOKENS[1:])]


@pytest.mark.skipif(os.name == "nt", reason="POSIX sh execution test")
@pytest.mark.parametrize("token", _EXECUTION_TOKENS)
def test_rewritten_helper_prints_exact_key_under_posix_sh(token: str) -> None:
    with _helper_for_this_interpreter(token) as helper:
        result = subprocess.run(
            [shutil.which("sh") or "/bin/sh", "-c", helper],
            capture_output=True,
            timeout=30,
            check=True,
        )
    assert result.stdout == token.encode("utf-8")


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe execution test (Windows only)")
@pytest.mark.parametrize("token", _EXECUTION_TOKENS)
def test_rewritten_helper_prints_exact_key_under_cmd_exe(token: str) -> None:
    with _helper_for_this_interpreter(token) as helper:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        result = subprocess.run(
            f'"{comspec}" /d /s /c "{helper}"',
            capture_output=True,
            timeout=30,
            check=True,
        )
    assert result.stdout == token.encode("utf-8")


@pytest.mark.skipif(os.name != "nt", reason="native Windows interpreter-path test")
def test_windows_helper_uses_interpreter_path_with_spaces(tmp_path: Path) -> None:
    venv = tmp_path / "helper interpreter"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    key_file = PrivateStaticKeyFile("dummy-quoted-path-key")
    try:
        helper = ex._windows_static_key_helper_command(
            str(venv / "Scripts" / "python.exe"), key_file.path
        )
        assert helper is not None
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        result = subprocess.run(
            f'"{comspec}" /d /s /c "{helper}"',
            capture_output=True,
            timeout=30,
            check=True,
        )
        assert result.stdout == b"dummy-quoted-path-key"
    finally:
        key_file.close()


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL verification")
def test_windows_key_file_has_a_protected_current_user_acl() -> None:
    key_file = PrivateStaticKeyFile("dummy-acl-key")
    script = """
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$sidType = [System.Security.Principal.SecurityIdentifier]
foreach ($path in @($env:OMNI_TEST_KEY_DIRECTORY, $env:OMNI_TEST_KEY_PATH)) {
    $acl = Get-Acl -LiteralPath $path
    $rules = @($acl.Access)
    if (!$acl.AreAccessRulesProtected -or $rules.Count -ne 1) { throw 'ACL is not private' }
    if ($acl.GetOwner($sidType).Value -ne $sid) { throw 'Wrong owner' }
    if ($rules[0].IdentityReference.Translate($sidType).Value -ne $sid) {
        throw 'Wrong principal'
    }
    if ($rules[0].AccessControlType -ne 'Allow' -or
        $rules[0].FileSystemRights -ne 'FullControl') { throw 'Wrong rights' }
}
Write-Output 'owner-only ACL verified'
$directory = $env:OMNI_TEST_KEY_DIRECTORY
$acl = Get-Acl -LiteralPath $directory
$world = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($world, 'Read', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $directory -AclObject $acl
"""
    try:
        result = subprocess.run(
            [
                shutil.which("pwsh") or "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            env={
                **os.environ,
                "OMNI_TEST_KEY_DIRECTORY": str(key_file.directory),
                "OMNI_TEST_KEY_PATH": str(key_file.path),
            },
            capture_output=True,
            timeout=30,
            check=True,
        )
        assert b"owner-only ACL verified" in result.stdout
        assert key_file.path.read_bytes() == b"dummy-acl-key"
        with pytest.raises(OSError, match="unexpected access"):
            key_files._WindowsOwnerAcl().verify(key_file.directory, directory=True)
    finally:
        key_file.close()
    assert not key_file.directory.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL failure handling")
@pytest.mark.parametrize("reject_directory", [True, False])
def test_windows_key_file_cleans_up_when_permissions_cannot_be_verified(
    reject_directory: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(key_files.tempfile, "gettempdir", lambda: str(tmp_path))
    verify = key_files._WindowsOwnerAcl.verify
    checked: list[Path] = []

    def refuse(acl, path: Path, *, directory: bool, handle: int | None = None) -> None:
        if directory == reject_directory:
            checked.append(path)
            if not directory:
                assert path.stat().st_size == 0
            raise OSError("permissions unavailable")
        verify(acl, path, directory=directory, handle=handle)

    monkeypatch.setattr(key_files._WindowsOwnerAcl, "verify", refuse)
    with pytest.raises(RuntimeError, match="Could not create a private"):
        PrivateStaticKeyFile("dummy-unwritten-key")
    assert checked
    assert not list(tmp_path.glob("omnigent-claude-key-*"))
