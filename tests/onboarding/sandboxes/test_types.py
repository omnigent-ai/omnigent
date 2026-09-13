"""Tests for :mod:`omnigent.onboarding.sandboxes.types`."""

from __future__ import annotations

import click
import pytest

from omnigent.onboarding.sandboxes.types import (
    HostContext,
    RepoWorkspace,
    SandboxCapabilities,
    SandboxCommandError,
    SandboxConfigError,
    SandboxError,
    SandboxInfo,
    SandboxSpec,
    clone_dir_names,
)


def _repo(url: str, name: str) -> RepoWorkspace:
    return RepoWorkspace(url=url, branch=None, repo_name=name)


def test_clone_dir_names_disambiguates_collisions_by_owner() -> None:
    """Distinct URLs deriving the same repo_name get owner-qualified dirs; a
    unique name stays plain (so the single-repo working directory is unchanged)."""
    repos = [
        _repo("https://github.com/org-a/api", "api"),
        _repo("git@github.com:org-b/api.git", "api"),
        _repo("https://github.com/org-c/web", "web"),
    ]
    assert clone_dir_names(repos) == ["org-a__api", "org-b__api", "web"]
    # A single repo never collides — plain name.
    assert clone_dir_names([_repo("https://github.com/o/solo", "solo")]) == ["solo"]


def test_clone_dir_names_suffixes_residual_collisions() -> None:
    """If even the owner-qualified name repeats (same owner+name), a numeric
    suffix guarantees uniqueness so no two repos share a clone directory."""
    repos = [
        _repo("https://github.com/org/api", "api"),
        _repo("https://github.com/org/api", "api"),
    ]
    names = clone_dir_names(repos)
    assert len(set(names)) == 2, names


def test_capabilities_defaults() -> None:
    """The default capability set has every feature disabled."""
    caps = SandboxCapabilities()
    assert caps.cli_bootstrap is False
    assert caps.managed_launch is False
    assert caps.local_port_forward is False
    assert caps.resume_stopped is False
    assert caps.snapshot_restore is False
    assert caps.programmatic_terminate is False
    assert caps.file_copy is False
    assert caps.streaming_exec is False
    assert caps.foreground_exec is False
    # Off by default so a single-repo provider (and every out-of-tree one) is
    # never handed a multi-repo request; providers opt in explicitly.
    assert caps.multi_repo is False


def test_capabilities_custom() -> None:
    """Capabilities can be enabled field-by-field."""
    caps = SandboxCapabilities(cli_bootstrap=True, foreground_exec=True)
    assert caps.cli_bootstrap is True
    assert caps.foreground_exec is True
    assert caps.managed_launch is False


def test_sandbox_spec_defaults() -> None:
    """SandboxSpec has sensible defaults for optional fields."""
    spec = SandboxSpec(name="test")
    assert spec.name == "test"
    assert spec.image is None
    assert spec.cpu is None
    assert spec.memory_mib is None
    assert spec.disk_gb is None
    assert spec.lifetime_s is None
    assert spec.tags == {}


def test_sandbox_info_defaults() -> None:
    """SandboxInfo carries an id and optional workspace/metadata."""
    info = SandboxInfo(sandbox_id="sb_123")
    assert info.sandbox_id == "sb_123"
    assert info.workspace_path is None
    assert info.metadata == {}


def test_host_context_defaults() -> None:
    """HostContext has defaults for optional repo/config/stage args."""
    ctx = HostContext(token="tok", host_id="hid", host_name="hname", server_url="https://srv")
    assert ctx.token == "tok"
    assert ctx.host_id == "hid"
    assert ctx.host_name == "hname"
    assert ctx.server_url == "https://srv"
    assert ctx.repos == []
    assert ctx.on_stage is None
    assert ctx.host_config == {}


def test_errors_inherit_from_sandbox_error() -> None:
    """All sandbox error types are catchable as SandboxError."""
    with pytest.raises(SandboxError):
        raise SandboxConfigError("bad config")


def test_sandbox_command_error_carries_fields() -> None:
    """SandboxCommandError exposes command, returncode, and streams."""
    exc = SandboxCommandError(
        "failed",
        command="echo hi",
        returncode=1,
        stdout="out",
        stderr="err",
    )
    assert exc.command == "echo hi"
    assert exc.returncode == 1
    assert exc.stdout == "out"
    assert exc.stderr == "err"
    assert str(exc) == "failed"


def test_sandbox_capability_error_is_click_exception() -> None:
    """SandboxCapabilityError is catchable as click.ClickException (transition)."""
    from omnigent.onboarding.sandboxes import SandboxCapabilityError

    with pytest.raises(click.ClickException):
        raise SandboxCapabilityError("not supported")
