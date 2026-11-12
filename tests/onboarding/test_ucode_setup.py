"""Unit tests for omnigent.onboarding.ucode_setup."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from click import ClickException

from omnigent.onboarding.ucode_setup import (
    build_ucode_configure_command,
    configure_ucode_for_workspace,
    find_ucode_command,
    ucode_workspace_exists,
)


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
            "git+https://github.com/databricks/ucode@304e4a29c5ca73b3bfaaf1911e38d0080833fda4",
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
        "--enable-fable",
    ]


def test_build_ucode_configure_command_supports_uvx_prefix() -> None:
    """Builds a configure command from a multi-part uvx command prefix."""
    command = build_ucode_configure_command(
        (
            "/usr/bin/uvx",
            "--from",
            "git+https://github.com/databricks/ucode@304e4a29c5ca73b3bfaaf1911e38d0080833fda4",
            "ucode",
        ),
        workspace_urls=("https://one.example.databricks.com",),
    )

    assert command == [
        "/usr/bin/uvx",
        "--from",
        "git+https://github.com/databricks/ucode@304e4a29c5ca73b3bfaaf1911e38d0080833fda4",
        "ucode",
        "configure",
        "--workspaces",
        "https://one.example.databricks.com",
        "--agents",
        "claude,codex,pi",
        "--enable-fable",
    ]


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
            "--enable-fable",
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
        "--enable-fable",
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
