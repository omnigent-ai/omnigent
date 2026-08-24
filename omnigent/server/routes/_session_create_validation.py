"""Shared validation for creating session-like conversations.

The interactive session route and scheduled tasks both persist values that
eventually cross runner or host boundaries. Keep the security-sensitive checks
in one place so scheduled task create/update/fire cannot drift from
``POST /v1/sessions``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.model_override import validate_model_override
from omnigent.reasoning_effort import EFFORT_VALUES, validate_effort
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, local_single_user_enabled
from omnigent.server.routes._auth_helpers import require_access
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.host_store import host_is_live

_logger = logging.getLogger(__name__)


def validate_session_model_metadata(
    *,
    model_override: str | None,
    reasoning_effort: str | None,
) -> tuple[str | None, str | None]:
    """Validate persisted model metadata shared by sessions and schedules."""
    # The persisted override reaches native CLIs as a ``--model`` argv element
    # at terminal launch, so reject shell-/flag-shaped values before any
    # session row or scheduled task row persists it.
    validated_model: str | None = None
    if model_override is not None:
        try:
            validated_model = validate_model_override(model_override)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid model_override: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    # Persisted effort reaches native CLIs as a ``--effort`` argv element at
    # terminal launch (and SDK harnesses via the spawn env). Validate against
    # the shared vocabulary before any row persists it; provider-specific
    # support is enforced downstream at launch, mirroring the multipart
    # metadata create path.
    validated_effort: str | None = None
    if reasoning_effort is not None:
        try:
            validated_effort = validate_effort(
                reasoning_effort,
                "session metadata",
                EFFORT_VALUES,
            )
        except ValueError as exc:
            raise OmnigentError(
                f"invalid reasoning_effort: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
    return validated_model, validated_effort


async def validate_session_agent(
    *,
    user_id: str | None,
    agent_id: str,
    agent_store: AgentStore,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
) -> Any:
    """Load a bindable agent and authorize session-scoped agent access."""
    agent = await asyncio.to_thread(agent_store.get, agent_id)
    if agent is None:
        raise OmnigentError(
            f"Agent not found: {agent_id!r}",
            code=ErrorCode.NOT_FOUND,
        )

    # Session-scoped agents belong to a specific session. The caller must have
    # at least READ access to that owning session — otherwise they can execute
    # another user's private agent by guessing the raw agent id.
    if agent.session_id is not None:
        # Single-user servers persist the local owner as NULL (scheduled tasks
        # store user_id=None), but session grants are keyed by the "local"
        # sentinel — the same identity POST /v1/sessions checks. Resolve None to
        # it here so a session-scoped agent authorizes, instead of tripping the
        # require_access unauthenticated guard. Multi-user servers leave None as
        # None, which still correctly 401s.
        access_user = user_id
        if access_user is None and local_single_user_enabled():
            access_user = RESERVED_USER_LOCAL
        await require_access(
            access_user,
            agent.session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
    return agent


async def validate_existing_host_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
    host_store: Any | None,
    host_registry: Any | None,
) -> str:
    """Validate a connected-host workspace against the agent's os_env boundary."""
    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        validate_workspace,
    )

    if workspace is None:
        raise OmnigentError(
            "workspace required when host_id is set",
            code=ErrorCode.INVALID_INPUT,
        )
    from omnigent.server.routes._workspace_validation import _is_windows_absolute_path

    if not workspace.startswith("/") and not _is_windows_absolute_path(workspace):
        raise OmnigentError(
            "workspace must be an absolute path starting with /",
            code=ErrorCode.INVALID_INPUT,
        )
    if agent_cache is None:
        # Should never happen in production — the route factory always wires
        # an agent cache. Fail loud rather than silently skipping validation,
        # which would let bad workspaces through.
        raise OmnigentError(
            "workspace validation requires an agent cache",
            code=ErrorCode.INTERNAL_ERROR,
        )
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured on this server",
            code=ErrorCode.INTERNAL_ERROR,
        )

    from omnigent.server.routes._host_launch import resolve_host_owner

    # Authorize host ownership FIRST — before loading the agent spec or the
    # host.stat round-trip below. A non-owner must be rejected (403/404 via the
    # shared resolve_host_owner) before we touch the host or even read the agent
    # bundle (cross-user host probe). The returned host also gives the display
    # name for error messages.
    host_name: str | None = None
    if host_store is not None:
        host = await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id,
            host_id=host_id,
            host_store=host_store,
        )
        host_name = host.name
        # Wrong-replica classification, same as the /v1/hosts/* endpoints and
        # RunnerRouter: validate_workspace below does a local host_registry miss
        # → "host is offline" (invalid_input), which the client can't recover
        # from. If the host is live per the store but its tunnel isn't on this
        # replica, the create landed on the wrong replica — surface WRONG_REPLICA
        # so the client re-addresses WITHOUT the key. A genuinely offline host
        # falls through to the invalid_input case. Both are 400; the distinct
        # code, not the status, is what tells the client to re-address rather
        # than give up. Safe to raise here: workspace validation runs BEFORE
        # create_conversation, so no orphan row is left.
        if host_registry is not None and host_registry.get(host_id) is None and host_is_live(host):
            raise OmnigentError(
                f"host {host_name or host_id!r} is on another replica; retry",
                code=ErrorCode.WRONG_REPLICA,
            )

    # Read the agent's os_env.cwd — None when the spec has no os_env block
    # (headless agents). Headless agents have no filesystem access at all but
    # still get launched on hosts for sessions that don't need it; treat their
    # cwd as relative-equivalent so the boundary is unrestricted.
    spec_cwd: str | None = None
    if agent.bundle_location is not None:
        try:
            loaded = await asyncio.to_thread(
                agent_cache.load,
                agent.id,
                agent.bundle_location,
            )
            os_env = getattr(loaded.spec, "os_env", None)
            spec_cwd = getattr(os_env, "cwd", None) if os_env is not None else None
        except Exception as exc:
            _logger.exception("Failed to load agent spec for workspace validation")
            raise OmnigentError(
                f"failed to load agent spec: {exc}",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc

    try:
        return await validate_workspace(
            host_registry=host_registry,
            host_id=host_id,
            workspace=workspace,
            spec_cwd=spec_cwd,
            host_name_for_errors=host_name,
        )
    except WorkspaceValidationError as exc:
        raise OmnigentError(
            exc.message,
            code=ErrorCode.INVALID_INPUT,
        ) from exc
