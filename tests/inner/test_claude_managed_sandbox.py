"""Managed policy stays intact while model credentials remain outside the worker."""

from __future__ import annotations

import json
import plistlib
from collections.abc import Callable, Iterator
from os import stat_result
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.inner import claude_model_access
from omnigent.inner.claude_model_access import (
    ClaudeModelAccess,
    _claude_managed_preferences_paths,
    _validate_claude_managed_preferences,
)
from omnigent.inner.claude_sdk_executor import _validate_claude_managed_config
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.onboarding import ambient

_MANAGED_FILES = (
    "managed-settings.json",
    "managed-settings.d/auth.json",
    "managed-mcp.json",
)


@pytest.fixture(autouse=True)
def isolated_managed_preferences(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_model_access, "_claude_managed_preferences_paths", lambda: ())


@pytest.fixture
def isolated_managed_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], tuple[Path, ...]]:
    discover = ambient.claude_code_managed_mcp_paths
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", ())
    monkeypatch.setattr(ambient, "claude_code_managed_mcp_paths", lambda: ())
    return discover


@pytest.fixture
def managed_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "managed"
    directory.mkdir()
    return directory


def test_benign_managed_policy_is_preserved(managed_dir: Path) -> None:
    settings = {
        "permissions": {"deny": ["Bash(curl *)"]},
        "env": {"ANTHROPIC_BASE_URL": "https://model.example.test", "ANTHROPIC_MODEL": "test"},
        "apiKeyHelper": "",
    }
    dropins = managed_dir / "managed-settings.d"
    dropins.mkdir()
    (managed_dir / "managed-settings.json").write_text(json.dumps(settings))
    (dropins / "policy.json").write_text('{"allowManagedMcpServersOnly":true}')
    (dropins / "README.txt").write_text("This is not a settings drop-in.")
    (managed_dir / "managed-mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "example": {
                        "command": "${OMNIGENT_MCP_RELAY_PYTHON}",
                        "env": {"OMNIGENT_MCP_RELAY_BRIDGE_DIR": "${BRIDGE_DIR}"},
                    }
                }
            }
        )
    )
    original = {path: path.read_bytes() for path in managed_dir.rglob("*") if path.is_file()}

    _validate_claude_managed_config([managed_dir], {"CUSTOM_MODEL_TOKEN"})

    assert {
        path: path.read_bytes() for path in managed_dir.rglob("*") if path.is_file()
    } == original


@pytest.mark.parametrize("relative_path", _MANAGED_FILES)
@pytest.mark.parametrize(
    "payload",
    [
        {"env": {"ANTHROPIC_API_KEY": "parent-only-key"}},
        {"env": {"CUSTOM_MODEL_TOKEN": "parent-only-key"}},
        {"apiKeyHelper": "printf parent-only-key"},
        {"env": {"CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": "0"}},
        {"env": {"OMNIGENT_CLAUDE_API_KEY_HELPER": "printf parent-only-key"}},
    ],
    ids=["api-key", "source-env", "helper", "token-fd", "helper-env"],
)
def test_managed_auth_is_rejected_in_every_file(
    managed_dir: Path, relative_path: str, payload: dict[str, object]
) -> None:
    """Settings and drop-ins must be checked without a managed MCP file."""
    path = managed_dir / relative_path
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload))
    if relative_path != "managed-mcp.json":
        assert not (managed_dir / "managed-mcp.json").exists()

    with pytest.raises(ValueError, match="unsupported credentials") as error:
        _validate_claude_managed_config([managed_dir], {"CUSTOM_MODEL_TOKEN"})

    assert "parent-only-key" not in str(error.value)
    assert json.loads(path.read_text()) == payload


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "OMNIGENT_ANTHROPIC_API_KEY",
        "OMNIGENT_CUSTOM_MODEL_TOKEN",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
        "OMNIGENT_CLAUDE_FUTURE_SECRET",
    ],
)
def test_nested_managed_mcp_env_rejects_credentials_and_aliases(
    managed_dir: Path, name: str
) -> None:
    payload = {"mcpServers": {"example": {"nested": [{"env": {name: "parent-only-key"}}]}}}
    (managed_dir / "managed-mcp.json").write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="unsupported credentials"):
        _validate_claude_managed_config([managed_dir], {"CUSTOM_MODEL_TOKEN"})


