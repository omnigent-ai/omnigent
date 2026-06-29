"""Private module-level state for the runtime.

Never import this module outside of omnigent.runtime.
Use the public getter functions in runtime/__init__.py instead.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
    from omnigent.stores.work_item_store import WorkItemStore
    from omnigent.terminals import TerminalRegistry
    from omnigent.tools import ToolManager
    from omnigent.tools.base import ToolContext

_conversation_store: ConversationStore | None = None
_agent_store: AgentStore | None = None
_agent_cache: AgentCache | None = None
_file_store: FileStore | None = None
_artifact_store: ArtifactStore | None = None
_comment_store: CommentStore | None = None
_policy_store: PolicyStore | None = None
_schedule_store: ScheduleStore | None = None
_work_item_store: WorkItemStore | None = None
_canvas_store: CanvasStore | None = None
_push_subscription_store: PushSubscriptionStore | None = None
_caps: RuntimeCaps = RuntimeCaps()

# Server-resident tmux terminal registry. Initialized in
# :func:`init` and accessed via ``get_terminal_registry()`` in
# ``omnigent.runtime``. Conversation-scoped: one entry per
# ``(conversation_id, terminal_name, session_key)`` triple, instances
# persist across task workflows within a conversation. See
# ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.2.
_terminal_registry: TerminalRegistry | None = None
_resource_registry: SessionResourceRegistry | None = None

# AP-wide harness process manager. Initialized by the FastAPI
# lifespan in ``omnigent/server/app.py``; accessed via
# ``get_harness_process_manager()`` in ``omnigent.runtime``.
# See §Step 5b in designs/SERVER_HARNESS_CONTRACT.md /
# Autonomous decisions.
_harness_process_manager: HarnessProcessManager | None = None

# Phase 5 runner client. When set (via ``set_runner_client()`` in
# ``omnigent.runtime``), the runner routes
# ALL harness-backed executors through the runner instead of spawning
# harness subprocesses directly. ``None`` (default) preserves the
# existing direct-to-harness path. See ``designs/RUNNER.md`` §7
# Phase 5.
_runner_client: Any | None = None  # httpx.AsyncClient; typed as Any to avoid import

# Conversation-aware runner router. The production server sets this
# during lifespan startup; workflows use it to resolve the runner for
# each conversation instead of mutating a process-wide "active runner"
# when tunnels connect.
_runner_router: RunnerRouter | None = None

# Runner WebSocket connect factory. Set by the server lifespan when
# the runner is reachable over a local Unix domain socket;
# ``None`` when no runner is configured *or* when the runner
# lives in the same process (registry is shared via
# ``create_runner_app(terminal_registry=...)``, so the local fallback
# in :mod:`omnigent.server.routes.terminal_attach` produces the
# same result). The factory takes the runner-side path (``str``) and
# returns an async context manager yielding a connected
# :mod:`websockets` client connection. Used by the server's
# terminal-attach proxy to bridge xterm.js frames to the runner WS.
_runner_ws_factory: Any | None = None  # Callable[[str], AsyncContextManager[ClientConnection]]

# Optional fixed runner id for the legacy/test direct runner-client
# path. Production dispatch uses _runner_router and conversation
# affinity instead of this process-wide id.
_runner_id: str | None = None

# Per-workflow tool manager. ContextVar ensures thread-safe isolation
# across concurrent workflow tasks.
_tool_manager_var: ContextVar[ToolManager | None] = ContextVar(
    "_tool_manager",
    default=None,
)


@dataclass
class DispatchCapability:
    """
    Process-local handle the parent agent workflow registers so
    children spawned by the runner's tool dispatcher can locate
    the parent's runtime state without re-building it.

    Each parent workflow registers ONE :class:`DispatchCapability`
    keyed on its ``task_id`` at workflow start, and unregisters in
    its ``finally``. Concurrent action_required dispatches spawn
    independent child workflows (per
    ``designs/TOOL_DISPATCH_CHILD_WORKFLOWS.md``); each child looks
    up the parent's capability by ``parent_task_id``, executes the
    server-side tool through the parent's :class:`ToolManager`, and
    PATCHes the result back to the harness.

    Why a process-local registry instead of a ContextVar:
    children run in their own asyncio task contexts. ContextVars are
    scoped to the asyncio task / thread that set them, so a child's
    context does not inherit the parent's :data:`_tool_manager_var`
    binding. Pinning the capability under the parent's ``task_id``
    lets the child resolve it explicitly via
    :func:`get_dispatch_capability`.

    :param tool_mgr: The parent's :class:`ToolManager`. Children
        invoke ``tool_mgr.call_tool(name, args, tool_ctx)``
        on a worker thread to execute the tool.
    :param tool_ctx: The :class:`ToolContext` the parent built for
        its own tool invocations. Reused verbatim by children so
        ``ctx.workspace`` / ``ctx.conversation_id`` agree with what
        the harness/LLM saw.
    :param policy_engine: The per-workflow policy engine (``Any``
        because the import would create a cycle here). Reserved
        for the followup that wires TOOL_CALL enforcement into the
        action_required path; currently held for forward
        compatibility.
    :param conversation_id: The conversation the parent runs under,
        e.g. ``"conv_abc123"``. Used by children when they need to
        look up the harness UDS socket via
        :class:`HarnessProcessManager`.
    :param root_task_id: The root task id when the parent is itself
        a sub-agent, ``None`` for top-level parents. Threaded
        through so children can route SSE / signal correctly.
    :param agent_name: The parent's agent name, e.g.
        ``"databricks_coding_agent"``. Used for telemetry and
        log lines on the child side.
    """

    tool_mgr: ToolManager
    tool_ctx: ToolContext
    policy_engine: Any
    conversation_id: str
    root_task_id: str | None
    agent_name: str


# Per-task dispatch-capability registry. The parent agent workflow
# registers itself at workflow start so children can find it via
# ``get_dispatch_capability(parent_task_id)``. Plain dict (not a
# ContextVar) because lookups happen from a different asyncio task
# context than the one that registered the entry. Process-local — a
# child workflow that recovers on a different process will not find
# the entry and must surface a clear error rather than silently
# dispatching on stale state.
_dispatch_capabilities: dict[str, DispatchCapability] = {}


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
    caps: RuntimeCaps | None = None,
) -> None:
    """
    Set the runtime's store references. Called once at server
    startup.

    :param conversation_store: The ConversationStore instance
        for persisting conversation items.
    :param agent_store: The AgentStore instance for CRUD
        operations on registered agents.
    :param agent_cache: The AgentCache instance for loading
        and caching parsed agent specs.
    :param file_store: The FileStore instance for file
        metadata lookups during content resolution.
        ``None`` disables multimodal file_id resolution.
    :param artifact_store: The ArtifactStore instance for
        fetching file binary content during content
        resolution. ``None`` disables multimodal file_id
        resolution.
    :param comment_store: The CommentStore instance for
        per-session review comments. ``None`` when comments
        are not configured (e.g. local dev or CLI mode);
        comment tools return an error when invoked without
        a store.
    :param policy_store: The PolicyStore instance for
        session-scoped policies managed via the CRUD API.
        ``None`` when session policies are not configured;
        the policy engine will only use spec-declared
        policies.
    :param caps: Operator-configured execution ceiling.
        ``None`` uses :class:`RuntimeCaps` defaults.
    """
    from omnigent.terminals import TerminalRegistry

    global _conversation_store, _agent_store
    global _agent_cache, _file_store, _artifact_store, _caps
    global _terminal_registry, _comment_store, _policy_store
    global _schedule_store, _work_item_store, _canvas_store, _push_subscription_store
    _conversation_store = conversation_store
    _agent_store = agent_store
    _agent_cache = agent_cache
    _file_store = file_store
    _artifact_store = artifact_store
    _comment_store = comment_store
    _policy_store = policy_store
    _schedule_store = schedule_store
    _work_item_store = work_item_store
    _canvas_store = canvas_store
    _push_subscription_store = push_subscription_store
    _caps = caps if caps is not None else RuntimeCaps()
    # Tmux terminal registry: server-resident, conversation-scoped
    # ``inner.terminal.TerminalInstance`` map. See
    # ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.2 for the design.
    # Sandbox enforcement is per-terminal (declared on the spec's
    # ``TerminalEnvSpec.os_env.sandbox``), not a registry-wide
    # toggle — different terminals in the same agent can have
    # different sandbox policies.
    _terminal_registry = TerminalRegistry()
