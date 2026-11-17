"""Reject session requirements that connected hosts and runners cannot enforce."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from omnigent.errors import ErrorCode, OmnigentError, WrongReplicaError
from omnigent.host.frames import CAP_MCP_REGISTRY, CAP_TCP_EGRESS
from omnigent.inner.datamodel import OSEnvSpec

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.runner.routing import RunnerRouter
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.host_registry import HostRegistry
    from omnigent.spec import AgentSpec
    from omnigent.stores import AgentStore


class RuntimeSupportError(OmnigentError):
    """Runtime requirements could not be verified for a saved session."""


def registry_services(spec: AgentSpec | None) -> set[str]:
    """Include nested agents: runners parse the whole bundle before selecting one."""
    if spec is None:
        return set()
    services = {server.name for server in spec.mcp_servers if server.transport == "registry"}
    for child in spec.sub_agents:
        services.update(registry_services(child))
    return services


def uses_tcp_egress(spec: AgentSpec | None) -> bool:
    """Inspect the whole bundle, including explicit terminal environments."""
    if spec is None:
        return False
    environments = [
        spec.os_env,
        *(terminal.os_env for terminal in (spec.terminals or {}).values()),
    ]
    return any(
        isinstance(env, OSEnvSpec)
        and env.sandbox is not None
        and env.sandbox.tcp_egress is not None
        for env in environments
    ) or any(uses_tcp_egress(child) for child in spec.sub_agents)


def require_tcp_egress_support(
    required: bool, capabilities: Collection[str], *, component: str
) -> None:
    if required and CAP_TCP_EGRESS not in capabilities:
        raise OmnigentError(
            f"The {component} does not advertise TCP egress support required by the sandbox. "
            "Install an Omnigent build with TCP egress support on the execution host "
            "(or update its sandbox image), then restart the host and session runner. "
            "Updating only the server is not sufficient.",
            code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
        )


def require_registry_mcp_support(
    services: Collection[str], capabilities: Collection[str], *, component: str
) -> None:
    """Reject only registry MCP use on a connected build lacking the capability."""
    if services and CAP_MCP_REGISTRY not in capabilities:
        raise OmnigentError(
            f"The {component} does not advertise MCP registry support required by "
            f"{', '.join(sorted(services))}. Install an Omnigent build with MCP registry "
            "support on the execution host (or update its sandbox image), then restart "
            "the host and session runner. Updating only the server is not sufficient.",
            code=ErrorCode.RUNNER_CAPABILITY_MISMATCH,
        )


def require_session_runtime_support(
    conversation: Conversation,
    capabilities: Collection[str],
    *,
    component: str,
    agent_store: AgentStore,
    agent_cache: AgentCache,
) -> None:
    """Check a saved bundle before launching or initializing its runtime."""
    if {CAP_MCP_REGISTRY, CAP_TCP_EGRESS}.issubset(capabilities) or conversation.agent_id is None:
        return
    agent = agent_store.get(conversation.agent_id)
    if agent is None:
        return
    try:
        spec = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        ).spec
    except Exception as exc:
        raise RuntimeSupportError(
            "Cannot verify runtime support because this session's agent bundle could not "
            "be loaded. Restore or replace the bundle and retry.",
            code=exc.code if isinstance(exc, OmnigentError) else ErrorCode.CONFLICT,
        ) from exc
    from omnigent.mcp_selection import selected_mcp_spec

    require_registry_mcp_support(
        registry_services(selected_mcp_spec(spec, conversation.labels)),
        capabilities,
        component=component,
    )
    require_tcp_egress_support(uses_tcp_egress(spec), capabilities, component=component)


def require_runtime_support(
    services: Collection[str],
    *,
    tcp_egress_required: bool = False,
    host_id: str | None,
    runner_id: str | None,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    runner_router: RunnerRouter | None = None,
) -> None:
    """Check known runtimes; existing routing handles offline and remote replicas."""
    if not services and not tcp_egress_required:
        return
    host = host_registry.get(host_id) if host_id and host_registry is not None else None
    runner = tunnel_registry.get(runner_id) if runner_id and tunnel_registry is not None else None
    if host is None and runner is None and host_id and runner_router is not None:
        if runner_router.host_is_on_another_replica(host_id):
            raise WrongReplicaError(
                "Runtime support must be checked on the host's server replica",
                host_id=host_id,
            )
    for component, connection in (("host", host), ("runner", runner)):
        if connection is not None:
            require_registry_mcp_support(
                services, connection.hello.capabilities, component=component
            )
            require_tcp_egress_support(
                tcp_egress_required, connection.hello.capabilities, component=component
            )