@pytest.mark.parametrize("relative_path", _MANAGED_FILES)
@pytest.mark.parametrize("raw", ["{", "[]", "null", '{"env":[]}'])
def test_invalid_managed_config_fails_closed(
    managed_dir: Path, relative_path: str, raw: str
) -> None:
    path = managed_dir / relative_path
    path.parent.mkdir(exist_ok=True)
    path.write_text(raw)

    with pytest.raises(ValueError, match="unreadable, invalid"):
        _validate_claude_managed_config([managed_dir], ())


@pytest.mark.parametrize("relative_path", _MANAGED_FILES)
@pytest.mark.parametrize("error_type", [PermissionError, FileNotFoundError])
def test_unreadable_or_disappearing_managed_file_fails_closed(
    managed_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    error_type: type[OSError],
) -> None:
    path = managed_dir / relative_path
    path.parent.mkdir(exist_ok=True)
    path.write_text("{}")
    original_read = Path.read_text

    # Inject the read failure so this also works under a privileged test user.
    def read_text(candidate: Path, *args: object, **kwargs: object) -> str:
        if candidate == path:
            raise error_type("parent-only-key")
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(ValueError, match="unreadable, invalid") as error:
        _validate_claude_managed_config([managed_dir], ())
    assert "parent-only-key" not in str(error.value)


@pytest.mark.parametrize("dropin_directory", [False, True], ids=["managed-dir", "dropin-dir"])
def test_unreadable_managed_directory_fails_closed(
    managed_dir: Path, monkeypatch: pytest.MonkeyPatch, dropin_directory: bool
) -> None:
    unreadable = managed_dir / "managed-settings.d" if dropin_directory else managed_dir
    unreadable.mkdir(exist_ok=True)
    original_iterdir = Path.iterdir

    def iterdir(candidate: Path) -> Iterator[Path]:
        if candidate == unreadable:
            raise PermissionError("parent-only-key")
        return original_iterdir(candidate)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(ValueError, match="cannot inspect") as error:
        _validate_claude_managed_config([managed_dir], ())
    assert "parent-only-key" not in str(error.value)


def test_missing_managed_files_are_optional(managed_dir: Path) -> None:
    _validate_claude_managed_config([managed_dir], ())
    (managed_dir / "managed-mcp.json").write_text("{}")
    _validate_claude_managed_config([managed_dir], ())
    (managed_dir / "managed-mcp.json").unlink()
    _validate_claude_managed_config([managed_dir], ())


@pytest.mark.parametrize(
    "raw",
    [
        '{"env":{"ANTHROPIC_API_KEY":"parent-only-key"},"env":{}}',
        '{"apiKeyHelper":"printf parent-only-key","apiKeyHelper":""}',
    ],
)
def test_duplicate_keys_cannot_hide_readable_credentials(managed_dir: Path, raw: str) -> None:
    (managed_dir / "managed-settings.json").write_text(raw)

    with pytest.raises(ValueError, match="unsupported credentials"):
        _validate_claude_managed_config([managed_dir], ())


