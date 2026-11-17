"""How a deployment records the MCP services a session starts with.

Interactive sessions pick MCP services when they are created. Sessions the
server creates on someone's behalf, a scheduled task's fired sessions and the
workers a session starts, must start with the same services, so the server
needs to turn a stored list of service names into session labels and read it
back. The registry default writes ``omnigent.mcp_service.<id>`` labels, which
the runner applies when it loads the agent spec, so it works wherever a session
runs. A sandbox provider that configures MCP servers inside the sandbox at launch
sets its own :class:`SessionMcpSelection` as its config's ``mcp_selection``; the
server uses it only for sessions in that provider's sandboxes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.mcp_selection import MCP_SELECTION_PREFIX, registry_selection_labels
from omnigent.server.schemas import MAX_MCP_REGISTRY_SERVICES, MCP_REGISTRY_SERVICE_ID_PATTERN

if TYPE_CHECKING:
    from fastapi import Request

    from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
    from omnigent.spec.types import AgentSpec

_REGISTRY_SERVICE_ID = re.compile(MCP_REGISTRY_SERVICE_ID_PATTERN)
MAX_SESSION_MCP_SERVICES = MAX_MCP_REGISTRY_SERVICES


class SessionMcpSelection(Protocol):
    """Validates, writes and reads a session's MCP service selection."""

    def validate(
        self,
        request: Request,
        services: Sequence[str],
        *,
        spec: AgentSpec | None,
        user_id: str | None,
    ) -> tuple[str, ...]:
        """Return the de-duplicated services, or raise ``OmnigentError``.

        Called when an owner saves the selection, so it may require the owner
        to be signed in to every service.
        """
        ...

    def session_labels(self, services: Sequence[str]) -> dict[str, str]:
        """Labels that make a new session start with *services*."""
        ...

    def selected_services(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        """The services a session's *labels* select, in label order."""
        ...


class RegistryMcpSelection:
    """Selects services from the server's MCP registry."""

    def validate(
        self,
        request: Request,
        services: Sequence[str],
        *,
        spec: AgentSpec | None,
        user_id: str | None,
    ) -> tuple[str, ...]:
        from omnigent.server.auth import RESERVED_USER_LOCAL
        from omnigent.server.routes.session_mcp_servers import _require_registry_service

        unique = _unique_services(services)
        owner = user_id or RESERVED_USER_LOCAL
        for service in unique:
            if not _REGISTRY_SERVICE_ID.match(service):
                raise OmnigentError(
                    f"Invalid MCP service id {service!r}", code=ErrorCode.INVALID_INPUT
                )
            _require_registry_service(request, service, user_id)
            registry = request.app.state.mcp_registry
            if not registry.connected(registry.services[service], owner, request.app.state):
                raise OmnigentError(
                    f"Connect your {service} account before selecting it",
                    code=ErrorCode.FORBIDDEN,
                )
        if spec is not None:
            # Rejects a service whose name the agent already uses for a custom server.
            registry_selection_labels(spec, list(unique))
        return unique

    def session_labels(self, services: Sequence[str]) -> dict[str, str]:
        return {MCP_SELECTION_PREFIX + service: "1" for service in services}

    def selected_services(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(
            key.removeprefix(MCP_SELECTION_PREFIX)
            for key, value in labels.items()
            if key.startswith(MCP_SELECTION_PREFIX) and value == "1"
        )


def mcp_selection_for(sandbox: ManagedSandboxConfig | None) -> SessionMcpSelection:
    """The selection for a session in *sandbox*'s provider, or the registry's outside one."""
    configured = sandbox.mcp_selection if sandbox is not None else None
    return configured if configured is not None else RegistryMcpSelection()


def task_sandbox(
    deployment: ManagedSandboxDeployment | None, execution_target: str
) -> ManagedSandboxConfig | None:
    """The provider a scheduled task's new runs launch in, or ``None`` on a connected host."""
    if execution_target != "managed_sandbox" or deployment is None:
        return None
    return deployment.default


def host_sandbox(app_state: Any, host_id: str | None) -> ManagedSandboxConfig | None:
    """The provider behind an existing *host_id*, or ``None`` for a connected host.

    Blocking: reads the host row.
    """
    deployment = getattr(app_state, "sandbox_config", None)
    host_store = getattr(app_state, "host_store", None)
    if host_id is None or deployment is None or host_store is None:
        return None
    host = host_store.get_host(host_id)
    if host is None or host.sandbox_provider is None:
        return None
    return deployment.for_provider(host.sandbox_provider)


def _unique_services(services: Sequence[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(services))
    if len(unique) > MAX_SESSION_MCP_SERVICES:
        raise OmnigentError(
            f"Select at most {MAX_SESSION_MCP_SERVICES} MCP services",
            code=ErrorCode.INVALID_INPUT,
        )
    return unique
