"""How a process reads pull requests on code host instance hosts, decided once.

The host names its instance file, and every host the file declares, in the
environment of a runner it starts; it sets them last, after anything a session
forwarded, so only the host can name them. The runner takes both out of its own
environment when it starts, before it starts any process, and loads the file once.
From then on each declared host has one fixed route for the runner's lifetime: the
instance's CLI, or nothing. A declared host never runs plain ``gh``.

The host process itself never runs an instance's CLI: while a session's runner is
parked, the host's own reads for every declared host run nothing.

Instances serve reads only for now: an instance never offers a capability that
writes to its code host, whatever its file lists.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from omnigent.git_providers import provider_display
from omnigent.git_providers.code_host_instances import (
    NO_INSTANCES,
    CodeHostInstance,
    CodeHostInstanceError,
    CodeHostInstances,
    load_code_host_instances,
)
from omnigent.runner.git_providers import ProviderCapabilities
from omnigent.runner.identity import RUNNER_SESSION_ENV_NAMES_ENV_VAR, strip_runner_auth_secrets

_logger = logging.getLogger(__name__)

# Set by the host on the runners it starts; the absolute path of its instance file.
CODE_HOST_INSTANCES_FILE_ENV_VAR = "OMNIGENT_RUNNER_CODE_HOST_INSTANCES_FILE"
# Set with it: every host the host's file declares, comma-separated, even when the
# host refused the file. A runner runs nothing for one it can't serve.
CODE_HOST_INSTANCE_HOSTS_ENV_VAR = "OMNIGENT_RUNNER_CODE_HOST_INSTANCE_HOSTS"
# What an instance may offer: reading pull requests, never changing them.
READ_CAPABILITIES = frozenset(
    {"line_counts", "linked_pr_diff", "review_threads", "commit_history"}
)
CAPABILITY_NAMES = frozenset(field.name for field in dataclasses.fields(ProviderCapabilities))

# Why a declared host runs nothing: this runner couldn't load its instance, or this
# is the host process, which leaves instance reads to a session's runner.
DeniedReason = Literal["cli_unavailable", "needs_runner"]


@dataclass(frozen=True)
class CodeHostRoute:
    """How this process reads one declared host's pull requests, fixed once.

    :ivar instance: The instance whose CLI runs, or ``None`` when nothing runs.
    :ivar denied: Why nothing runs, when :attr:`instance` is ``None``.
    :ivar environment: The CLI's whole environment, pinned from values the host set.
    """

    instance: CodeHostInstance | None
    denied: DeniedReason | None = None
    environment: Mapping[str, str] = dataclasses.field(
        default_factory=lambda: MappingProxyType({}), compare=False, repr=False
    )


@dataclass(frozen=True)
class CodeHostRouting:
    """Every declared host's route in this process; hosts not in it run plain ``gh``."""

    instances: CodeHostInstances
    routes: Mapping[str, CodeHostRoute]

    def route_for(self, host: str | None) -> CodeHostRoute | None:
        return self.routes.get(host.lower()) if host else None


NO_ROUTING = CodeHostRouting(NO_INSTANCES, MappingProxyType({}))

# What the runner took from its environment at start: the file, the declared hosts,
# and the values its host set, without any a session forwarded. Bare CLI names
# resolve only on a PATH among those values.
_taken: tuple[str, frozenset[str], Mapping[str, str]] | None = None
_host_routing: CodeHostRouting | None = None
_host_routing_lock = threading.Lock()


def take_runner_settings(environ: dict[str, str] | os._Environ[str] = os.environ) -> None:
    """Take the host's instance settings out of a starting runner's environment.

    Runs first in a runner, so no process it starts inherits them.
    """
    global _taken
    raw = environ.pop(CODE_HOST_INSTANCES_FILE_ENV_VAR, "")
    hosts = frozenset(
        name.strip().lower()
        for name in environ.pop(CODE_HOST_INSTANCE_HOSTS_ENV_VAR, "").split(",")
        if name.strip()
    )
    session_names = set(environ.get(RUNNER_SESSION_ENV_NAMES_ENV_VAR, "").split(","))
    host_values = {
        name: value
        for name, value in strip_runner_auth_secrets(environ).items()
        if name not in session_names
    }
    _taken = (raw, hosts, MappingProxyType(host_values))
    _runner_routing.cache_clear()


def discard_runner_settings(environ: dict[str, str] | os._Environ[str] = os.environ) -> None:
    """Drop runner-only settings a host process inherited; it never serves them."""
    environ.pop(CODE_HOST_INSTANCES_FILE_ENV_VAR, None)
    environ.pop(CODE_HOST_INSTANCE_HOSTS_ENV_VAR, None)


def deny_in_this_process(hosts: Iterable[str]) -> None:
    """Make the host's own reads run nothing for *hosts*; the first call decides.

    The host calls this once, when it reads its file, before it answers any read.
    """
    global _host_routing
    with _host_routing_lock:
        if _host_routing is not None:
            return
        _host_routing = CodeHostRouting(
            NO_INSTANCES,
            MappingProxyType(
                {host.lower(): CodeHostRoute(None, "needs_runner") for host in hosts}
            ),
        )


def code_host_routing() -> CodeHostRouting:
    """This process's routing: the host's, or this runner's, decided once."""
    return _host_routing if _host_routing is not None else _runner_routing()


@functools.cache
def _runner_routing() -> CodeHostRouting:
    """Route each host the runner's host declared, from the file it named, once.

    A declared host whose instance can't load runs nothing. Without settings taken
    at start, such as in the host process, nothing is routed.
    """
    if _taken is None:
        return NO_ROUTING
    raw, declared, host_values = _taken
    instances = NO_INSTANCES
    if raw and os.path.isabs(raw):
        try:
            # A PATH a session forwarded is absent here, so it never picks the CLI.
            instances = load_code_host_instances(
                Path(raw),
                known_capabilities=CAPABILITY_NAMES,
                host_environment={"PATH": host_values.get("PATH", "")},
            )
        except CodeHostInstanceError:
            _logger.warning("code host instances are refused in this runner", exc_info=False)
    routes: dict[str, CodeHostRoute] = {
        host: CodeHostRoute(None, "cli_unavailable") for host in declared
    }
    for instance in instances.instances:
        if instance.provider == "github":
            environment = MappingProxyType(instance.environment(host_values))
            for host in instance.hosts:
                routes[host.lower()] = CodeHostRoute(instance, environment=environment)
    return CodeHostRouting(instances, MappingProxyType(routes))


def active_code_host_instances() -> CodeHostInstances:
    """Return the instances this process runs."""
    return code_host_routing().instances


def instance_for_host(host: str | None) -> CodeHostInstance | None:
    """Return the GitHub-compatible instance serving *host*, or ``None``."""
    route = code_host_routing().route_for(host)
    return route.instance if route is not None else None


def is_instance_host(host: str | None) -> bool:
    """Whether *host* is declared for an instance: its CLI runs, or nothing does."""
    return code_host_routing().route_for(host) is not None


def unavailable_instance_host(host: str | None) -> bool:
    """Whether *host* is declared for an instance that this process doesn't run.

    Such a host must not fall back to plain ``gh``, which could use a stored token.
    """
    route = code_host_routing().route_for(host)
    return route is not None and route.instance is None


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
