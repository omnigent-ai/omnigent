"""Unit tests for omnigent.onboarding.ucode_setup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from click import ClickException

from omnigent.cli_config import _run_configure_databricks
from omnigent.onboarding.setup import ProfileSpec
from omnigent.onboarding.ucode_setup import (
    build_ucode_configure_command,
    configure_ucode_for_workspace,
    find_ucode_command,
    model_gateway_workspace_urls,
    ucode_workspace_exists,
)

_PINNED_UCODE_SOURCE = (
    "git+https://github.com/databricks/ucode@5f72236eea3ab01db2cc4ff5fd4f15ede7626c33"
)
_WORKSPACE_URL = "https://example.cloud.databricks.com"
_GLOBAL_SCOPE_ARGV = [
    "ucode",
    "configure",
    "--workspaces",
    _WORKSPACE_URL,
    "--agents",
    "claude,codex,pi",
]


@pytest.fixture(autouse=True)
def _isolate_scope_inputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's ucode state, OS and managed settings out of scope selection."""
    monkeypatch.setattr("omnigent.onboarding.ucode_state._STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS",
        (tmp_path / "ClaudeCode" / "managed-settings.json",),
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_setup._CODEX_MANAGED_CONFIG_PATH",
        tmp_path / "codex" / "managed_config.toml",
    )


@pytest.fixture
def macos_account(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[int]], None]:
    """Simulate a non-root macOS user; call the result with the user's group ids."""
    grp = pytest.importorskip("grp")
    pwd = pytest.importorskip("pwd")
    admin_gid = 80

    def _getgrnam(name: str) -> types.SimpleNamespace:
        if name != "admin":
            raise KeyError(name)
        return types.SimpleNamespace(gr_gid=admin_gid)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(grp, "getgrnam", _getgrnam)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_name="dev"))

    def _set_groups(group_ids: list[int]) -> None:
        monkeypatch.setattr(os, "getgrouplist", lambda user, gid: list(group_ids))

    return _set_groups


def _configure_and_record(returncode: int = 0) -> list[list[str]]:
    """Run ``configure_ucode_for_workspace`` against a stubbed ucode; return its argv."""
    recorded: list[list[str]] = []

    def _run(argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=returncode)

    with (
        patch("omnigent.onboarding.ucode_setup.find_ucode_command", return_value=["ucode"]),
        patch("omnigent.onboarding.ucode_setup.subprocess.run", _run),
    ):
        configure_ucode_for_workspace(f"{_WORKSPACE_URL}/")
    return recorded


def _stub_internal_beta(
    monkeypatch: pytest.MonkeyPatch, profiles: tuple[ProfileSpec, ...]
) -> None:
    """Install a synthetic ``internal_beta`` catalog, which the OSS build omits."""
    stub = types.ModuleType("omnigent.onboarding.internal_beta")
    stub.DEFAULT_PROFILES = profiles
    monkeypatch.setitem(sys.modules, "omnigent.onboarding.internal_beta", stub)


def test_find_ucode_command_prefers_uvx_pinned_commit() -> None:
    """Prefers an ephemeral uvx run pinned to a fixed commit, even if ucode is
    installed.

    The locally-installed binary may be stale (predating
    ``configure --workspaces``), so uvx-from-pinned-commit must win when both
    are present. The ref is a full SHA so setup resolves a reproducible ucode
    rather than a moving ``main`` HEAD.
    """
    with patch("shutil.which", return_value="/usr/bin/uvx"):
        assert find_ucode_command() == [
            "/usr/bin/uvx",
            "--from",
            _PINNED_UCODE_SOURCE,
            "ucode",
        ]


def test_find_ucode_command_falls_back_to_local_binary_without_uvx() -> None:
    """Uses an installed ucode binary only when uvx is unavailable."""

    def _which(name: str) -> str | None:
        """Return only ucode as available on PATH."""
        return "/usr/bin/ucode" if name == "ucode" else None

    with patch("shutil.which", side_effect=_which):
        assert find_ucode_command() == ["/usr/bin/ucode"]


def test_find_ucode_command_raises_when_no_runner_exists() -> None:
    """Raises a ClickException when neither uvx nor ucode is available."""
    import click

    with patch("shutil.which", return_value=None):
        try:
            find_ucode_command()
        except click.ClickException as exc:
            assert "uvx is not on PATH" in str(exc)
        else:
            raise AssertionError("expected ClickException")


def test_build_ucode_configure_command_uses_workspaces() -> None:
    """Builds a single-workspace configure command; ucode rejects a comma list."""
    command = build_ucode_configure_command(
        ("/usr/bin/ucode",), workspace_url="https://one.example.databricks.com/"
    )

    assert command == [
        "/usr/bin/ucode",
        "configure",
        "--workspaces",
        "https://one.example.databricks.com",
        "--agents",
        "claude,codex,pi",
    ]