@pytest.mark.parametrize(
    "relative_path", _MANAGED_FILES[:2], ids=["settings-only", "dropins-only"]
)
def test_benign_policy_without_mcp_is_preserved_during_source_protection(
    managed_dir: Path,
    isolated_managed_discovery: Callable[[], tuple[Path, ...]],
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    tmp_path: Path,
) -> None:
    path = managed_dir / relative_path
    path.parent.mkdir(exist_ok=True)
    raw = b'{"permissions":{"deny":["Bash(curl *)"]},"env":{"ANTHROPIC_MODEL":"test"}}'
    path.write_bytes(raw)
    monkeypatch.setattr(
        ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed_dir / "managed-settings.json",)
    )
    monkeypatch.setattr(ambient, "claude_code_managed_mcp_paths", isolated_managed_discovery)
    assert ambient.claude_code_managed_mcp_paths() == (managed_dir / "managed-mcp.json",)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = SandboxPolicy("darwin_seatbelt", True, [managed_dir], [workspace], [], False)
    access = ClaudeModelAccess("https://model.example.test", "authorization", lambda: "parent-key")

    _validate_claude_managed_config([managed_dir], ())
    protected = access.protect_sources(policy, workspace)

    assert protected.read_roots == policy.read_roots
    assert protected.write_roots == policy.write_roots
    assert {candidate for candidate in managed_dir.rglob("*") if candidate.is_file()} == {path}
    assert path.read_bytes() == raw
    assert not (managed_dir / "managed-mcp.json").exists()


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY], ids=["xml", "binary"])
@pytest.mark.parametrize(
    "payload",
    [
        {"policy": {"nested": [{"env": {"ANTHROPIC_AUTH_TOKEN": "parent-only-key"}}]}},
        {"policy": {"nested": [{"env": {"OMNIGENT_CUSTOM_MODEL_TOKEN": "parent-only-key"}}]}},
        {"policy": {"nested": [{"apiKeyHelper": "printf parent-only-key"}]}},
        {"policy": {"nested": [{"env": {"CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": "0"}}]}},
    ],
    ids=["nested-token", "nested-source-alias", "nested-helper", "nested-token-fd"],
)
def test_managed_preferences_reject_nested_credentials(
    tmp_path: Path, fmt: plistlib.PlistFormat, payload: dict[str, object]
) -> None:
    path = tmp_path / "managed.plist"
    raw = plistlib.dumps(payload, fmt=fmt)
    path.write_bytes(raw)

    with pytest.raises(ValueError, match="unsupported credentials") as error:
        _validate_claude_managed_preferences([path], {"CUSTOM_MODEL_TOKEN"})

    assert "parent-only-key" not in str(error.value)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY], ids=["xml", "binary"])
def test_benign_managed_preferences_are_byte_identical(
    tmp_path: Path, fmt: plistlib.PlistFormat
) -> None:
    path = tmp_path / "managed.plist"
    raw = plistlib.dumps(
        {
            "permissions": {"deny": ["Bash(curl *)"]},
            "env": {"ANTHROPIC_MODEL": "test", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "2048"},
            "apiKeyHelper": "",
        },
        fmt=fmt,
    )
    path.write_bytes(raw)

    _validate_claude_managed_preferences([path], {"CUSTOM_MODEL_TOKEN"})

    assert path.read_bytes() == raw


