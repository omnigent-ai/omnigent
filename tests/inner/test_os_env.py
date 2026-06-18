"""Unit tests for :mod:`omnigent.inner.os_env` helper-env construction."""

from __future__ import annotations

import base64
from pathlib import Path

from omnigent.inner.os_env import _read_impl, build_helper_env
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR


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


# ---------------------------------------------------------------------------
# _read_impl — binary file handling
# ---------------------------------------------------------------------------

_BINARY = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff"


def test_read_impl_binary_descriptor_for_agent(tmp_path: Path) -> None:
    """With no byte cap (agent ``sys_os_read`` path) binary is not inlined.

    The base64 payload would be useless to the model and could saturate the
    context window, so only a descriptor is returned.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000)

    assert result["encoding"] == "base64"
    assert result["content"] == ""
    assert result["total_bytes"] == len(_BINARY)
    # Not truncated — the payload was deliberately omitted, not cut short.
    assert result["truncated"] is False
    assert "note" in result


def test_read_impl_binary_inlined_within_cap(tmp_path: Path) -> None:
    """A byte cap larger than the file inlines the whole payload, untruncated.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000, max_binary_bytes=10 * 1024 * 1024)

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == _BINARY
    assert result["total_bytes"] == len(_BINARY)
    assert result["truncated"] is False


def test_read_impl_binary_truncated_at_cap(tmp_path: Path) -> None:
    """A byte cap smaller than the file truncates and flags it.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000, max_binary_bytes=4)

    assert base64.b64decode(result["content"]) == _BINARY[:4]
    assert result["returned_bytes"] == 4
    assert result["total_bytes"] == len(_BINARY)
    assert result["truncated"] is True
