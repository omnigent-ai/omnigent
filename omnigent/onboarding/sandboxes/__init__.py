"""Sandbox launchers: run Omnigent hosts in remote sandboxes.

Public API for the ``omnigent sandbox`` CLI and anything else that
bootstraps a sandbox-backed host. Core Omnigent contributes built-in
providers directly; third-party packages can contribute providers through
the ``omnigent.sandbox_providers`` entry point group.
"""

from __future__ import annotations

import click

from omnigent.onboarding.sandboxes.base import (
    OwnerCredentialMissingError,
    RemoteCommandResult,
    RemoteProcess,
    SandboxCapabilityError,
    SandboxGoneError,
    SandboxHostLauncher,
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
    GitCloneOptions,
    HostContext,
    RepoWorkspace,
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
    "GitCloneOptions",
    "HostContext",
    "OwnerCredentialMissingError",
    "RemoteCommandResult",
    "RemoteProcess",
    "RepoWorkspace",
    "SandboxCapabilities",
    "SandboxCapabilityError",
    "SandboxCommandError",
    "SandboxConfigError",
    "SandboxError",
    "SandboxGoneError",
    "SandboxHostLauncher",
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


def get_launcher(
    provider: str,
    *,
    server_url: str | None = None,
) -> SandboxHostLauncher:
    """
    Resolve a provider name to a launcher instance.

    :param provider: Provider name, e.g. ``"modal"``.
    :param server_url: Normalized CLI target URL. Microsandbox uses it to
        scope guest-to-host access to a local server's port; other providers
        ignore it.
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
        return instantiate(provider, server_url=server_url)
    except SandboxRegistryError as exc:
        raise click.ClickException(str(exc)) from exc
