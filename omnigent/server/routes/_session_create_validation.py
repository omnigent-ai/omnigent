"""Shared validation for creating session-like conversations.

The interactive session route and scheduled tasks both persist values that
eventually cross runner or host boundaries. Keep the security-sensitive checks
in one place so scheduled task create/update/fire cannot drift from
``POST /v1/sessions``.
"""

from __future__ import annotations

import asyncio
import logging
import ntpath
import os
import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import ValidationError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.model_override import validate_model_override
from omnigent.reasoning_effort import EFFORT_VALUES, validate_effort
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_READ, RESERVED_USER_LOCAL, local_single_user_enabled
from omnigent.server.routes._auth_helpers import require_access
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.host_store import host_is_live
from omnigent.stores.project_store import ProjectStore

_logger = logging.getLogger(__name__)

_STRICT_PROJECT_CREATE_ENV = "OMNIGENT_STRICT_PROJECT_SESSION_CREATE"


@dataclass(frozen=True)
class ProjectCreateResolution:
    """Project-aware request values and any non-fatal consistency warnings."""

    body: Any
    project_id: str | None = None
    warnings: tuple[dict[str, str], ...] = ()


def _strict_project_create_enabled() -> bool:
    """Return strict mismatch mode for direct creates and inherited fork filing."""
    return os.environ.get(_STRICT_PROJECT_CREATE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _workspace_within(candidate: str, root: str) -> bool:
    """Return whether normalized *candidate* is lexically inside *root*."""
    try:
        windows = "\\" in candidate or "\\" in root
        path_module = ntpath if windows else posixpath
        path_type = PureWindowsPath if windows else PurePosixPath
        candidate_path = path_type(path_module.normpath(candidate))
        root_path = path_type(path_module.normpath(root))
        return candidate_path.is_relative_to(root_path)
    except (TypeError, ValueError):
        return False


async def resolve_project_session_create(
    *,
    body: Any,
    user_id: str | None,
    project_store: ProjectStore | None,
    warn_on_mismatch: bool = True,
) -> ProjectCreateResolution:
    """Apply opt-in project defaults before any create-side validation.

    Field presence, rather than value, controls defaulting.  Consequently an
    explicit JSON ``null`` remains explicit and is never replaced by a project
    hint.  Unknown and foreign projects deliberately share one 404 response.

    ``warn_on_mismatch=False`` skips the consistency warnings (and their
    strict-mode escalation) for callers whose project is inherited rather than
    requested — e.g. a fork filing into its source's project, where a mismatch
    belongs to the source session, not this request.
    """
    fields_set = set(body.model_fields_set)
    project_id = getattr(body, "project_id", None)
    if "project_id" not in fields_set or project_id is None:
        if getattr(body, "agent_id", None) is None and "agent_id" in body.__class__.model_fields:
            raise OmnigentError("agent_id is required", code=ErrorCode.INVALID_INPUT)
        return ProjectCreateResolution(body=body)
    if project_store is None:
        raise OmnigentError(
            "Project not found",
            code=ErrorCode.NOT_FOUND,
        )
    project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
    if project is None:
        raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)

    config = project.config
    updates: dict[str, Any] = {}
    for field in ("agent_id", "workspace", "git"):
        if field not in fields_set and field in config and field in body.__class__.model_fields:
            updates[field] = config[field]
    resolved_data = body.model_dump()
    resolved_data.update(updates)
    # Re-validate project hints because config is intentionally stored as
    # opaque JSON and may not match the session-create field types.
    try:
        resolved = body.__class__.model_validate(resolved_data)
    except ValidationError as exc:
        first = exc.errors(include_context=False)[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "configuration"
        raise OmnigentError(
            f"Invalid project config field {field!r}: {first['msg']}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc

    if getattr(resolved, "agent_id", None) is None and "agent_id" in body.__class__.model_fields:
        raise OmnigentError("agent_id is required", code=ErrorCode.INVALID_INPUT)
    if getattr(resolved, "git", None) is not None and getattr(resolved, "host_id", None) is None:
        raise OmnigentError(
            "git worktree creation requires host_id",
            code=ErrorCode.INVALID_INPUT,
        )

    if not warn_on_mismatch:
        return ProjectCreateResolution(body=resolved, project_id=project_id)

    warnings: list[dict[str, str]] = []
    explicit_agent_id = getattr(body, "agent_id", None) if "agent_id" in fields_set else None
    pinned_agent_id = config.get("agent_id")
    if explicit_agent_id and pinned_agent_id and explicit_agent_id != pinned_agent_id:
        warnings.append(
            {
                "code": "project_agent_mismatch",
                "message": "Explicit agent_id differs from the project's pinned agent",
            }
        )

    explicit_workspace = getattr(body, "workspace", None) if "workspace" in fields_set else None
    configured_workspace = config.get("workspace")
    if (
        isinstance(explicit_workspace, str)
        and isinstance(configured_workspace, str)
        and not _workspace_within(explicit_workspace, configured_workspace)
    ):
        warnings.append(
            {
                "code": "project_workspace_mismatch",
                "message": "Explicit workspace is outside the project's configured workspace root",
            }
        )

    for warning in warnings:
        _logger.warning(
            "project-aware session create warning project_id=%s code=%s: %s",
            project_id,
            warning["code"],
            warning["message"],
        )
    if warnings and _strict_project_create_enabled():
        raise OmnigentError(
            "Project session create mismatch: " + "; ".join(w["message"] for w in warnings),
            code=ErrorCode.INVALID_INPUT,
        )
    return ProjectCreateResolution(body=resolved, project_id=project_id, warnings=tuple(warnings))


# Claude Code's ``--permission-mode`` launch vocabulary — every value the CLI
# accepts at start, not just the shift+tab-switchable subset. ``dontAsk`` and
# ``bypassPermissions`` are launch-only (rejected on a running-session PATCH),
# but a scheduled task launches a fresh session each fire, so all of them are
# valid here. Mirrors the frontend's ``CLAUDE_NATIVE_PERMISSION_MODES``.
CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES: frozenset[str] = frozenset(
    {"default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
)


# The only harness that accepts a ``--permission-mode`` launch arg — the same
# ``permissionMode`` capability the web dialog gates its permission control on.
# Other native CLIs (codex / cursor / …) use different flags, so injecting
# ``--permission-mode`` there would be an unknown flag that breaks the launch.
_PERMISSION_MODE_HARNESS = "claude-native"


async def validate_permission_mode_agent_support(
    *,
    permission_mode: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
) -> None:
    """Reject a ``permission_mode`` on an agent whose harness has no such flag.

    Mirrors the web dialog's capability gate on the server so the REST endpoint
    and agent tools enforce the same rule the UI does: only a ``claude-native``
    agent may carry a ``permission_mode``. Without this, a task on a codex /
    cursor agent could persist a mode the fire path would inject as an unknown
    ``--permission-mode`` flag, breaking the launch.

    This is an early, friendly 4xx at persist time. A ``None`` mode is always
    allowed (nothing to gate). When the harness cannot be resolved (no bundle /
    cache / a load error), this is a no-op rather than a rejection: the value has
    already passed the vocabulary allowlist, and the fire path's launch-arg
    derivation is itself harness-gated fail-safe (it injects ``--permission-mode``
    ONLY for a confirmed ``claude-native`` agent, omitting it otherwise), so a
    non-Claude mode can never actually reach the launch args regardless.
    """
    if permission_mode is None or agent is None:
        return
    if agent_cache is None or getattr(agent, "bundle_location", None) is None:
        return
    from omnigent.harness_aliases import canonicalize_harness

    try:
        loaded = await asyncio.to_thread(agent_cache.load, agent.id, agent.bundle_location)
        executor = getattr(loaded.spec, "executor", None)
        raw_harness = None
        if executor is not None:
            raw_harness = executor.config.get("harness") or executor.type
        harness = canonicalize_harness(raw_harness) or raw_harness
    except Exception:
        # A spec that won't load fails elsewhere (workspace validation / fire);
        # don't turn an unrelated load error into a permission_mode rejection.
        _logger.exception("Failed to load agent spec for permission_mode gating")
        return
    if harness != _PERMISSION_MODE_HARNESS:
        raise OmnigentError(
            f"permission_mode is only supported for {_PERMISSION_MODE_HARNESS} agents, "
            f"not {harness!r}",
            code=ErrorCode.INVALID_INPUT,
        )


def validate_session_permission_mode(permission_mode: str | None) -> str | None:
    """Validate a persisted per-task permission mode shared by schedules.

    A scheduled task fires a fresh native session each run, so the whole launch
    vocabulary is allowed (including the launch-only ``dontAsk`` /
    ``bypassPermissions``). The value reaches the native CLI as the
    ``--permission-mode`` argv element the fire path derives, so reject anything
    outside the known set before a row persists it.
    """
    if permission_mode is None:
        return None
    if permission_mode not in CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES:
        raise OmnigentError(
            f"invalid permission_mode: {permission_mode!r} (expected one of "
            f"{sorted(CLAUDE_NATIVE_LAUNCH_PERMISSION_MODES)})",
            code=ErrorCode.INVALID_INPUT,
        )
    return permission_mode


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


def _require_absolute_host_workspace(workspace: str | None) -> str:
    """
    Enforce the shape checks shared by every host-workspace validation.

    :param workspace: Caller-supplied workspace, or ``None``.
    :returns: The workspace, guaranteed present and absolute.
    :raises OmnigentError: ``INVALID_INPUT`` when the workspace is
        missing or not an absolute path.
    """
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
    return workspace


async def _authorize_host_for_workspace(
    *,
    user_id: str | None,
    host_id: str,
    host_store: Any | None,
    host_registry: Any,
) -> str | None:
    """
    Authorize host ownership and classify a wrong-replica landing.

    Ownership runs FIRST — before any agent-spec load or the
    ``host.stat`` round-trip the caller performs next. A non-owner must
    be rejected (403/404 via the shared ``resolve_host_owner``) before
    we touch the host or even read the agent bundle (cross-user host
    probe).

    :param user_id: Authenticated caller, or ``None`` when auth is off.
    :param host_id: Target host id.
    :param host_store: Persistent host registrations; ``None`` skips
        the ownership check (minimal test wirings).
    :param host_registry: Live host tunnels on this replica.
    :returns: The host's display name for error messages, or ``None``.
    :raises OmnigentError: ``WRONG_REPLICA`` when the host is live but
        its tunnel is on another replica.
    """
    from omnigent.server.routes._host_launch import resolve_host_owner

    if host_store is None:
        return None
    host = await asyncio.to_thread(
        resolve_host_owner,
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
    )
    # Wrong-replica classification, same as the /v1/hosts/* endpoints and
    # RunnerRouter: validate_workspace does a local host_registry miss
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
            f"host {host.name or host_id!r} is on another replica; retry",
            code=ErrorCode.WRONG_REPLICA,
        )
    return host.name


async def _canonical_workspace_or_invalid_input(
    *,
    host_registry: Any,
    host_id: str,
    workspace: str,
    spec_cwd: str | None,
    host_name: str | None,
) -> str:
    """Run the seven-step validation, mapping failures to ``INVALID_INPUT``."""
    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        validate_workspace,
    )

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
    workspace = _require_absolute_host_workspace(workspace)
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

    host_name = await _authorize_host_for_workspace(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
        host_registry=host_registry,
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

    return await _canonical_workspace_or_invalid_input(
        host_registry=host_registry,
        host_id=host_id,
        workspace=workspace,
        spec_cwd=spec_cwd,
        host_name=host_name,
    )


async def validate_uploaded_bundle_host_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    spec_cwd: str | None,
    host_store: Any | None,
    host_registry: Any | None,
) -> str:
    """
    Validate a connected-host workspace for a bundle-upload create.

    The multipart ``POST /v1/sessions`` form carries the agent spec in
    the request itself, so — unlike
    :func:`validate_existing_host_workspace` — there is no registered
    agent row or cache entry to load the boundary from; the caller
    passes the freshly parsed spec's ``os_env.cwd`` directly. Shares
    every other check (shape, ownership-before-host-contact,
    wrong-replica classification, the seven-step host validation) so
    the two create forms cannot drift.

    :param user_id: Authenticated caller, or ``None`` when auth is off.
    :param host_id: Caller-supplied external host id.
    :param workspace: Caller-supplied absolute path on the host.
    :param spec_cwd: ``os_env.cwd`` from the uploaded bundle's spec,
        or ``None`` when the spec has no os_env block.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels on this replica.
    :returns: The canonical workspace path to persist on the session
        row.
    :raises OmnigentError: On any validation failure; ``INTERNAL_ERROR``
        when the server has no host registry.
    """
    workspace = _require_absolute_host_workspace(workspace)
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured on this server",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_name = await _authorize_host_for_workspace(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
        host_registry=host_registry,
    )
    return await _canonical_workspace_or_invalid_input(
        host_registry=host_registry,
        host_id=host_id,
        workspace=workspace,
        spec_cwd=spec_cwd,
        host_name=host_name,
    )
