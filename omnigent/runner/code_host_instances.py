"""The code host instances a runner serves, as its host started it with.

The host names its instance file in the runner's environment only when its server
turned instances on and the host loaded at least one; the host sets that variable
last, after anything a session forwarded, so only the host can name the file. The
runner reads the file once, the first time a panel step asks, and never again.

Instances serve reads only for now: an instance never offers a capability that
writes to its code host, whatever its file lists.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
from pathlib import Path

from omnigent.git_providers import provider_display
from omnigent.git_providers.code_host_instances import (
    NO_INSTANCES,
    CodeHostInstance,
    CodeHostInstanceError,
    CodeHostInstances,
    load_code_host_instances,
)
from omnigent.runner.git_providers import ProviderCapabilities

_logger = logging.getLogger(__name__)

# Set by the host on the runners it starts; the absolute path of its instance file.
CODE_HOST_INSTANCES_FILE_ENV_VAR = "OMNIGENT_RUNNER_CODE_HOST_INSTANCES_FILE"
# Set with it: the hosts the host serves, comma-separated. A runner that can't load an
# instance for one of them runs nothing for it rather than fall back to plain ``gh``.
CODE_HOST_INSTANCE_HOSTS_ENV_VAR = "OMNIGENT_RUNNER_CODE_HOST_INSTANCE_HOSTS"
# What an instance may offer: reading pull requests, never changing them.
READ_CAPABILITIES = frozenset(
    {"line_counts", "linked_pr_diff", "review_threads", "commit_history"}
)
CAPABILITY_NAMES = frozenset(field.name for field in dataclasses.fields(ProviderCapabilities))


# The instances a host serves to pull request reads it answers itself, while a
# runner is parked; set only by the host, from records its own loader built.
_served_in_this_process: CodeHostInstances | None = None


def serve_in_this_process(instances: CodeHostInstances | None) -> None:
    """Make this process's panel steps serve *instances*, or stop with ``None``.

    The host calls this with the instances it loaded at start, while it serves them
    to a server that turned them on, so its own pull request reads match its runners'.
    """
    global _served_in_this_process
    _served_in_this_process = instances


def active_code_host_instances() -> CodeHostInstances:
    """Return the instances this process serves.

    The host's own set when it serves in process; otherwise a runner's, read once.
    """
    if _served_in_this_process is not None:
        return _served_in_this_process
    return _runner_instances()


@functools.cache
def _runner_instances() -> CodeHostInstances:
    """Return the instances this runner serves, read once from its host's file.

    No instances when the host named no file, or the file is refused now.
    """
    raw = os.environ.get(CODE_HOST_INSTANCES_FILE_ENV_VAR, "")
    if not raw or not os.path.isabs(raw):
        return NO_INSTANCES
    try:
        return load_code_host_instances(
            Path(raw), known_capabilities=CAPABILITY_NAMES, host_environment=os.environ
        )
    except CodeHostInstanceError:
        _logger.warning("code host instances are refused in this runner", exc_info=False)
        return NO_INSTANCES


def instance_for_host(host: str | None) -> CodeHostInstance | None:
    """Return the GitHub-compatible instance serving *host*, or ``None``."""
    return active_code_host_instances().instance_for("github", host)


def unavailable_instance_host(host: str | None) -> bool:
    """Whether the host serves *host* as an instance, but this runner couldn't load it.

    Such a host must not fall back to plain ``gh``, which could use a stored token.
    """
    if not host:
        return False
    served = {
        name.strip().lower()
        for name in os.environ.get(CODE_HOST_INSTANCE_HOSTS_ENV_VAR, "").split(",")
        if name.strip()
    }
    return host.lower() in served and instance_for_host(host) is None


def instance_capabilities(
    instance: CodeHostInstance, capabilities: ProviderCapabilities
) -> ProviderCapabilities:
    """Return *capabilities* narrowed to what *instance* may offer.

    A capability stays on only when the facet offers it, the instance lists it, and it
    reads rather than writes.
    """
    return dataclasses.replace(
        capabilities,
        **{
            name: bool(getattr(capabilities, name))
            and name in instance.capabilities
            and name in READ_CAPABILITIES
            for name in CAPABILITY_NAMES
        },
    )


def provider_display_for(provider_id: str, host: str | None) -> dict[str, str] | None:
    """Return *provider_id*'s display labels, named for the instance serving *host*."""
    display = provider_display(provider_id)
    instance = instance_for_host(host) if provider_id == "github" else None
    if display is None or instance is None:
        return display
    return {**display, "display_name": instance.display_name}
