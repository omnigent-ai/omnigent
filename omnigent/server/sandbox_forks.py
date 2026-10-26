"""Eligibility for provider-owned sandbox copies."""

from __future__ import annotations

from omnigent.entities import Conversation
from omnigent.server.managed_hosts import ManagedSandboxDeployment
from omnigent.stores.host_store import Host


def sandbox_fork_provider(
    conversation: Conversation,
    workspace_readable: bool,
    host: Host | None,
    deployment: ManagedSandboxDeployment | None,
) -> str | None:
    """Return the source provider when this viewer can request a sandbox copy."""
    if (
        not workspace_readable
        or not conversation.workspace
        or host is None
        or host.host_id != conversation.host_id
        or host.deleted_at is not None
        or not host.sandbox_id
        or not host.sandbox_provider
        or deployment is None
    ):
        return None
    provider_config = deployment.for_provider(host.sandbox_provider)
    if provider_config is None or not provider_config.managed_launch_supported:
        return None
    capabilities = provider_config.launcher_factory().capabilities
    return host.sandbox_provider if capabilities.fork_sandbox else None