def test_duplicate_xml_env_cannot_hide_a_readable_credential(tmp_path: Path) -> None:
    path = tmp_path / "managed.plist"
    raw = (
        b'<?xml version="1.0"?><plist version="1.0"><dict>'
        b"<key>env</key><dict><key>ANTHROPIC_API_KEY</key><string>parent-only-key</string></dict>"
        b"<key>env</key><dict/></dict></plist>"
    )
    path.write_bytes(raw)

    with pytest.raises(ValueError, match=r"invalid|unsupported") as error:
        _validate_claude_managed_preferences([path], ())

    assert "parent-only-key" not in str(error.value)
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"not a plist",
        b'<?xml version="1.0"?><plist><dict>',
        b"bplist00truncated",
        plistlib.dumps(["not-an-object"]),
    ],
    ids=["unknown-format", "malformed-xml", "malformed-binary", "not-an-object"],
)
def test_invalid_managed_preferences_fail_closed(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "managed.plist"
    path.write_bytes(raw)

    with pytest.raises(ValueError, match="invalid"):
        _validate_claude_managed_preferences([path], ())


def test_unreadable_managed_preferences_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "managed.plist"
    path.write_bytes(plistlib.dumps({}))
    original_read = Path.read_bytes

    def read_bytes(candidate: Path) -> bytes:
        if candidate == path:
            raise PermissionError("parent-only-key")
        return original_read(candidate)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(ValueError, match="cannot read") as error:
        _validate_claude_managed_preferences([path], ())
    assert "parent-only-key" not in str(error.value)


def test_missing_preferences_do_not_skip_later_existing_auth(tmp_path: Path) -> None:
    missing = tmp_path / "missing.plist"
    _validate_claude_managed_preferences([missing], ())
    existing = tmp_path / "managed.plist"
    existing.write_bytes(plistlib.dumps({"apiKeyHelper": "printf parent-only-key"}))

    with pytest.raises(ValueError, match="unsupported credentials"):
        _validate_claude_managed_preferences([missing, existing], ())


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY], ids=["xml", "binary"])
@pytest.mark.parametrize("credential_file", ["account", "device"])
def test_protect_sources_rejects_managed_preferences_before_private_source_exposure(
    tmp_path: Path,
    isolated_managed_discovery: Callable[[], tuple[Path, ...]],
    monkeypatch: pytest.MonkeyPatch,
    fmt: plistlib.PlistFormat,
    credential_file: str,
) -> None:
    managed = tmp_path / "managed-preferences"
    account = managed / "example-user/com.anthropic.claudecode.plist"
    device = managed / "com.anthropic.claudecode.plist"
    account.parent.mkdir(parents=True)
    for name, path in (("account", account), ("device", device)):
        payload = (
            {"env": {"OMNIGENT_CUSTOM_MODEL_TOKEN": "parent-only-key"}}
            if name == credential_file
            else {"permissions": {"deny": ["Bash(curl *)"]}}
        )
        path.write_bytes(plistlib.dumps(payload, fmt=fmt))
    monkeypatch.setattr(
        claude_model_access, "_claude_managed_preferences_paths", lambda: (account, device)
    )
    credential = Mock(side_effect=AssertionError("credential must remain unread"))
    protect_source = Mock(side_effect=AssertionError("managed preferences must be checked first"))
    monkeypatch.setattr(claude_model_access, "protect_credential_source", protect_source)
    access = ClaudeModelAccess(
        "https://model.example.test",
        "authorization",
        credential,
        source_paths=(tmp_path / "parent-credential.json",),
        source_env_names=frozenset({"CUSTOM_MODEL_TOKEN"}),
    )
    policy = SandboxPolicy("darwin_seatbelt", True, [managed], [], [], False)

    with pytest.raises(ValueError, match="unsupported credentials") as error:
        access.protect_sources(policy, tmp_path)

    assert "parent-only-key" not in str(error.value)
    protect_source.assert_not_called()
    credential.assert_not_called()
    assert policy.read_roots == [managed]
    assert policy.credential_source_paths is None


