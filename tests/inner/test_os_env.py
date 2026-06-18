"""Unit tests for :mod:`omnigent.inner.os_env` helper-env construction."""

from __future__ import annotations

from omnigent.inner.os_env import build_helper_env
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner.identity import (
    OMNIGENT_SESSION_ENV_VALUE,
    OMNIGENT_SESSION_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
)


def _inactive_policy() -> SandboxPolicy:
    """A ``sandbox.type: none`` policy (user opted out of sandboxing).

    :returns: An inactive :class:`SandboxPolicy` whose ``build_helper_env``
        branch mirrors the parent environment.
    """
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def _active_policy() -> SandboxPolicy:
    """An active policy that drives ``build_helper_env``'s allowlist branch.

    ``build_helper_env`` only consults ``active`` and ``env_passthrough``;
    the ``backend_type`` is never activated here, so ``"none"`` is fine.

    :returns: An active :class:`SandboxPolicy`.
    """
    return SandboxPolicy(
        backend_type="none",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def test_build_helper_env_inactive_strips_binding_token() -> None:
    """``sandbox.type: none`` mirrors parent env MINUS the binding token.

    Opting out of sandboxing grants the agent broad
    file/network access, but it must NOT additionally leak the runner's
    control-plane auth secret. Asserts ``PATH`` survives (the opt-out
    still mirrors the parent env) while the token is dropped.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "bug-binding-token-secret",
    }

    env = build_helper_env(parent, _inactive_policy())

    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in env
    assert "bug-binding-token-secret" not in env.values()
    assert env["PATH"] == "/usr/bin"


def test_build_helper_env_active_drops_binding_token() -> None:
    """The active allowlist branch never admits the binding token.

    The deny-by-default allowlist excludes the token's name, so even if
    it is present in the parent env it does not reach the helper.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "bug-binding-token-secret",
    }

    env = build_helper_env(parent, _active_policy())

    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in env
    assert "bug-binding-token-secret" not in env.values()
    assert env["PATH"] == "/usr/bin"  # PATH is in the default allowlist


def test_build_helper_env_active_passes_omnigent_session_marker() -> None:
    """The ``OMNIGENT`` session marker survives the active allowlist.

    The marker (set once on the runner process) must reach an agent's
    sandboxed shell so code running there can detect it is inside an
    Omnigent session, the way ``CLAUDE_CODE`` / ``CODEX`` are visible in
    their own agents' shells.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        OMNIGENT_SESSION_ENV_VAR: OMNIGENT_SESSION_ENV_VALUE,
    }

    env = build_helper_env(parent, _active_policy())

    assert env[OMNIGENT_SESSION_ENV_VAR] == OMNIGENT_SESSION_ENV_VALUE
