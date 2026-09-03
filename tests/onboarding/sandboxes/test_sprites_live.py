"""Opt-in live bootstrap coverage for the managed Sprites provider."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from omnigent.onboarding.sandboxes.sprites import (
    _AGENT_CLI_SPECS,
    _BOOTSTRAP_SCHEMA_VERSION,
    TOKEN_ENV_VAR,
    SpritesSandboxLauncher,
    render_bootstrap_command,
)

_RUN_LIVE_ENV_VAR = "OMNIGENT_RUN_SPRITES_LIVE_TEST"
_LIVE_INSTALL_SPEC_ENV_VAR = "OMNIGENT_SPRITES_LIVE_INSTALL_SPEC"


def test_live_sprite_bootstrap_is_versioned_and_idempotent() -> None:
    """Provision a real Sprite and verify exact native harness versions."""
    if os.environ.get(_RUN_LIVE_ENV_VAR) != "1":
        pytest.skip(f"set {_RUN_LIVE_ENV_VAR}=1 to run the live Sprites test")
    if not os.environ.get(TOKEN_ENV_VAR):
        pytest.fail(f"{TOKEN_ENV_VAR} is required when {_RUN_LIVE_ENV_VAR}=1")
    install_spec = os.environ.get(_LIVE_INSTALL_SPEC_ENV_VAR)
    if not install_spec:
        pytest.fail(
            f"{_LIVE_INSTALL_SPEC_ENV_VAR} must name a Sprite-reachable Omnigent "
            "artifact when the live test is enabled"
        )

    launcher = SpritesSandboxLauncher(install_spec=install_spec)
    sandbox_id: str | None = None
    try:
        launcher.prepare()
        sandbox_id = launcher.provision(f"live-bootstrap-{uuid4().hex[:10]}")

        package_tree = json.loads(
            launcher.run(
                sandbox_id,
                'npm list --global --prefix "$HOME/.local" --depth=0 --json',
            ).stdout
        )
        installed = package_tree["dependencies"]
        for spec in _AGENT_CLI_SPECS.values():
            package, expected_version = spec.rsplit("@", 1)
            assert installed[package]["version"] == expected_version

        marker_path = "$HOME/.local/share/omnigent-host/bootstrap-version"
        marker = json.loads(launcher.run(sandbox_id, f'cat "{marker_path}"').stdout)
        assert marker == {
            "agent_clis": _AGENT_CLI_SPECS,
            "install_spec": install_spec,
            "schema": _BOOTSTRAP_SCHEMA_VERSION,
        }

        launcher.run(sandbox_id, f'touch -d @1 "{marker_path}"')
        launcher.run(sandbox_id, render_bootstrap_command(install_spec))
        marker_mtime = launcher.run(
            sandbox_id,
            f'stat -c %Y "{marker_path}"',
        ).stdout.strip()
        assert marker_mtime == "1"
    finally:
        if sandbox_id is not None:
            launcher.terminate(sandbox_id)
