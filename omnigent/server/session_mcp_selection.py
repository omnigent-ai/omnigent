"""How a deployment records the MCP services a session starts with.

Interactive sessions pick MCP services when they are created. Sessions the
server creates on someone's behalf, a scheduled task's fired sessions and the
workers a session starts, must start with the same services, so the server
needs to turn a stored list of service names into session labels and read it
back. The registry default writes ``omnigent.mcp_service.<id>`` labels, which
the runner applies when it loads the agent spec. A deployment that configures
MCP servers some other way (for example, inside a sandbox at launch) passes its
own :class:`SessionMcpSelection` to ``create_app``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.mcp_selection import MCP_SELECTION_PREFIX, registry_selection_labels

if TYPE_CHECKING:
    from fastapi import Request

    from omnigent.spec.types import AgentSpec

# Matches the session-create ``mcp_registry_services`` id format and limit.
_REGISTRY_SERVICE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
MAX_SESSION_MCP_SERVICES = 40


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
        from omnigent.server.routes.session_mcp_servers import _require_registry_service

        unique = _unique_services(services)
        for service in unique:
            if not _REGISTRY_SERVICE_ID.match(service):
                raise OmnigentError(
                    f"Invalid MCP service id {service!r}", code=ErrorCode.INVALID_INPUT
                )
            _require_registry_service(request, service, user_id)
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


def session_mcp_selection(app_state: Any) -> SessionMcpSelection:
    """The deployment's selection, or the registry default when none is configured."""
    configured = getattr(app_state, "session_mcp_selection", None) if app_state else None
    return configured if configured is not None else RegistryMcpSelection()


def _unique_services(services: Sequence[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(services))
    if len(unique) > MAX_SESSION_MCP_SERVICES:
        raise OmnigentError(
            f"Select at most {MAX_SESSION_MCP_SERVICES} MCP services",
            code=ErrorCode.INVALID_INPUT,
        )
    return unique
