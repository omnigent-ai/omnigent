"""Tests for :mod:`omnigent.onboarding.sandboxes.types`."""

from __future__ import annotations

import click
import pytest

from omnigent.onboarding.sandboxes.types import (
    HostContext,
    SandboxCapabilities,
    SandboxCommandError,
    SandboxConfigError,
    SandboxError,
    SandboxInfo,
    SandboxSpec,
)


def test_capabilities_defaults() -> None:
    """The default capability set has every feature disabled."""
    caps = SandboxCapabilities()
    assert caps.cli_bootstrap is False
    assert caps.managed_launch is False
    assert caps.local_port_forward is False
    assert caps.resume_stopped is False
    assert caps.programmatic_terminate is False
    assert caps.file_copy is False
    assert caps.streaming_exec is False
    assert caps.foreground_exec is False


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
    assert ctx.repo_url is None
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
