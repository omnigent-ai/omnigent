"""Sandbox launchers: run Omnigent hosts in remote sandboxes.

Public API for the ``omnigent sandbox`` CLI and anything else that
bootstraps a sandbox-backed host. Core Omnigent contributes built-in
providers directly; third-party packages can contribute providers through
the ``omnigent.sandbox_providers`` entry point group.
"""

from __future__ import annotations

import click

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxCapabilityError,
    SandboxLauncher,
)
from omnigent.onboarding.sandboxes.bootstrap import (
    DEFAULT_SANDBOX_NAME,
    DerivedWorkspace,
    bootstrap_sandbox_host,
    build_wheels,
    connect_sandbox_host,
    derive_workspace,
    login_app_oauth_in_sandbox,
    set_sandbox_host_name,
    ship_wheels,
)
from omnigent.onboarding.sandboxes.registry import (
    COMMUNITY_MODULE_PREFIX,
    SandboxProviderContribution,
    SandboxProviderMetadata,
    SandboxProviderPluginState,
    SandboxRegistryError,
    available_providers,
    get_provider_metadata,
    instantiate,
    plugin_state,
    reset_plugin_state_for_tests,
)
from omnigent.onboarding.sandboxes.types import (
    HostContext,
    SandboxCapabilities,
    SandboxCommandError,
    SandboxConfigError,
    SandboxError,
    SandboxInfo,
    SandboxSpec,
)

__all__ = [
    "COMMUNITY_MODULE_PREFIX",
    "DEFAULT_SANDBOX_NAME",
    "DerivedWorkspace",
    "HostContext",
    "RemoteCommandResult",
    "RemoteProcess",
    "SandboxCapabilities",
    "SandboxCapabilityError",
    "SandboxCommandError",
    "SandboxConfigError",
    "SandboxError",
    "SandboxInfo",
    "SandboxLauncher",
    "SandboxProviderContribution",
    "SandboxProviderMetadata",
    "SandboxProviderPluginState",
    "SandboxRegistryError",
    "SandboxSpec",
    "available_providers",
    "bootstrap_sandbox_host",
    "build_wheels",
    "connect_sandbox_host",
    "derive_workspace",
    "get_launcher",
    "get_provider_metadata",
    "login_app_oauth_in_sandbox",
    "plugin_state",
    "reset_plugin_state_for_tests",
    "set_sandbox_host_name",
    "ship_wheels",
]


def get_launcher(provider: str, *, workspace_host: str | None = None) -> SandboxLauncher:
    """
    Resolve a provider name to a launcher instance.

    :param provider: Provider name, e.g. ``"lakebox"``.
    :param workspace_host: Databricks workspace fronting the target
        server (derived from ``--server`` via
        :func:`~omnigent.onboarding.sandboxes.bootstrap.derive_workspace`),
        e.g. ``"https://example.databricks.com"``. Consumed only by
        the lakebox launcher, which pins its local ``databricks
        lakebox`` calls to it so sandboxes are created in the server's
        workspace. Other providers' sandboxes don't live in a
        Databricks workspace, so the value is meaningless for them and
        deliberately not forwarded — the CLI derives it from --server
        for every provider, so rejecting it here would break them.
    :returns: A fresh launcher for the provider.
    :raises click.ClickException: If the provider is unknown or its
        launcher module is not present in this build.
    """
    if provider not in plugin_state():
        offered = ", ".join(available_providers()) or "(none in this build)"
        raise click.ClickException(
            f"Unknown or unavailable sandbox provider '{provider}'. Available: {offered}."
        )
    try:
        return instantiate(provider, workspace_host=workspace_host)
    except SandboxRegistryError as exc:
        raise click.ClickException(str(exc)) from exc
