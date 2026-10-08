"""Keep registry references away from hosts and runners that cannot load them."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_MCP_REGISTRY

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.runner.routing import RunnerRouter
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.host_registry import HostRegistry
    from omnigent.spec import AgentSpec
    from omnigent.stores import AgentStore


def registry_services(spec: AgentSpec | None) -> set[str]:
    """Include nested agents: runners parse the whole bundle before selecting one."""
    if spec is None:
        return set()
    services = {server.name for server in spec.mcp_servers if server.transport == "registry"}
    for child in spec.sub_agents:
        services.update(registry_services(child))
    return services


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


def require_session_registry_mcp_support(
    conversation: Conversation,
    capabilities: Collection[str],
    *,
    component: str,
    agent_store: AgentStore,
    agent_cache: AgentCache,
) -> None:
    """Check a saved bundle before launching or initializing its runtime."""
    if CAP_MCP_REGISTRY in capabilities or conversation.agent_id is None:
        return
    agent = agent_store.get(conversation.agent_id)
    if agent is None:
        return
    spec = agent_cache.load(
        agent.id, agent.bundle_location, expand_env=agent.operator_authored
    ).spec
    require_registry_mcp_support(registry_services(spec), capabilities, component=component)


def require_registry_mcp_runtime(
    services: Collection[str],
    *,
    host_id: str | None,
    runner_id: str | None,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    runner_router: RunnerRouter | None = None,
) -> None:
    """Check known runtimes; existing routing handles offline and remote replicas."""
    if not services:
        return
    host = host_registry.get(host_id) if host_id and host_registry is not None else None
    runner = tunnel_registry.get(runner_id) if runner_id and tunnel_registry is not None else None
    if host is None and runner is None and host_id and runner_router is not None:
        if runner_router.host_is_on_another_replica(host_id):
            raise OmnigentError(
                "MCP registry support must be checked on the host's server replica",
                code=ErrorCode.WRONG_REPLICA,
            )
    for component, connection in (("host", host), ("runner", runner)):
        if connection is not None:
            require_registry_mcp_support(
                services, connection.hello.capabilities, component=component
            )
