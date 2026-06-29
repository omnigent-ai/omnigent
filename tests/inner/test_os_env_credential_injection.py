"""Functional test: per-user credential injection into the OS-env helper (#5).

Drives the real :func:`~omnigent.inner.os_env.create_os_environment` →
``_HelperProcessClient`` → ``subprocess.Popen`` path and reads the injected
value back out of a live shell, proving the acting collaborator's resolved
secrets actually reach their tool subprocess (and that two actors get their own,
since each tool call spawns its own helper).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.runtime.credentials.injection import build_credential_env

pytestmark = pytest.mark.asyncio


def _spec(cwd: Path) -> OSEnvSpec:
    """A caller-process env with sandboxing off.

    The injection works identically under an active sandbox (the overlay merges
    just before ``Popen`` regardless), but ``type="none"`` keeps this test off
    the platform sandbox backend so it asserts the credential plumbing, not
    seatbelt/bwrap availability.
    """
    return OSEnvSpec(type="caller_process", cwd=str(cwd), sandbox=OSEnvSandboxSpec(type="none"))


async def _echo(os_env: Any, var: str) -> str:
    """Return the value of ``$var`` as seen inside the helper's shell."""
    result = await os_env.shell(f'printf "%s" "${var}"')
    assert result.get("exit_code") == 0, result
    return result.get("stdout", "")


async def test_github_secret_is_injected_into_the_shell(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os_env = create_os_environment(
        _spec(workspace),
        credential_env=build_credential_env({"github": "ghp_ALICE"}),
    )
    assert os_env is not None
    try:
        # The github secret feeds both vars the relevant CLIs read.
        assert await _echo(os_env, "GITHUB_TOKEN") == "ghp_ALICE"
        assert await _echo(os_env, "GH_TOKEN") == "ghp_ALICE"
    finally:
        os_env.close()


async def test_two_actors_get_their_own_token(tmp_path: Path) -> None:
    # Each tool call builds its own helper subprocess, so two collaborators'
    # shells in a shared session see their own credential — the core property.
    for user, token in (("alice", "ghp_ALICE"), ("bob", "ghp_BOB")):
        ws = tmp_path / user
        ws.mkdir()
        os_env = create_os_environment(
            _spec(ws),
            credential_env=build_credential_env({"github": token}),
        )
        assert os_env is not None
        try:
            assert await _echo(os_env, "GITHUB_TOKEN") == token
        finally:
            os_env.close()


async def test_no_overlay_injects_nothing(tmp_path: Path) -> None:
    # The credential-less / single-user path: a var the parent env never sets
    # stays empty, so nothing is injected when there's no acting-user overlay.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    os_env = create_os_environment(_spec(workspace))
    assert os_env is not None
    try:
        assert await _echo(os_env, "OMNIGENT_PERUSER_PROBE_TOKEN") == ""
    finally:
        os_env.close()
