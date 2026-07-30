"""Session resource domain entities and shared constants.

The Sessions Resource API exposes runner-owned resources (logical OS
environments, terminals, and later files) as stable handles under a
session/conversation.  These entities live outside the server API schema
module so both the server and runner can share ids and projection helpers
without creating an API-layer import cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from omnigent.entities.pagination import PagedList
from omnigent.terminals.registry import TerminalListEntry

if TYPE_CHECKING:
    from omnigent.terminals.registry import TerminalRegistry

DEFAULT_ENVIRONMENT_ID = "default"

ResourceType = Literal["environment", "terminal", "file"]

_SAFE_RESOURCE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_resource_component(value: str) -> str:
    """Return a deterministic URL/id-safe component for resource ids.

    The public API treats resource ids as opaque, but Phase 1a uses
    deterministic ids so tests and clients can see stable handles before
    durable resource-id generation exists.  Preserve common readable id
    characters and collapse every other run to ``_``.

    :param value: Raw component, e.g. a terminal name or session key.
    :returns: Sanitized non-empty component.
    """
    stripped = value.strip()
    safe = _SAFE_RESOURCE_COMPONENT_RE.sub("_", stripped).strip("_")
    return safe or "resource"


def terminal_resource_id(terminal_name: str, session_key: str) -> str:
    """Build the deterministic Phase-1 terminal resource id."""
    return (
        f"terminal_{safe_resource_component(terminal_name)}_{safe_resource_component(session_key)}"
    )


def terminal_environment_resource_id(terminal_name: str, session_key: str) -> str:
    """Build the deterministic Phase-1 terminal environment resource id."""
    return (
        f"env_terminal_{safe_resource_component(terminal_name)}_"
        f"{safe_resource_component(session_key)}"
    )


@dataclass(frozen=True)
class SessionResourceView:
    """Runner/server-neutral view of one session resource."""

    id: str
    type: ResourceType
    session_id: str
    name: str
    metadata: dict[str, object] = field(default_factory=dict)
    environment: str | None = None


def environment_safety_metadata(os_env_spec: Any | None) -> dict[str, object]:
    """Derive descriptive share-safety metadata from an OS environment spec.

    Duck-typed (``.type`` / ``.sandbox.type``) to avoid importing the inner
    datamodel. Returns ``{}`` for ``None`` so spec-less callers keep the
    legacy projection. Makes no product-policy judgement about shareability.

    :param os_env_spec: An ``OSEnvSpec`` or ``None``.
    :returns: ``environment_type`` / ``sandbox_type`` / ``sandbox_active``,
        or ``{}`` when *os_env_spec* is ``None``.
    """
    if os_env_spec is None:
        return {}
    sandbox = getattr(os_env_spec, "sandbox", None)
    sandbox_type = getattr(sandbox, "type", "none") if sandbox is not None else "none"
    # sandbox_active=False (type="none"/no sandbox) means shell/terminal access
    # is unconfined even though the filesystem API still enforces the root.
    sandbox_active = sandbox is not None and sandbox_type != "none"
    return {
        "environment_type": getattr(os_env_spec, "type", "caller_process"),
        "sandbox_type": sandbox_type,
        "sandbox_active": sandbox_active,
    }


def default_environment_resource(
    session_id: str,
    os_env_spec: Any | None = None,
) -> SessionResourceView:
    """Return the logical primary/default environment resource.

    When *os_env_spec* is provided its share-safety metadata is merged in;
    when ``None`` the legacy ``caller_process`` projection is preserved.

    :param session_id: Owning session/conversation id.
    :param os_env_spec: Optional ``OSEnvSpec`` for the primary environment.
    """
    metadata: dict[str, object] = {
        "environment_type": "caller_process",
        "role": "primary",
    }
    metadata.update(environment_safety_metadata(os_env_spec))
    return SessionResourceView(
        id=DEFAULT_ENVIRONMENT_ID,
        type="environment",
        session_id=session_id,
        name="Primary environment",
        metadata=metadata,
    )


def _terminal_environment_id_for_entry(entry: TerminalListEntry) -> str:
    """Return the environment id the terminal actually uses.

    Phase 1a adapts the existing TerminalRegistry. Terminals that have a
    distinct ``instance.os_env`` get a separate terminal environment
    resource. If no distinct OS environment is attached, the terminal is
    treated as running in the primary environment.
    """
    if entry.instance.os_env is None:
        return DEFAULT_ENVIRONMENT_ID
    return terminal_environment_resource_id(entry.terminal_name, entry.session_key)


def terminal_resource_view(session_id: str, entry: TerminalListEntry) -> SessionResourceView:
    """Build the :class:`SessionResourceView` for one running terminal.

    Public so that the ``sys_terminal_launch`` tool can construct the
    same record the list/POST endpoints produce when emitting a
    ``session.resource.created`` event after a successful launch.

    :param session_id: Owning session/conversation id,
        e.g. ``"conv_abc123"``.
    :param entry: The terminal registry entry to project.
    :returns: The :class:`SessionResourceView` for *entry*.
    """
    terminal_name = entry.terminal_name
    session_key = entry.session_key
    return SessionResourceView(
        id=terminal_resource_id(terminal_name, session_key),
        type="terminal",
        session_id=session_id,
        name=f"{terminal_name}:{session_key}",
        environment=_terminal_environment_id_for_entry(entry),
        metadata={
            "terminal_name": terminal_name,
            "session_key": session_key,
            "running": entry.instance.running,
            "tmux_socket": str(entry.instance.socket_path),
            "tmux_target": entry.instance.tmux_target,
            # Effective web-attach transport (``"pty"`` / ``"control"``) absent
            # a per-attach ``?transport=`` override, so the browser can pick
            # the matching mouse/selection behavior. Control mode lets xterm
            # own scrollback + selection; PTY mode still needs the modifier
            # workarounds + hint bar.
            "terminal_transport": _resolve_transport_for_view(entry),
        },
    )


def _resolve_transport_for_view(entry: TerminalListEntry) -> str:
    """Resolve a terminal's default web-attach transport for metadata.

    Mirrors :func:`omnigent.inner.terminal.resolve_terminal_transport` with no
    per-attach override — the spec's declared transport, else the global
    default. Imported lazily to keep this projection import-light.

    :param entry: The terminal registry entry to project.
    :returns: ``"pty"`` or ``"control"``.
    """
    from omnigent.inner.terminal import resolve_terminal_transport

    return resolve_terminal_transport(spec_transport=entry.instance.terminal_transport)


def _terminal_environment_resource(
    session_id: str,
    entry: TerminalListEntry,
) -> SessionResourceView | None:
    os_env = entry.instance.os_env
    if os_env is None:
        return None

    metadata: dict[str, object] = {
        "environment_type": os_env.spec.type,
        "role": "terminal",
        "terminal_name": entry.terminal_name,
        "session_key": entry.session_key,
    }
    if os_env.cwd is not None:
        metadata["root"] = str(os_env.cwd)
    if os_env.spec.sandbox is not None:
        metadata["sandbox_type"] = os_env.spec.sandbox.type

    return SessionResourceView(
        id=terminal_environment_resource_id(entry.terminal_name, entry.session_key),
        type="environment",
        session_id=session_id,
        name=f"Environment for {entry.terminal_name}:{entry.session_key}",
        metadata=metadata,
    )


def session_resource_view_to_dict(resource: SessionResourceView) -> dict[str, object]:
    """Return the public JSON-compatible resource object shape."""
    payload: dict[str, object] = {
        "id": resource.id,
        "object": "session.resource",
        "type": resource.type,
        "session_id": resource.session_id,
        "name": resource.name,
        "metadata": resource.metadata,
    }
    if resource.environment is not None:
        payload["environment"] = resource.environment
    return payload


def list_session_resources_from_terminal_registry(
    session_id: str,
    terminal_registry: TerminalRegistry | None,
    *,
    has_os_env: bool = True,
    primary_os_env_spec: Any | None = None,
) -> PagedList[SessionResourceView]:
    """Build the Phase-1a session resource inventory.

    Includes the logical default environment plus running terminal
    resources and their distinct terminal environment resources (when a
    terminal has a backing ``os_env``).  Files, typed collections, and
    environment filesystem entries are intentionally out of scope for
    Phase 1a.

    :param session_id: Owning session/conversation id,
        e.g. ``"conv_abc123"``.
    :param terminal_registry: Terminal registry to scan for running
        terminals.  ``None`` skips terminal enumeration.
    :param has_os_env: When ``False``, the logical default environment
        resource is omitted because the agent spec has no ``os_env``
        configured.  Defaults to ``True`` so callers that do not
        pass an agent spec see the normal primary environment entry.
    :param primary_os_env_spec: Optional ``OSEnvSpec`` for the primary
        environment, used to enrich the default environment resource with
        share-safety metadata.  ``None`` preserves the legacy projection.
    :returns: :class:`PagedList` of :class:`SessionResourceView` items.
    """
    resources: list[SessionResourceView] = []
    if has_os_env:
        resources.append(default_environment_resource(session_id, primary_os_env_spec))
    if terminal_registry is not None:
        entries = [
            entry
            for entry in terminal_registry.list_for_conversation(session_id)
            if entry.instance.running
        ]
        for entry in entries:
            resources.append(terminal_resource_view(session_id, entry))
            terminal_env = _terminal_environment_resource(session_id, entry)
            if terminal_env is not None:
                resources.append(terminal_env)

    return PagedList(
        data=resources,
        first_id=resources[0].id if resources else None,
        last_id=resources[-1].id if resources else None,
        has_more=False,
    )


def get_resource_by_id(
    page: PagedList[SessionResourceView],
    resource_id: str,
) -> SessionResourceView | None:
    """Find a single resource by id from a pre-built inventory.

    :param page: Resource inventory from
        :func:`list_session_resources_from_terminal_registry`.
    :param resource_id: Opaque resource id to look up,
        e.g. ``"default"`` or ``"terminal_bash_s1"``.
    :returns: The matching resource or ``None``.
    """
    for resource in page.data:
        if resource.id == resource_id:
            return resource
    return None


def filter_resources_by_type(
    page: PagedList[SessionResourceView],
    resource_type: ResourceType,
) -> PagedList[SessionResourceView]:
    """Return only resources matching *resource_type*.

    :param page: Full resource inventory.
    :param resource_type: One of ``"environment"``, ``"terminal"``,
        or ``"file"``.
    :returns: Filtered :class:`PagedList` with standard cursor fields.
    """
    filtered = [r for r in page.data if r.type == resource_type]
    return PagedList(
        data=filtered,
        first_id=filtered[0].id if filtered else None,
        last_id=filtered[-1].id if filtered else None,
        has_more=False,
    )


def resolve_terminal_entry_by_resource_id(
    session_id: str,
    terminal_id: str,
    terminal_registry: TerminalRegistry | None,
) -> TerminalListEntry | None:
    """Find the :class:`TerminalListEntry` matching a terminal resource id.

    Scans the registry for the session and computes resource ids to
    match.  Returns ``None`` if no running terminal matches.

    :param session_id: Owning session/conversation id.
    :param terminal_id: Opaque terminal resource id,
        e.g. ``"terminal_bash_s1"``.
    :param terminal_registry: The runner's terminal registry.
    :returns: The matching entry or ``None``.
    """
    if terminal_registry is None:
        return None
    for entry in terminal_registry.list_for_conversation(session_id):
        if not entry.instance.running:
            continue
        if terminal_resource_id(entry.terminal_name, entry.session_key) == terminal_id:
            return entry
    return None
