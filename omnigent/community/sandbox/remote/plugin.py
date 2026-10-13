"""Entry point for the ``omnigent.sandbox_providers`` plugin group."""

from __future__ import annotations

from omnigent.community.sandbox.remote.config import RemoteSandboxConfig, managed_token_ttl_s
from omnigent.onboarding.sandboxes.registry import (
    SandboxProviderContribution,
    SandboxProviderMetadata,
)


def get_contribution() -> SandboxProviderContribution:
    return SandboxProviderContribution(
        name="omnigent-remote-sandbox",
        providers={
            "remote": SandboxProviderMetadata(
                name="remote",
                launcher_class=(
                    "omnigent.community.sandbox.remote.launcher:RemoteSandboxLauncher"
                ),
                config_model=RemoteSandboxConfig,
                managed_token_ttl_s=managed_token_ttl_s(),
            )
        },
    )