def test_build_ucode_configure_command_appends_user_scope() -> None:
    """``user_scope`` selects ucode's per-user settings, which need no sudo."""
    command = build_ucode_configure_command(
        ("ucode",), workspace_url=_WORKSPACE_URL, agents=("claude",), user_scope=True
    )

    assert command == [
        "ucode",
        "configure",
        "--workspaces",
        _WORKSPACE_URL,
        "--agents",
        "claude",
        "--user",
    ]


def test_build_ucode_configure_command_supports_uvx_prefix() -> None:
    """Builds a configure command from a multi-part uvx command prefix."""
    command = build_ucode_configure_command(
        (
            "/usr/bin/uvx",
            "--from",
            _PINNED_UCODE_SOURCE,
            "ucode",
        ),
        workspace_url="https://one.example.databricks.com",
    )

    assert command == [
        "/usr/bin/uvx",
        "--from",
        _PINNED_UCODE_SOURCE,
        "ucode",
        "configure",
        "--workspaces",
        "https://one.example.databricks.com",
        "--agents",
        "claude,codex,pi",
    ]


def test_model_gateway_workspace_urls_excludes_mcp_only_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only gateway workspaces reach ucode; MCP-only profiles are excluded.

    ucode configures the model-serving gateway, so the pre-fix behavior of
    passing every profile made ucode do wasted gateway-discovery work against
    MCP-only workspaces. This pins the result to exactly the
    ``is_model_gateway`` workspace, so a regression that reverted to passing
    all profiles (or that dropped the gateway) fails here. Uses a synthetic
    catalog stubbed into ``sys.modules`` because the production function
    lazy-imports ``internal_beta``, which is absent from the OSS build;
    the real-catalog pins live in ``test_internal_beta.py``.
    """
    profiles = (
        ProfileSpec(
            name="gateway",
            host="https://gateway.example.databricks.com/",
            purpose="model-serving gateway",
            is_model_gateway=True,
        ),
        ProfileSpec(
            name="mcp-only-a",
            host="https://mcp-a.example.databricks.com",
            purpose="MCP auth only",
            is_model_gateway=False,
        ),
        ProfileSpec(
            name="mcp-only-b",
            host="https://mcp-b.example.databricks.com",
            purpose="MCP auth only",
            is_model_gateway=False,
        ),
    )
    _stub_internal_beta(monkeypatch, profiles)

    urls = model_gateway_workspace_urls()

    # Exactly the gateway workspace, trailing "/" stripped, nothing else.
    # An MCP-only host appearing here means the filter regressed to the
    # old "configure every profile"; a missing gateway host means the
    # gateway flag stopped being honored.
    assert urls == ["https://gateway.example.databricks.com"]


def test_ucode_workspace_exists_checks_single_workspace() -> None:
    """Delegates a single workspace existence check to the state reader."""
    with patch("omnigent.onboarding.ucode_setup.read_ucode_state", return_value=object()):
        assert ucode_workspace_exists("https://example.databricks.com")


def test_configure_ucode_for_workspace_targets_single_workspace() -> None:
    """``configure_ucode_for_workspace`` runs ``ucode configure`` for exactly the
    one workspace it is given, with all three coding agents.

    This is the per-workspace path behind ``configure harnesses → Databricks``:
    a regression that passed multiple workspaces (the retired multi-profile
    behavior) or dropped an agent would change the recorded argv and fail here.
    """
    # The input's trailing slash must be stripped in the emitted command.
    assert _configure_and_record() == [_GLOBAL_SCOPE_ARGV]


def test_configure_ucode_for_workspace_raises_on_nonzero_exit() -> None:
    """A non-zero ``ucode configure`` exit surfaces as a ``ClickException``.

    The add flow must abort loudly rather than persist a Databricks provider
    whose gateway was never actually wired up. The message names the exact
    ``--user`` rerun for a user who cannot approve the managed-settings write.
    """
    with pytest.raises(ClickException, match="exited with code 3") as excinfo:
        _configure_and_record(returncode=3)

    assert (
        "ucode configure --workspaces https://example.cloud.databricks.com "
        "--agents claude,codex,pi --user"
    ) in excinfo.value.message


def test_configure_ucode_for_workspace_uses_user_scope_for_macos_non_admin(
    macos_account: Callable[[list[int]], None],
) -> None:
    """A Mac user outside the ``admin`` group configures without ucode's sudo write."""
    macos_account([20, 12])

    assert _configure_and_record() == [[*_GLOBAL_SCOPE_ARGV, "--user"]]


def test_configure_ucode_for_workspace_keeps_global_scope_for_macos_admin(
    macos_account: Callable[[list[int]], None],
) -> None:
    """A Mac admin can approve the sudo prompt, so ucode keeps its default scope."""
    macos_account([20, 80])

    assert _configure_and_record() == [_GLOBAL_SCOPE_ARGV]


