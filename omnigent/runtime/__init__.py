"""Runtime package — public API for accessing runtime state.

Workflow code imports getter functions from here rather than
touching _globals directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omnigent.runtime import _globals
from omnigent.runtime.caps import RuntimeCaps

if TYPE_CHECKING:
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runner.routing import RunnerRouter
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
    from omnigent.stores import (
        AgentStore,
        ArtifactStore,
        ConversationStore,
        FileStore,
    )
    from omnigent.stores.canvas_store import CanvasStore
    from omnigent.stores.comment_store import CommentStore
    from omnigent.stores.policy_store import PolicyStore
    from omnigent.stores.push_subscription_store import PushSubscriptionStore
    from omnigent.stores.schedule_store import ScheduleStore
    from omnigent.stores.user_credential_store import UserCredentialStore
    from omnigent.stores.work_item_store import WorkItemStore
    from omnigent.terminals import TerminalRegistry
    from omnigent.tools import ToolManager


def init(
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    agent_cache: AgentCache,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    comment_store: CommentStore | None = None,
    policy_store: PolicyStore | None = None,
    schedule_store: ScheduleStore | None = None,
    work_item_store: WorkItemStore | None = None,
    canvas_store: CanvasStore | None = None,
    push_subscription_store: PushSubscriptionStore | None = None,
    user_credential_store: UserCredentialStore | None = None,
    caps: RuntimeCaps | None = None,
) -> None:
    """
    Initialize the runtime with store references.
    Called once at server startup before any workflows run.

    :param conversation_store: The ConversationStore instance
        for persisting conversation items.
    :param agent_store: The AgentStore instance for
        CRUD operations on registered agents.
    :param agent_cache: The AgentCache instance for
        loading and caching parsed agent specs.
    :param file_store: The FileStore instance for file
        metadata lookups during content resolution.
        ``None`` disables multimodal file_id resolution.
    :param artifact_store: The ArtifactStore instance for
        fetching file binary content during resolution.
        ``None`` disables multimodal file_id resolution.
    :param comment_store: The CommentStore instance for
        per-session review comments. ``None`` when comments
        are not configured (e.g. local dev or CLI mode).
    :param policy_store: The PolicyStore instance for
        session-scoped policies managed via the CRUD API.
        ``None`` when session policies are not configured.
    :param caps: Operator-configured execution ceiling.
        ``None`` uses :class:`RuntimeCaps` defaults.
    """
    _globals.init(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
        policy_store=policy_store,
        schedule_store=schedule_store,
        work_item_store=work_item_store,
        canvas_store=canvas_store,
        push_subscription_store=push_subscription_store,
        user_credential_store=user_credential_store,
        caps=caps,
    )


def get_conversation_store() -> ConversationStore:
    """
    Return the canonical ConversationStore instance.

    :returns: The ConversationStore set during :func:`init`.
    :raises RuntimeError: If the runtime has not been
        initialized.
    """
    store = _globals._conversation_store
    if store is None:
        raise RuntimeError("runtime not initialized — call init() first")
    return store


def get_agent_store() -> AgentStore:
    """
    Return the canonical AgentStore instance.

    :returns: The AgentStore set during :func:`init`.
    :raises RuntimeError: If the runtime has not been
        initialized.
    """
    store = _globals._agent_store
    if store is None:
        raise RuntimeError("runtime not initialized — call init() first")
    return store


def get_file_store() -> FileStore | None:
    """
    Return the FileStore instance, or ``None`` if not configured.

    Returns ``None`` (rather than raising) because file_store is
    optional — multimodal file_id resolution is simply skipped
    when no file store is available.

    :returns: The FileStore set during :func:`init`, or ``None``.
    """
    return _globals._file_store


def get_artifact_store() -> ArtifactStore | None:
    """
    Return the ArtifactStore instance, or ``None`` if not configured.

    Returns ``None`` (rather than raising) because artifact_store
    is optional — multimodal file_id resolution is simply skipped
    when no artifact store is available.

    :returns: The ArtifactStore set during :func:`init`, or ``None``.
    """
    return _globals._artifact_store


def get_comment_store() -> CommentStore | None:
    """
    Return the CommentStore instance, or ``None`` if not configured.

    Returns ``None`` (rather than raising) because comment_store is
    optional — comment tools surface a clear error to the agent when
    invoked without a configured store.

    :returns: The CommentStore set during :func:`init`, or ``None``.
    """
    return _globals._comment_store


def get_policy_store() -> PolicyStore | None:
    """
    Return the PolicyStore instance, or ``None`` if not configured.

    Returns ``None`` (rather than raising) because policy_store is
    optional — the policy engine falls back to spec-declared
    policies only when no store is available.

    :returns: The PolicyStore set during :func:`init`, or ``None``.
    """
    return _globals._policy_store


def get_work_item_store() -> WorkItemStore | None:
    """
    Return the WorkItemStore instance, or ``None`` if not configured.

    Returns ``None`` (rather than raising) because work_item_store is
    optional — the work-item tools surface a clear error to the agent
    when invoked without a configured store.

    :returns: The WorkItemStore set during :func:`init`, or ``None``.
    """
    return _globals._work_item_store


def get_schedule_store() -> ScheduleStore | None:
    """
    Return the ScheduleStore instance, or ``None`` if not configured.

    Returns ``None`` (rather than raising) because schedule_store is
    optional — the scheduler tools surface a clear error to the agent
    when invoked without a configured store.

    :returns: The ScheduleStore set during :func:`init`, or ``None``.
    """
    return _globals._schedule_store


def get_canvas_store() -> CanvasStore | None:
    """
    Return the CanvasStore instance, or ``None`` if not configured.

    Returns ``None`` (rather than raising) because canvas_store is optional —
    the ``set_canvas`` tool surfaces a clear error when invoked without it.

    :returns: The CanvasStore set during :func:`init`, or ``None``.
    """
    return _globals._canvas_store


def get_push_subscription_store() -> PushSubscriptionStore | None:
    """
    Return the PushSubscriptionStore instance, or ``None`` if not configured.

    Returns ``None`` (rather than raising) because Web Push is optional — the
    sender no-ops and the REST endpoints 401/404 when it's absent.

    :returns: The PushSubscriptionStore set during :func:`init`, or ``None``.
    """
    return _globals._push_subscription_store


def get_user_credential_store() -> UserCredentialStore | None:
    """
    Return the UserCredentialStore (per-user secret vault), or ``None``.

    Returns ``None`` (rather than raising) because the vault is optional — the
    REST endpoints 404 and :func:`resolve_user_secret` returns ``None`` when
    it's absent.

    :returns: The UserCredentialStore set during :func:`init`, or ``None``.
    """
    return _globals._user_credential_store


def get_agent_cache() -> AgentCache:
    """
    Return the canonical AgentCache instance.

    :returns: The AgentCache set during :func:`init`.
    :raises RuntimeError: If the runtime has not been
        initialized.
    """
    cache = _globals._agent_cache
    if cache is None:
        raise RuntimeError("runtime not initialized — call init() first")
    return cache


def get_tool_manager() -> ToolManager:
    """
    Return the current workflow's ToolManager from the
    ContextVar. Must be called within a workflow that has
    set the tool manager.

    :returns: The ToolManager for the current workflow.
    :raises RuntimeError: If no ToolManager has been set for
        the current workflow context.
    """
    mgr = _globals._tool_manager_var.get()
    if mgr is None:
        raise RuntimeError("no ToolManager set for this workflow")
    return mgr


def set_tool_manager(mgr: ToolManager | None) -> None:
    """
    Set or clear the per-workflow ToolManager ContextVar.

    :param mgr: The ToolManager for the current workflow,
        or ``None`` to clear the binding (e.g. in a
        ``finally`` block after the workflow completes).
    """
    _globals._tool_manager_var.set(mgr)


def register_dispatch_capability(
    parent_task_id: str,
    capability: _globals.DispatchCapability,
) -> None:
    """
    Pin the parent agent workflow's dispatch state under
    ``parent_task_id`` so child tool dispatches
    invocations can locate it.

    See :class:`_globals.DispatchCapability` and
    ``designs/TOOL_DISPATCH_CHILD_WORKFLOWS.md`` for the why.

    :param parent_task_id: The parent agent workflow's
        ``task_id``, e.g. ``"resp_abc123"``.
    :param capability: The capability handle.
    """
    _globals._dispatch_capabilities[parent_task_id] = capability


def get_dispatch_capability(
    parent_task_id: str,
) -> _globals.DispatchCapability | None:
    """
    Look up a parent agent workflow's dispatch capability.

    :param parent_task_id: The parent's ``task_id`` exactly as it
        was passed to :func:`register_dispatch_capability`.
    :returns: The :class:`_globals.DispatchCapability`, or
        ``None`` when the parent is not registered (parent has
        already torn down, or the child is recovering on a
        different process — both surface to the caller as a
        clean dispatch error rather than a silent fallback).
    """
    return _globals._dispatch_capabilities.get(parent_task_id)


def unregister_dispatch_capability(parent_task_id: str) -> None:
    """
    Drop a parent agent workflow's dispatch capability.

    Idempotent — missing keys are silently ignored so the
    parent's ``finally`` block can call this regardless of
    whether registration ran (e.g. in early-fail paths).

    :param parent_task_id: The parent's ``task_id``.
    """
    _globals._dispatch_capabilities.pop(parent_task_id, None)


def get_caps() -> RuntimeCaps:
    """
    Return the runtime caps set during :func:`init`.

    :returns: The :class:`RuntimeCaps` instance. Always
        non-None (defaults are used if none were provided).
    """
    return _globals._caps


def get_terminal_registry() -> TerminalRegistry:
    """
    Return the server-resident tmux terminal registry.

    Constructed once by :func:`init` and shared across all
    workflows. Callers use ``registry.launch(...)``,
    ``registry.get(...)``, ``registry.list_for_conversation(...)``,
    ``registry.close(...)`` keyed on
    ``(conversation_id, terminal_name, session_key)``. See
    ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.2.

    :returns: The :class:`TerminalRegistry` set during
        :func:`init`.
    :raises RuntimeError: If the runtime has not been
        initialized.
    """
    reg = _globals._terminal_registry
    if reg is None:
        raise RuntimeError("runtime not initialized — call init() first")
    return reg


def get_resource_registry() -> SessionResourceRegistry | None:
    """Return the session resource registry, or ``None`` if not set.

    :returns: The :class:`SessionResourceRegistry` or ``None``.
    """
    return _globals._resource_registry


def set_resource_registry(registry: SessionResourceRegistry | None) -> None:
    """Set the session resource registry.

    :param registry: The registry instance, or ``None`` to clear.
    """
    _globals._resource_registry = registry


def set_runner_client(client: Any) -> None:
    """Set the legacy fixed runner httpx client.

    Production servers should use :func:`set_runner_router` so
    dispatch is resolved from conversation affinity. This hook remains
    for focused tests and older in-process setups that intentionally
    provide one fixed runner client.

    :param client: An ``httpx.AsyncClient`` pointed at the runner
        (any transport). ``None`` to disable runner routing.
    """
    _globals._runner_client = client


def get_runner_client() -> Any:
    """Return the runner client, or ``None`` if not configured."""
    return _globals._runner_client


def set_runner_router(router: RunnerRouter | None) -> None:
    """
    Set the conversation-aware runner router.

    Production Omnigent servers use this instead of a process-wide runner
    client so each dispatch resolves the correct runner from
    ``conversations.runner_id`` and the live tunnel registry.

    :param router: :class:`omnigent.runner.routing.RunnerRouter`,
        or ``None`` to disable routed runner dispatch.
    :returns: None.
    """
    _globals._runner_router = router


def get_runner_router() -> RunnerRouter | None:
    """
    Return the configured runner router, if any.

    :returns: :class:`omnigent.runner.routing.RunnerRouter` or
        ``None``.
    """
    return _globals._runner_router


def set_runner_ws_factory(factory: Any) -> None:
    """Set the WebSocket connect factory for the runner.

    The factory is a callable ``(path: str) -> AsyncContextManager``
    that yields a connected :mod:`websockets` client connection.
    Used by the terminal-attach proxy to bridge frames to the
    runner's WS attach endpoint when the runner runs out-of-process.

    Leave unset (``None``) for in-process runners. The server's
    terminal-attach route falls back to the shared in-process
    terminal registry, which lives in the same memory as the
    runner-side one in that mode.
    """
    _globals._runner_ws_factory = factory


def get_runner_ws_factory() -> Any:
    """Return the runner WebSocket factory, or ``None`` if unset."""
    return _globals._runner_ws_factory


def set_runner_id(runner_id: str | None) -> None:
    """Set the stable runner UUID for conversation affinity."""
    _globals._runner_id = runner_id


def get_runner_id() -> str | None:
    """Return the stable runner UUID, or ``None`` if not set."""
    return _globals._runner_id


def set_harness_process_manager(manager: HarnessProcessManager | None) -> None:
    """
    Set the AP-wide :class:`HarnessProcessManager` singleton.

    Called once by the FastAPI lifespan in
    ``omnigent/server/app.py`` after ``HarnessProcessManager.start()``.
    Workflows access the manager via
    :func:`get_harness_process_manager`.

    :param manager: The instance to set, or ``None`` to clear
        (e.g. on lifespan teardown).
    """
    _globals._harness_process_manager = manager


def get_harness_process_manager() -> HarnessProcessManager:
    """
    Return the AP-wide :class:`HarnessProcessManager`.

    :returns: The manager instance set by the FastAPI lifespan.
    :raises RuntimeError: If the lifespan startup hasn't run yet
        (e.g. workflow invoked before Omnigent boot completed, or in a
        unit-test setting that didn't call
        :func:`set_harness_process_manager`).
    """
    pm = _globals._harness_process_manager
    if pm is None:
        raise RuntimeError(
            "HarnessProcessManager not initialized — Omnigent lifespan startup "
            "must call set_harness_process_manager() before any workflow "
            "dispatches to a non-default harness"
        )
    return pm
