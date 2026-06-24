"""Unit tests for omnigent.onboarding.ucode_setup."""

from __future__ import annotations

import subprocess
import sys
import types
from unittest.mock import patch

import pytest
from click import ClickException

from omnigent.onboarding.setup import ProfileSpec
from omnigent.onboarding.ucode_setup import (
    build_ucode_configure_command,
    configure_ucode_for_workspace,
    find_ucode_command,
    model_gateway_workspace_urls,
    ucode_workspace_exists,
)


def test_find_ucode_command_prefers_uvx_from_main() -> None:
    """Prefers an ephemeral uvx run pinned to main, even if ucode is installed.

    The locally-installed binary may be stale (predating
    ``configure --workspaces``), so uvx-from-main must win when both are
    present. ``--refresh-package ucode`` forces re-resolution of the mutable
    ``main`` ref so the cache can't serve an old commit.
    """
    with patch("shutil.which", return_value="/usr/bin/uvx"):
        assert find_ucode_command() == [
            "/usr/bin/uvx",
            "--refresh-package",
            "ucode",
            "--from",
            "git+https://github.com/databricks/ucode@main",
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
    """Builds the multi-workspace ucode configure command."""
    command = build_ucode_configure_command(
        ("/usr/bin/ucode",),
        workspace_urls=(
            "https://one.example.databricks.com/",
            "https://two.example.databricks.com",
        ),
    )

    assert command == [
        "/usr/bin/ucode",
        "configure",
        "--workspaces",
        "https://one.example.databricks.com,https://two.example.databricks.com",
        "--agents",
        "claude,codex,pi",
    ]


def test_build_ucode_configure_command_supports_uvx_prefix() -> None:
    """Builds a configure command from a multi-part uvx command prefix."""
    command = build_ucode_configure_command(
        (
            "/usr/bin/uvx",
            "--refresh-package",
            "ucode",
            "--from",
            "git+https://github.com/databricks/ucode@main",
            "ucode",
        ),
        workspace_urls=("https://one.example.databricks.com",),
    )

    assert command == [
        "/usr/bin/uvx",
        "--refresh-package",
        "ucode",
        "--from",
        "git+https://github.com/databricks/ucode@main",
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
    stub = types.ModuleType("omnigent.onboarding.internal_beta")
    stub.DEFAULT_PROFILES = profiles
    monkeypatch.setitem(sys.modules, "omnigent.onboarding.internal_beta", stub)

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
    recorded: list[list[str]] = []

    def _run(argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0)

    with (
        patch("omnigent.onboarding.ucode_setup.find_ucode_command", return_value=["ucode"]),
        patch("omnigent.onboarding.ucode_setup.subprocess.run", _run),
    ):
        # Trailing slash on input must be stripped in the emitted command.
        configure_ucode_for_workspace("https://example.cloud.databricks.com/")

    assert recorded == [
        [
            "ucode",
            "configure",
            "--workspaces",
            "https://example.cloud.databricks.com",
            "--agents",
            "claude,codex,pi",
        ]
    ]


def test_configure_ucode_for_workspace_raises_on_nonzero_exit() -> None:
    """A non-zero ``ucode configure`` exit surfaces as a ``ClickException``.

    The add flow must abort loudly rather than persist a Databricks provider
    whose gateway was never actually wired up.
    """

    def _run(argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=3)

    with (
        patch("omnigent.onboarding.ucode_setup.find_ucode_command", return_value=["ucode"]),
        patch("omnigent.onboarding.ucode_setup.subprocess.run", _run),
        pytest.raises(ClickException, match="exited with code 3"),
    ):
        configure_ucode_for_workspace("https://example.cloud.databricks.com")


def test_build_ucode_configure_command_normalizes_pasted_url() -> None:
    """A browser-pasted workspace URL is reduced to scheme://host in the
    ``--workspaces`` argument, so ``ucode configure`` never receives the
    ``/browse?o=...`` path the Databricks CLI cannot tokenize."""
    argv = build_ucode_configure_command(
        ["ucode"],
        workspace_urls=["https://example.cloud.databricks.com/browse?o=42"],
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