def test_managed_preferences_discovery_uses_effective_uid_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pwd = pytest.importorskip("pwd")
    lookup = Mock(return_value=SimpleNamespace(pw_name="example-user"))
    monkeypatch.setattr(claude_model_access.sys, "platform", "darwin")
    monkeypatch.setattr(claude_model_access.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(claude_model_access.os, "getuid", lambda: 4321)
    monkeypatch.setattr(pwd, "getpwuid", lookup)
    monkeypatch.setenv("USER", "wrong-user")
    monkeypatch.setenv("LOGNAME", "wrong-user")

    paths = _claude_managed_preferences_paths()

    assert paths == (
        Path("/Library/Managed Preferences/example-user/com.anthropic.claudecode.plist"),
        Path("/Library/Managed Preferences/com.anthropic.claudecode.plist"),
    )
    lookup.assert_called_once_with(1234)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_managed_preferences_discovery_is_macos_only(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(claude_model_access.sys, "platform", platform)

    assert _claude_managed_preferences_paths() == ()


@pytest.mark.parametrize("error_type", [KeyError, OSError])
def test_managed_preferences_account_lookup_fails_closed_with_sanitized_error(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    pwd = pytest.importorskip("pwd")
    lookup = Mock(side_effect=error_type("parent-only-key"))
    monkeypatch.setattr(claude_model_access.sys, "platform", "darwin")
    monkeypatch.setattr(claude_model_access.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(pwd, "getpwuid", lookup)

    with pytest.raises(
        ValueError, match="cannot resolve managed Claude preferences account"
    ) as error:
        _claude_managed_preferences_paths()

    lookup.assert_called_once_with(1234)
    assert "parent-only-key" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("discovery", ["settings-paths", "mcp-paths"])
@pytest.mark.parametrize(
    "relative_path", _MANAGED_FILES[:2], ids=["settings-only", "dropins-only"]
)
@pytest.mark.parametrize(
    "payload",
    [
        {"env": {"ANTHROPIC_API_KEY": "parent-only-key"}},
        {"env": {"OMNIGENT_CUSTOM_MODEL_TOKEN": "parent-only-key"}},
        {"apiKeyHelper": "printf parent-only-key"},
    ],
    ids=["api-key", "source-env-alias", "helper"],
)
def test_protect_sources_discovers_managed_auth_before_protecting_private_sources(
    managed_dir: Path,
    isolated_managed_discovery: Callable[[], tuple[Path, ...]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    discovery: str,
    relative_path: str,
    payload: dict[str, object],
) -> None:
    path = managed_dir / relative_path
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload))
    if discovery == "settings-paths":
        monkeypatch.setattr(
            ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed_dir / "managed-settings.json",)
        )
        monkeypatch.setattr(ambient, "claude_code_managed_mcp_paths", isolated_managed_discovery)
    else:
        monkeypatch.setattr(
            ambient, "claude_code_managed_mcp_paths", lambda: (managed_dir / "managed-mcp.json",)
        )
    assert ambient.claude_code_managed_mcp_paths() == (managed_dir / "managed-mcp.json",)
    assert not (managed_dir / "managed-mcp.json").exists()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_source = tmp_path / "parent-credential.json"
    credential = Mock(side_effect=AssertionError("credential must remain unread"))
    protect_source = Mock(side_effect=AssertionError("managed auth must be checked first"))
    monkeypatch.setattr(claude_model_access, "protect_credential_source", protect_source)
    access = ClaudeModelAccess(
        "https://model.example.test",
        "authorization",
        credential,
        source_paths=(private_source,),
        source_env_names=frozenset({"CUSTOM_MODEL_TOKEN"}),
    )
    policy = SandboxPolicy("darwin_seatbelt", True, [managed_dir], [workspace], [], False)

    with pytest.raises(ValueError, match="unsupported credentials") as error:
        access.protect_sources(policy, workspace)

    assert "parent-only-key" not in str(error.value)
    protect_source.assert_not_called()
    credential.assert_not_called()
    assert policy.read_roots == [managed_dir]
    assert policy.write_roots == [workspace]
    assert policy.credential_source_paths is None


def test_protect_sources_stat_permission_error_fails_closed(
    managed_dir: Path,
    isolated_managed_discovery: Callable[[], tuple[Path, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ambient, "claude_code_managed_mcp_paths", lambda: (managed_dir / "managed-mcp.json",)
    )
    original_stat = Path.stat

    def stat(candidate: Path, *args: object, **kwargs: object) -> stat_result:
        if candidate == managed_dir:
            raise PermissionError("parent-only-key")
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    validate = Mock(side_effect=AssertionError("cannot inspect files before directory access"))
    monkeypatch.setattr(claude_model_access, "_validate_claude_managed_config", validate)
    access = ClaudeModelAccess("https://model.example.test", "authorization", lambda: "parent-key")
    policy = SandboxPolicy("darwin_seatbelt", True, [], [], [], False)

    with pytest.raises(ValueError, match="cannot inspect") as error:
        access.protect_sources(policy, managed_dir)

    validate.assert_not_called()
    assert "parent-only-key" not in str(error.value)
    assert policy.credential_source_paths is None


def test_protect_sources_skips_missing_dirs_but_validates_existing_settings_only_dir(
    managed_dir: Path,
    isolated_managed_discovery: Callable[[], tuple[Path, ...]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-managed-dir"
    monkeypatch.setattr(
        ambient,
        "claude_code_managed_mcp_paths",
        lambda: (missing / "managed-mcp.json", managed_dir / "managed-mcp.json"),
    )
    settings = managed_dir / "managed-settings.json"
    settings.write_text('{"permissions":{"deny":["Bash(curl *)"]}}')
    validate = Mock(wraps=claude_model_access._validate_claude_managed_config)
    monkeypatch.setattr(claude_model_access, "_validate_claude_managed_config", validate)
    access = ClaudeModelAccess("https://model.example.test", "authorization", lambda: "parent-key")
    policy = SandboxPolicy("darwin_seatbelt", True, [], [], [], False)

    protected = access.protect_sources(policy, tmp_path)

    validate.assert_called_once_with([managed_dir], frozenset())
    assert protected.read_roots == []
    assert protected.write_roots == []
    assert not missing.exists()
    assert not (managed_dir / "managed-mcp.json").exists()