@pytest.mark.parametrize(
    "managed_file",
    [
        pytest.param("ClaudeCode/managed-settings.json", id="claude"),
        pytest.param("codex/managed_config.toml", id="codex"),
    ],
)
def test_configure_ucode_for_workspace_keeps_global_scope_over_managed_settings(
    macos_account: Callable[[list[int]], None],
    tmp_path: Path,
    managed_file: str,
) -> None:
    """ucode refuses ``--user`` over existing managed settings, so Omnigent never sends it."""
    macos_account([20])
    managed_path = tmp_path / managed_file
    managed_path.parent.mkdir(parents=True)
    managed_path.write_text("{}")

    assert _configure_and_record() == [_GLOBAL_SCOPE_ARGV]


@pytest.mark.parametrize("saved_user_scope", [False, True])
def test_configure_ucode_for_workspace_managed_settings_failure_asks_for_administrator(
    tmp_path: Path,
    saved_user_scope: bool,
) -> None:
    """A failed run over existing managed settings is not told to retry with ``--user``."""
    if saved_user_scope:
        (tmp_path / "state.json").write_text(json.dumps({"config_scope": "user"}))
    managed_path = tmp_path / "ClaudeCode" / "managed-settings.json"
    managed_path.parent.mkdir(parents=True)
    managed_path.write_text("{}")

    with pytest.raises(ClickException, match="exited with code 1") as excinfo:
        _configure_and_record(returncode=1)

    assert "configure your user only" not in excinfo.value.message
    assert "ask an administrator" in excinfo.value.message


def test_configure_ucode_for_workspace_keeps_saved_user_scope(tmp_path: Path) -> None:
    """A user who chose ``ucode configure --user`` keeps that scope on every platform."""
    (tmp_path / "state.json").write_text(json.dumps({"config_scope": "user"}))

    assert _configure_and_record() == [[*_GLOBAL_SCOPE_ARGV, "--user"]]


def test_configure_ucode_for_workspace_user_scope_failure_has_no_user_hint(
    tmp_path: Path,
) -> None:
    """A failed ``--user`` run is not told to retry with ``--user``."""
    (tmp_path / "state.json").write_text(json.dumps({"config_scope": "user"}))

    with pytest.raises(ClickException, match="exited with code 2") as excinfo:
        _configure_and_record(returncode=2)

    assert "configure your user only" not in excinfo.value.message


def test_run_configure_databricks_configures_each_gateway_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal-beta flow runs one ``ucode configure`` per gateway workspace."""
    _stub_internal_beta(
        monkeypatch,
        (
            ProfileSpec(
                name="gateway-a",
                host="https://gateway-a.example.databricks.com",
                purpose="model-serving gateway",
                is_model_gateway=True,
            ),
            ProfileSpec(
                name="mcp-only",
                host="https://mcp.example.databricks.com",
                purpose="MCP auth only",
                is_model_gateway=False,
            ),
            ProfileSpec(
                name="gateway-b",
                host="https://gateway-b.example.databricks.com",
                purpose="model-serving gateway",
                is_model_gateway=True,
            ),
        ),
    )
    recorded: list[list[str]] = []

    def _run(argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr("omnigent.cli_config.find_ucode_command", lambda: ["ucode"])
    monkeypatch.setattr("omnigent.onboarding.ucode_setup.subprocess.run", _run)

    _run_configure_databricks()

    assert [argv[argv.index("--workspaces") + 1] for argv in recorded] == [
        "https://gateway-a.example.databricks.com",
        "https://gateway-b.example.databricks.com",
    ]


def test_build_ucode_configure_command_normalizes_pasted_url() -> None:
    """A browser-pasted workspace URL is reduced to scheme://host in the
    ``--workspaces`` argument, so ``ucode configure`` never receives the
    ``/browse?o=...`` path the Databricks CLI cannot tokenize."""
    argv = build_ucode_configure_command(
        ["ucode"],
        workspace_url="https://example.cloud.databricks.com/browse?o=42",
        agents=["claude"],
    )
    assert argv == [
        "ucode",
        "configure",
        "--workspaces",
        "https://example.cloud.databricks.com",
        "--agents",
        "claude",
    ]


def test_build_ucode_configure_command_for_profile_broker_mode() -> None:
    from omnigent.onboarding.ucode_setup import build_ucode_configure_command_for_profile

    argv = build_ucode_configure_command_for_profile(
        ["ucode"], profile="omnigent", agents=["claude", "codex", "pi"]
    )
    assert argv == [
        "ucode",
        "configure",
        "--profiles",
        "omnigent",
        "--agents",
        "claude,codex,pi",
        "--skip-validate",
        "--skip-upgrade",
        "--skip-unavailable",
    ]
    # broker mode: no --use-pat (the caller supplies DATABRICKS_BEARER_COMMAND)
    assert "--use-pat" not in argv


def test_build_ucode_configure_command_for_profile_pat_mode() -> None:
    from omnigent.onboarding.ucode_setup import build_ucode_configure_command_for_profile

    argv = build_ucode_configure_command_for_profile(
        ["ucode"], profile="DEFAULT", agents=["claude"], use_pat=True
    )
    assert argv[-1] == "--use-pat"  # lakebox authenticates from the injected profile PAT
    assert "claude" in argv[argv.index("--agents") + 1]
