"""Sandboxed terminal panes inherit only the allowlisted host environment."""

from __future__ import annotations

import os
from unittest.mock import patch

from omnigent.inner.sandbox import SandboxPolicy
from omnigent.inner.terminal import terminal_pane_environment


def _sandbox(*, env_passthrough: list[str] | None = None) -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=False,
        env_passthrough=env_passthrough,
    )


def test_active_sandbox_filters_ambient_credentials() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        "LANG": "C.UTF-8",
        "AWS_SECRET_ACCESS_KEY": "host-secret",
        "CUSTOM_ALLOWED": "explicitly-allowed",
        "CUSTOM_OTHER": "not-allowed",
    }
    with patch.dict(os.environ, parent, clear=True):
        env = terminal_pane_environment(
            inherit_env=True,
            env={"TERMINAL_OVERRIDE": "kept"},
            env_unset=[],
            sandbox_policy=_sandbox(env_passthrough=["CUSTOM_ALLOWED"]),
        )

    assert env["PATH"] == "/usr/bin"
    assert env["CUSTOM_ALLOWED"] == "explicitly-allowed"
    assert env["TERMINAL_OVERRIDE"] == "kept"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "CUSTOM_OTHER" not in env


def test_terminal_exclusions_apply_after_sandbox_passthrough() -> None:
    with patch.dict(os.environ, {"LANG": "C.UTF-8", "CUSTOM_ALLOWED": "value"}, clear=True):
        env = terminal_pane_environment(
            inherit_env=True,
            env={},
            env_unset=["CUSTOM_ALLOWED"],
            sandbox_policy=_sandbox(env_passthrough=["CUSTOM_ALLOWED"]),
        )
    assert "CUSTOM_ALLOWED" not in env


def test_unsandboxed_terminal_preserves_inherited_environment() -> None:
    with patch.dict(os.environ, {"LANG": "C.UTF-8", "CUSTOM_OTHER": "value"}, clear=True):
        env = terminal_pane_environment(inherit_env=True, env={}, env_unset=[])
    assert env["CUSTOM_OTHER"] == "value"


def test_inherit_env_false_ignores_parent_even_in_sandbox() -> None:
    with patch.dict(os.environ, {"LANG": "C.UTF-8", "CUSTOM_ALLOWED": "value"}, clear=True):
        env = terminal_pane_environment(
            inherit_env=False,
            env={},
            env_unset=[],
            sandbox_policy=_sandbox(env_passthrough=["CUSTOM_ALLOWED"]),
        )
    assert "CUSTOM_ALLOWED" not in env
