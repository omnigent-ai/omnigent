"""Wire schemas for the omnigent server's SSE stream events.

These Pydantic models are the single source of truth for every event a
client can receive on the session and response SSE endpoints
(:data:`ServerStreamEvent`) and the response objects those events carry.
They live in ``omnigent-client`` so the client can validate streamed
envelopes without installing the ``omnigent`` server package;
:mod:`omnigent.server.schemas` re-exports them for server code.

The module depends only on Pydantic and the standard library. Do not
import ``omnigent`` here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Sentinel ``Origin`` header that the project's own non-browser clients
# (the SDK, runner -> server tunnel, host/daemon -> server tunnel,
# terminal-attach) send so the server's CSWSH / CSRF origin guards allow
# them. It is part of the client/server handshake contract, so it lives
# with the wire schemas; ``omnigent.runner.identity`` re-exports it. The
# non-HTTP scheme is deliberate: a browser computes ``Origin`` from the
# page URL and can never emit this value.
OMNIGENT_INTERNAL_WS_ORIGIN = "omnigent://internal"


class ConversationRef(BaseModel):
    """
    Lightweight reference to a conversation, used in request and
    response bodies where only the conversation ID is needed.

    :param id: Conversation identifier, e.g. ``"conv_abc123"``.
    """

    id: str


class UsageDetails(BaseModel):
    """
    Breakdown of output token usage.

    :param reasoning_tokens: Number of tokens consumed by
        chain-of-thought reasoning.
    """

    reasoning_tokens: int = 0


class Usage(BaseModel):
    """
    Token usage statistics for a response.

    :param input_tokens: Number of input (prompt) tokens consumed.
    :param output_tokens: Number of output (completion) tokens
        generated.
    :param output_tokens_details: Breakdown of output token usage
        (e.g. reasoning tokens).
    :param total_tokens: Sum of input and output tokens across all
        LLM sub-calls for this turn (billing total).
    :param context_tokens: Context-fill estimate for the next turn —
        set only by executors that make multiple LLM sub-calls per
        turn (e.g. ``openai-agents``).  For single-call executors
        this is absent and ``total_tokens`` serves the same purpose.
        The toolbar context ring and ``/context`` command use this
        field when present, falling back to ``total_tokens``.
    :param cache_read_input_tokens: Prompt tokens served from a
        provider prompt cache (cache hit), billed at a reduced rate.
        Reported by Anthropic-style providers as a count *separate*
        from ``input_tokens`` (which carries only the non-cached
        portion); ``0`` when the provider does not break out cache
        usage. Consumed by the cache-aware server-side cost path.
    :param cache_creation_input_tokens: Prompt tokens written to the
        provider prompt cache (cache creation), billed at a premium
        rate. Like ``cache_read_input_tokens``, this is separate from
        ``input_tokens``; ``0`` when not reported.
    :param model: The LLM model the harness actually used for this
        turn, e.g. ``"claude-opus-4-8"`` or ``"databricks-gpt-5-5"``.
        Reported by relay executors so the server-side cost path can
        price the turn even when the agent spec pins no ``llm.model``
        (e.g. supervisors that delegate / use the harness default).
        ``None`` when the executor doesn't report it; the cost path
        then falls back to the session override / spec model.
    :param cost_usd: Authoritative per-turn cost in USD reported
        directly by the harness/provider (e.g. GitHub Copilot's
        AI-credit total). When present, the server-side cost path uses
        it in preference to the catalog token-price estimate; ``None``
        when the harness doesn't report a cost (the common case, where
        cost is computed from token counts x catalog pricing).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    output_tokens_details: UsageDetails = Field(default_factory=UsageDetails)
    total_tokens: int = 0
    context_tokens: int | None = None
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model: str | None = None
    cost_usd: float | None = None


class ErrorDetail(BaseModel):
    """
    Machine-readable error information attached to a failed response.

    :param code: Error code string, e.g. ``"server_error"``,
        ``"invalid_input"``.
    :param message: Human-readable error description. Always populated; older
        clients render this verbatim.
    :param title: Optional short headline naming what went wrong, e.g.
        ``"Claude Code can't run as root"``. Present when the runner
        recognized the failure (see ``omnigent.runner.launch_failure``); lets
        the UI show a clear card title instead of the raw ``code``.
    :param cause: Optional one/two-sentence explanation of why it failed.
        Paired with ``title``.
    :param remediation: Optional concrete next step to fix it, e.g. a command
        to run. ``None`` when there is no single clear fix.
    :param undelivered: ``True`` when the harness reports it never received the
        message this turn carried (it failed before delivery), so the sender's
        queued copy is the only record of it; absent otherwise.
    """

    code: str
    message: str
    title: str | None = None
    cause: str | None = None
    remediation: str | None = None
    undelivered: bool | None = None


class IncompleteDetails(BaseModel):
    """
    Details explaining why a response is incomplete.

    :param reason: Reason the response stopped early, e.g.
        ``"max_output_tokens"``, ``"max_tool_calls"``.
    """

    reason: str


class ResponseObject(BaseModel):
    """
    API representation of a response (task execution result).

    :param id: Unique response identifier, e.g.
        ``"resp_abc123"``.
    :param object: Fixed resource type, always ``"response"``.
    :param status: Lifecycle status, one of ``"queued"``,
        ``"in_progress"``, ``"completed"``, ``"failed"``,
        ``"incomplete"``, ``"cancelled"``.
    :param model: Agent name that produced this response,
        e.g. ``"research-agent"``.
    :param created_at: Unix epoch timestamp of creation.
    :param completed_at: Unix epoch timestamp of completion, or
        ``None`` if not yet complete.
    :param output: Heterogeneous output items (messages,
        reasoning, function_calls) serialized as dicts; shape
        varies by item type. Empty for non-completed responses.
    :param background: Whether this response was created as a
        background task.
    :param store: Whether this response is persisted. Always
        ``True``.
    :param usage: Token usage statistics, or ``None`` if not
        yet available.
    :param previous_response_id: ID of the prior response in
        the conversation thread, or ``None`` for the first turn.
    :param conversation: Reference to the owning conversation.
    :param instructions: Per-request system instructions composed
        additively with the agent's own, or ``None``.
    :param reasoning: Reasoning configuration,
        e.g. ``{"effort": "medium"}``.
    :param error: Error details if the response failed.
    :param incomplete_details: Details if the response is
        incomplete (e.g. hit token limit).
    """

    id: str
    object: str = "response"
    status: str
    model: str
    created_at: int
    completed_at: int | None = None
    # Heterogeneous output items (messages, reasoning, function_calls);
    # shape varies by item type.
    output: list[dict[str, Any]] = Field(default_factory=list)
    background: bool = False
    store: bool = True
    usage: Usage | None = None
    previous_response_id: str | None = None
    conversation: ConversationRef | None = None
    instructions: str | None = None
    reasoning: dict[str, str] | None = None
    error: ErrorDetail | None = None
    incomplete_details: IncompleteDetails | None = None


class FailedResponseObject(BaseModel):
    """Response payload for failures raised before response allocation.

    Transport and setup failures can terminate a turn before the harness has
    assigned the metadata required by :class:`ResponseObject`. The remaining
    fields mirror that model so fully allocated failures retain their complete
    wire representation.

    :param id: Unique response identifier, e.g. ``"resp_abc123"``, or
        ``None`` when allocation did not complete.
    :param object: Fixed resource type, always ``"response"``.
    :param status: Lifecycle status, normally ``"failed"``.
    :param model: Agent name that produced the response, e.g.
        ``"research-agent"``, or ``None`` when resolution did not complete.
    :param created_at: Unix epoch timestamp of creation, or ``None`` when the
        response failed before creation.
    :param completed_at: Unix epoch timestamp of completion, or ``None``.
    :param output: Heterogeneous serialized output items accumulated before
        failure.
    :param background: Whether the response was created as a background task.
    :param store: Whether the response is persisted.
    :param usage: Token usage statistics, or ``None`` when unavailable.
    :param previous_response_id: ID of the prior response, or ``None``.
    :param conversation: Reference to the owning conversation, or ``None``.
    :param instructions: Per-request instructions override, or ``None``.
    :param reasoning: Reasoning configuration, or ``None``.
    :param error: Error details describing the failure, or ``None``.
    :param incomplete_details: Incomplete-response details, or ``None``.
    """

    id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    object: str = "response"
    status: str
    model: str | None = Field(default=None, exclude_if=lambda value: value is None)
    created_at: int | None = Field(default=None, exclude_if=lambda value: value is None)
    completed_at: int | None = None
    output: list[dict[str, Any]] = Field(default_factory=list)
    background: bool = False
    store: bool = True
    usage: Usage | None = None
    previous_response_id: str | None = None
    conversation: ConversationRef | None = None
    instructions: str | None = None
    reasoning: dict[str, str] | None = None
    error: ErrorDetail | None = None
    incomplete_details: IncompleteDetails | None = None


# Stages of a managed-sandbox launch, in pipeline order: the sandbox
# is provisioned, the repository workspace is cloned into it (skipped
# when the session has no repo workspace), the in-sandbox host starts
# and registers, and the agent runner is launched on it. ``ready`` and
# ``failed`` are terminal. ``action_required`` is a provisioning-time
# warning: the user needs to do what the status's ``details`` say.
SandboxLaunchStage = Literal[
    "provisioning",
    "action_required",
    "cloning",
    "starting",
    "connecting",
    "ready",
    "failed",
]


class ModelUsage(BaseModel):
    """
    Cumulative token/cost usage attributed to a single LLM model.

    One value in the ``usage_by_model`` map on :class:`SessionResponse` /
    :class:`SessionUsageEvent`, keyed by the raw harness-reported model id
    (e.g. ``"claude-sonnet-4-6"``, ``"databricks-gpt-5-5"``). Counts are
    summed over the session's subtree (itself + sub-agent descendants), so a
    parent folds in sub-agents that ran a different model. Token buckets
    mirror the flat per-session breakdown.

    :param input_tokens: Cumulative non-cached input (prompt) tokens for this
        model over the subtree, e.g. ``12000``. ``None`` when not recorded.
    :param output_tokens: Cumulative output (completion) tokens, e.g.
        ``3400``. ``None`` when not recorded.
    :param total_tokens: Cumulative total tokens (counts cache buckets too,
        as the harness reports), e.g. ``15400``. ``None`` when not recorded.
    :param cache_read_input_tokens: Cumulative tokens read from the prompt
        cache, e.g. ``8000``. ``None`` when not recorded.
    :param cache_creation_input_tokens: Cumulative tokens written to the
        prompt cache, e.g. ``2000``. ``None`` when not recorded.
    :param total_cost_usd: Cumulative USD spend attributed to this model,
        e.g. ``0.42``. Present **only when this model's turns were priced**
        (same "priced ⟺ key present" contract as the session total); ``None``
        when the model is unpriced, so the sum of priced per-model costs
        equals the session ``total_cost_usd``.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    total_cost_usd: float | None = None


class BackgroundTaskInfo(BaseModel):
    """
    One still-running background shell from the claude-native ``Stop`` hook.

    Claude Code leaves finished shells in the hook's ``background_tasks``
    array, so the list surfaced here is filtered to the non-terminal ones —
    its length matches the ``background_task_count`` tally. Every field is
    best-effort: the hook shape is external, so an entry missing one field
    still yields a usable row (e.g. a ``description`` with no ``command``).

    :param id: Opaque per-shell identifier, e.g. ``"abc123"``.
    :param type: Task kind, e.g. ``"shell"``.
    :param status: Per-task status, e.g. ``"running"``.
    :param description: Human-readable label, e.g. ``"Wait for CI"``.
    :param command: Command the shell is running, e.g. ``"sleep 120"``.
    """

    id: str | None = None
    type: str | None = None
    status: str | None = None
    description: str | None = None
    command: str | None = None


class _SSEEventBase(BaseModel):
    """
    Common base for every SSE event payload model.

    All events share two ambient fields:

    - ``type``: the event-type discriminator literal (defined per
      subclass so :data:`ServerStreamEvent` can dispatch).
    - ``sequence_number``: monotonic per-stream sequence number
      assigned by the SSE serializer at emit time
      (``_format_sse`` in ``omnigent/server/routes/sessions.py``).
      Producers leave it ``None``; the route serializer populates
      it on the wire. ``None`` on session-scoped events emitted
      directly by the runtime (the session stream does not number
      events).

    Subclasses MUST declare ``type`` as ``Literal[...]`` so the
    discriminated-union machinery can route incoming dicts. The
    ``model_config`` is forward-compatible — see the module
    docstring for the rationale.

    :param sequence_number: Per-stream monotonic counter assigned
        by the SSE serializer, e.g. ``42``. ``None`` on the
        producer side (before serialization) and on session-scoped
        events that the runtime publishes directly without
        sequencing.
    """

    sequence_number: int | None = None

    model_config = ConfigDict(extra="ignore")


class SessionStatusEvent(_SSEEventBase):
    """
    Session lifecycle status transition.

    Emitted by the runtime / session route handler at every
    transition between ``launching`` / ``running`` / ``waiting`` /
    ``idle`` / ``failed``. Wire shape is
    FLAT (not enveloped): ``{"type": "session.status",
    "conversation_id": "...", "status": "...",
    "sequence_number": null}``.

    The ``waiting`` value is emitted by the runtime's parent agent
    loop when it parks on the ``async_work_complete`` drain
    (``_drain_async_completions(block_for_one=True)`` in
    ``omnigent/runtime/workflow.py``) — i.e. while the parent
    turn is suspended waiting for background tools or sub-agents
    to complete. Per the session-rearchitecture spec §3
    ("Event types and direction"), ``waiting`` is the
    session-status companion of the spec's ``turn.waiting``
    transient — clients should render the session as actively
    blocked-on-async-work, distinct from ``running``. When the
    drain wakes (a child completed), the runtime emits a follow-up
    ``running`` to resume.

    :param type: Always ``"session.status"``.
    :param conversation_id: The conversation/session identifier
        whose status changed, e.g. ``"conv_abc123"``.
    :param status: New session status. ``"launching"`` (session or
        child task created, but no concrete harness start observed),
        ``"idle"`` (no loop running), ``"running"`` (loop executing),
        ``"waiting"`` (parent turn parked on the async-work drain), or
        ``"failed"`` (terminal failure).
    :param response_id: Optional active response id for terminal-backed
        integrations, e.g. ``"codex_turn_abc123"``. Clients use it to
        associate coarse session status edges with the assistant bubble
        they describe. ``None`` for ordinary in-process runtime edges.
    :param error: Machine-readable failure detail, present only
        when ``status == "failed"``. Carries the message the
        runner attached when a turn died — most importantly a
        SETUP-phase failure (spec resolution, spawn-env build)
        that ends the turn before any ``response.failed`` event
        is emitted. ``None`` for every non-failed transition.
        Clients render ``error.message`` as the terminal error
        line; without it a setup failure shows as a silent end.
    :param background_task_count: Background shells still running at this
        edge (claude-native ``Stop`` hook). ``None`` when the edge carries no
        information (leave the sticky tally untouched); ``0`` clears it.
    :param background_tasks: Per-shell detail backing that tally, so the UI
        can name each running shell. ``None`` when the edge reports no detail
        (an older runner may send only the count).
    :param blocked_on: Short human phrase naming what a still-``running``
        session is parked on, e.g. ``"permission prompt"`` or
        ``"dialog open"``. Set by terminal-backed integrations whose agent
        can block on a dialog the web UI does not mirror, so the client can
        say *why* nothing is moving instead of showing a bare spinner.
        ``None`` whenever the session is not parked. Unrelated to the
        ``waiting`` status above, which means the turn has ended and only
        background work remains.

    Category: **transient** (SSE-only). Status is rederived on
    reconnect from the cached last-relayed turn lifecycle event
    or by re-querying the runner; not persisted by the runtime.
    """

    type: Literal["session.status"]
    conversation_id: str
    status: Literal["idle", "launching", "running", "waiting", "failed"]
    response_id: str | None = None
    error: ErrorDetail | None = None
    background_task_count: int | None = None
    background_tasks: list[BackgroundTaskInfo] | None = None
    blocked_on: str | None = None


class SessionUsageEvent(_SSEEventBase):
    """
    Token-usage update from a terminal-backed integration.

    Emitted after an ``external_session_usage`` POST from an
    out-of-AP runtime (e.g. the ``omnigent claude`` transcript
    forwarder). Either field may be absent; clients should leave
    cached values untouched for missing fields.

    :param type: Always ``"session.usage"``.
    :param conversation_id: Session identifier.
    :param context_tokens: ``input + cache_creation + cache_read``
        from the latest assistant ``message.usage``. ``None`` on a
        window-only broadcast.
    :param context_window: Resolved window in tokens (e.g. 200_000
        normally, 1_000_000 with ``opus[1m]`` / ``sonnet[1m]``).
        ``None`` on a tokens-only broadcast.
    :param total_cost_usd: Cumulative session spend in USD after this
        update, e.g. ``0.42`` — the server-computed total the
        cost-budget policy gates on. Present **only when the session
        is priced**; omitted (``None``, stripped by ``exclude_none``)
        when unpriced or on a broadcast that carries no cost change,
        so the client keeps its prior value (the snapshot seeds the
        initial "—" for an unpriced session). Once a session is priced
        the total only grows, so it never reverts to unpriced.
    :param usage_by_model: Per-model breakdown of the same subtree usage
        after this update, keyed by raw harness model id, e.g.
        ``{"claude-sonnet-4-6": ModelUsage(input_tokens=12000, ...)}``.
        ``None`` (stripped by ``exclude_none``) on a broadcast that carries
        no per-model change, so the client keeps its cached map.

    Category: **transient** (SSE-only). On reconnect, clients seed
    the ring from the session snapshot's ``last_total_tokens`` and
    ``context_window``, the cost indicator from ``total_cost_usd``,
    and the per-model token breakdown from ``usage_by_model``.
    """

    type: Literal["session.usage"]
    conversation_id: str
    context_tokens: int | None = None
    context_window: int | None = None
    total_cost_usd: float | None = None
    usage_by_model: dict[str, ModelUsage] | None = None


class SessionModelEvent(_SSEEventBase):
    """
    Active-model report from a harness integration.

    Emitted after an ``external_model_change`` POST from a native
    forwarder or when an SDK relay reports its concrete model in terminal
    response usage. Every surface re-renders its model display from this.

    :param type: Always ``"session.model"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param model: The model the harness reports the session is on,
        VERBATIM in the harness's own spelling, e.g.
        ``"claude-opus-4-8[1m]"`` or ``"gpt-5.6-luna"`` — never
        collapsed to a picker alias.

    Category: **transient** (SSE-only). The server also writes
    ``reported_model`` on the conversation (served on the snapshot's
    ``llm_model``), so on reconnect clients restore the display from
    the snapshot rather than from a replayed event.
    """

    type: Literal["session.model"]
    conversation_id: str
    model: str


class SessionTitleEvent(_SSEEventBase):
    """
    Session-title update from a terminal-backed integration.

    Emitted after an ``external_session_title`` POST from the
    ``omnigent claude`` transcript forwarder when the operator renames
    the session inside the Claude Code pane (``/rename``). Lets the web
    session list show the new name without a reload.

    :param type: Always ``"session.title"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param title: Title the session is now on, e.g. ``"auth-refactor"``.

    Category: **transient** (SSE-only). The server also writes ``title``
    on the conversation, so on reconnect clients restore the name from
    the session snapshot rather than from a replayed event.
    """

    type: Literal["session.title"]
    conversation_id: str
    title: str


class SessionReasoningEffortEvent(_SSEEventBase):
    """
    Active reasoning-effort update from a terminal-backed integration.

    Emitted after an ``external_reasoning_effort_change`` POST from a native
    terminal forwarder when the user changes the thinking level inside the
    terminal UI. Lets the web effort picker reflect a TUI-side switch without
    a reload.

    :param type: Always ``"session.reasoning_effort"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param reasoning_effort: Reasoning effort now active for the session, e.g.
        ``"medium"``, or ``None`` when Codex cleared to its default.

    Category: **transient** (SSE-only). The server also writes
    ``reasoning_effort`` on the conversation, so on reconnect clients restore
    the selection from the session snapshot rather than from a replayed event.
    """

    type: Literal["session.reasoning_effort"]
    conversation_id: str
    reasoning_effort: str | None = None


class SessionCollaborationModeEvent(_SSEEventBase):
    """
    Active collaboration-mode update from a Codex-native session.

    Emitted after the web UI toggles Codex collaboration mode, and after the
    Codex forwarder observes a ``thread/settings/updated`` notification from
    the native Codex TUI. Lets connected clients show a clear Plan-mode
    indicator without a reload.

    :param type: Always ``"session.collaboration_mode"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param mode: The active collaboration mode string, e.g. ``"plan"`` or
        ``"default"``.

    Category: **transient** (SSE-only). The server also writes
    ``omnigent.codex_native.collaboration_mode`` on the conversation labels,
    so reconnect clients restore the same state from the session snapshot.
    """

    type: Literal["session.collaboration_mode"]
    conversation_id: str
    mode: str


class SessionPermissionModeEvent(_SSEEventBase):
    """
    Active permission-mode update from a claude-native session.

    Emitted after the web UI switches the mode, and after the Claude forwarder
    observes a different mode in the pane footer — a shift+tab pressed inside
    the TUI, which Omnigent has no other way to see. Lets the composer's mode
    picker track the pane without a reload.

    :param type: Always ``"session.permission_mode"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param permission_mode: The active mode, e.g. ``"auto"`` or ``"plan"``.

    Category: **transient** (SSE-only). The server also writes
    ``omnigent.claude_native.permission_mode`` on the conversation labels, so
    reconnecting clients restore the same state from the session snapshot.
    """

    type: Literal["session.permission_mode"]
    conversation_id: str
    permission_mode: str


class SessionCodexApprovalModeEvent(_SSEEventBase):
    """
    Active approval/sandbox-mode update from a codex-native session.

    Emitted after the web UI switches the mode, and after the Codex forwarder
    observes a ``thread/settings/updated`` notification — an approval change the
    user made inside Codex's ``/permissions`` popup, which Omnigent has no other
    way to see. Lets the composer's approval picker track the thread without a
    reload.

    :param type: Always ``"session.codex_approval_mode"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param approval_mode: The active mode, one of ``"ask-for-approval"``,
        ``"approve-for-me"``, ``"full-access"``, ``"read-only"``.

    Category: **transient** (SSE-only). The server also writes
    ``omnigent.codex_native.approval_mode`` on the conversation labels (and
    ``terminal_launch_args``), so reconnecting clients restore the same state
    from the session snapshot.
    """

    type: Literal["session.codex_approval_mode"]
    conversation_id: str
    approval_mode: str


class SessionAgentChangedEvent(_SSEEventBase):
    """
    The session's bound agent changed.

    Emitted after a session's MCP servers are edited (the agent's bundle
    is rewritten). Connected clients re-derive their cached session state
    (bound agent, harness presentation labels) from a fresh snapshot.

    :param type: Always ``"session.agent_changed"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param agent_id: The agent bound to the session, e.g. ``"ag_abc123"``.
    :param agent_name: Display name of that agent, e.g. ``"claude-native-ui"``.

    Category: **transient** (SSE-only). The change is persisted, so on
    reconnect clients read it from the session snapshot.
    """

    type: Literal["session.agent_changed"]
    conversation_id: str
    agent_id: str
    agent_name: str


class SessionTodosEvent(_SSEEventBase):
    """
    Plan/TODO update from a native terminal-backed session.

    Emitted after an ``external_session_todos`` POST from a native
    harness forwarder, which captures structured Plan updates from
    Claude or Codex and forwards them to the Omnigent server. Lets web
    render a live todo panel in the right column without polling.

    :param type: Always ``"session.todos"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.
    :param todos: Current native Plan/TODO items from a harness.
        Each entry is a raw dict with ``content`` (str),
        ``status`` (``"pending"`` | ``"in_progress"`` |
        ``"completed"``), and ``activeForm`` (str, display activity)
        keys, e.g. ``[{"content": "Fix the bug", "status":
        "in_progress", "activeForm": "Fixing the bug"}]``.

    Category: **transient** (SSE-only). On reconnect, clients seed
    the panel from the session snapshot's ``todos`` field, which is
    restored from persisted metadata at snapshot build time.
    """

    type: Literal["session.todos"]
    conversation_id: str
    todos: list[dict[str, Any]]


class SessionTerminalPendingEvent(_SSEEventBase):
    """
    Terminal spin-up status for a terminal-first session.

    Two sources emit this event:

    1. The Omnigent server at ``POST /v1/sessions`` for host-launched
       terminal-first sessions — the earliest possible point, before
       the runner even starts, so the spinner appears immediately on
       session create rather than after the runner boots.
    2. The Omnigent relay when the runner's ``session.terminal_pending`` frame
       arrives — covers non-host-launched sessions (e.g. server-dispatched
       sub-agents) and carries the authoritative ``pending=False`` clear
       emitted by the runner's ``finally`` block.

    Together they allow web to show a spinner on the Terminal pill
    while the backend boots the terminal instead of a silent greyed-out
    button, and to distinguish "still starting up" from "no terminal"
    (killed or never created).

    :param type: Always ``"session.terminal_pending"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.
    :param pending: ``True`` while the terminal is being created;
        ``False`` once it lands or auto-create fails.

    Category: **transient** (SSE-only). On reconnect, clients seed the
    spinner from the session snapshot's ``terminal_pending`` field,
    which is populated by ``_session_terminal_pending_cache`` at
    snapshot build time.
    """

    type: Literal["session.terminal_pending"]
    conversation_id: str
    pending: bool


class SessionSandboxStatusEvent(_SSEEventBase):
    """
    Managed-sandbox launch progress for a ``host_type="managed"`` session.

    A managed create returns before its sandbox exists; the Omnigent
    server emits this event as the background launch pipeline advances
    so the Web UI can show live provisioning progress on the session
    page instead of a silent dead chat: sandbox provision → repository
    clone → host startup → runner connect → ready, or a terminal
    failure with the reason.

    :param type: Always ``"session.sandbox_status"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.
    :param stage: The launch stage just entered, e.g.
        ``"provisioning"`` — see :class:`SandboxStatus` for the full
        pipeline order.
    :param provider: Sandbox provider being launched, e.g. ``"arclet"``,
        or ``None`` when unknown.
    :param details: What the user needs to do when
        ``stage == "action_required"``, e.g. ``"Sign in on your laptop."``.
        ``None`` otherwise.
    :param error: Failure detail when ``stage == "failed"``, e.g.
        ``"managed sandbox launch failed: spend limit reached"``.
        ``None`` otherwise.

    Category: **transient** (SSE-only). On reconnect, clients seed the
    progress indicator from the session snapshot's ``sandbox_status``
    field, which is populated by ``_session_sandbox_status_cache`` at
    snapshot build time.
    """

    type: Literal["session.sandbox_status"]
    conversation_id: str
    stage: SandboxLaunchStage
    error: str | None = None
    provider: str | None = None
    details: str | None = None


class McpServerStartup(BaseModel):
    """
    One MCP server's startup state within a ``session.mcp_startup`` event.

    :param status: Latest startup state reported by the harness, mirroring
        Codex's ``McpServerStartupState`` enum.
    :param error: Failure detail when ``status == "failed"``, e.g.
        ``"handshaking with MCP server failed"``. ``None`` otherwise.
    """

    status: Literal["starting", "ready", "failed", "cancelled"]
    error: str | None = None


class SessionMcpStartupEvent(_SSEEventBase):
    """
    Per-MCP-server startup progress for a native harness session.

    A codex-native session brings up its configured MCP servers when its
    Codex thread starts; slow or failing servers previously left the web
    session looking hung with no signal. The native forwarder mirrors
    Codex's ``mcpServer/startupStatus/updated`` notifications as
    ``external_mcp_startup`` posts, republished here so the web UI can
    show which servers are still starting and which failed or were
    cancelled.

    :param type: Always ``"session.mcp_startup"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.
    :param servers: Latest per-server startup map, e.g.
        ``{"safe": {"status": "starting", "error": None}}``.

    Category: **transient** (SSE + snapshot cache). Not persisted; a
    client connecting mid-startup seeds from the session snapshot's
    ``mcp_startup`` field and updates live off this event.
    """

    type: Literal["session.mcp_startup"]
    conversation_id: str
    servers: dict[str, McpServerStartup]


class SessionModelOptionsEvent(_SSEEventBase):
    """
    Signal that a native session's model catalog has resolved.

    Model options are fetched from the bound runner and cached on the session
    snapshot. The initial snapshot can return an empty list while this
    background fetch is in flight; this event tells connected clients to
    re-read the snapshot and apply its now-populated ``model_options``.

    Carries no payload beyond the conversation id. The snapshot's
    ``model_options`` field remains the source of truth.

    :param type: Always ``"session.model_options"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.

    Category: **transient** (SSE-only). On reconnect, clients seed
    Native model / effort controls from the session snapshot.
    """

    type: Literal["session.model_options"]
    conversation_id: str


class SessionInputConsumedPayload(BaseModel):
    """
    Inner payload of a :class:`SessionInputConsumedEvent`.

    Emitted by the sessions route handler at the moment a client
    input is persisted into ``conversation_items``. Carries the
    persisted-item shape so clients can render the input (e.g.
    the user's message bubble) at the moment of acceptance.

    :param item_id: Stable identifier of the conversation item
        just persisted, e.g. ``"item_abc123"``.
    :param type: The item type discriminator — ``"message"`` for
        user messages, ``"function_call_output"`` for tool
        results, etc. Mirrors
        :class:`omnigent.server.schemas.SessionEventInput`'s
        ``type`` field.
    :param data: Decoded item payload, e.g.
        ``{"role": "user", "content": [{"type": "input_text",
        "text": "Hello"}]}``. Heterogeneous and ``type``-specific.
    :param created_by: Email of the human actor who posted the item,
        e.g. ``"alice@example.com"``. ``None`` for agent/tool/system
        items and single-user mode. Mirrors
        :meth:`ConversationItem.to_api_dict` for live attribution.
    :param cleared_pending_id: When this consumed message drains a
        :mod:`omnigent.runtime.pending_inputs` entry (a native-
        terminal web message round-tripping back from the transcript),
        the drained entry's id, e.g. ``"pending_a1b2c3"``. Lets a
        client drop the matching optimistic bubble by id instead of
        by position. ``None`` for non-native messages and for messages
        that matched no pending entry (e.g. typed directly in the TUI).
    """

    item_id: str
    type: str
    # Heterogeneous payload — concrete shape varies by ``type``
    # (matches :class:`SessionEventInput.data`).
    data: dict[str, Any]
    created_by: str | None = None
    cleared_pending_id: str | None = None

    model_config = ConfigDict(extra="ignore")


class SessionInputConsumedEvent(_SSEEventBase):
    """
    A queued input item was materialized into conversation history.

    Emitted by ``POST /v1/sessions/{id}/events`` once per accepted
    input item at the moment it is persisted into conversation
    history (either onto a steered active turn or as the seed item
    of a freshly-started one). Wire shape uses the NESTED envelope:
    ``{"type": "session.input.consumed", "data":
    <:class:`SessionInputConsumedPayload`>, "sequence_number":
    null}``.

    The event name is **provisional** — it may be renamed in a
    future revision. Consumers should reference
    :data:`SessionInputConsumedEvent` (or its ``type`` literal)
    rather than hardcoding the wire string.

    :param type: Always ``"session.input.consumed"``.
    :param data: The decoded queued-item payload — see
        :class:`SessionInputConsumedPayload`.
    """

    type: Literal["session.input.consumed"]
    data: SessionInputConsumedPayload


class SessionInterruptedPayload(BaseModel):
    """
    Inner payload of a :class:`SessionInterruptedEvent`.

    Built by ``_publish_interrupted`` in
    ``omnigent/server/routes/sessions.py``.

    :param requested_at: Unix epoch seconds when the interrupt
        request reached the server, e.g. ``1704067200``.
    :param response_id: Optional active response id for terminal-backed
        integrations, e.g. ``"codex_turn_abc123"``.
    """

    requested_at: int
    response_id: str | None = None

    model_config = ConfigDict(extra="ignore")


class SessionInterruptedEvent(_SSEEventBase):
    """
    User-triggered cancel reached the loop.

    Emitted by ``_publish_interrupted`` in
    ``omnigent/server/routes/sessions.py`` when a client posts
    a ``{"type": "interrupt"}`` to ``POST
    /v1/sessions/{id}/events``. Co-emitted with
    :class:`IncompleteEvent` (with the underlying response carrying
    ``incomplete_details.reason == "user_interrupt"``) so off-the-
    shelf Responses parsers still close cleanly. Wire shape uses
    the NESTED envelope verbatim from the existing emit site.

    :param type: Always ``"session.interrupted"``.
    :param data: The interrupt metadata — see
        :class:`SessionInterruptedPayload`.
    """

    type: Literal["session.interrupted"]
    data: SessionInterruptedPayload


class SessionCreatedEvent(_SSEEventBase):
    """
    A child (sub-agent) session was spawned from this session.

    Emitted by ``omnigent/tools/builtins/spawn.py:_spawn_one``
    onto the **parent** session's conversation stream after the
    child conversation row is created and the child task has been
    started. Per the session-rearchitecture spec §3 ("Event types
    and direction") and §7 ("Flow: client interacts with
    sub-agent"), this lets clients watching the parent session's
    SSE subscribe directly to the child's stream without polling
    history for the tunneled ``function_call`` item.

    The wire shape is FLAT (not enveloped):
    ``{"type": "session.created", "conversation_id": <parent>,
    "child_session_id": <child>, "agent_id": <agent or None>,
    "parent_session_id": <parent>, "sequence_number": null}``.

    The existing tunneled ``function_call`` ConversationItem
    (carried inside :class:`OutputItemDoneEvent`) is retained
    for compatibility — clients that don't yet implement the
    "subscribe to child stream" pattern can keep rendering sub-
    agent calls from the parent's persistent history.

    :param type: Always ``"session.created"``.
    :param conversation_id: The PARENT session/conversation id —
        this event rides the parent's stream, e.g.
        ``"conv_parent123"``.
    :param child_session_id: The newly-created child session id,
        e.g. ``"conv_child456"``. Same as ``conversation_id`` on
        the child's own stream when consumers pivot to it.
    :param agent_id: Registered agent id the child runs as,
        e.g. ``"agent_xyz"``. ``None`` is permitted only for
        legacy spawn paths that did not record an agent id;
        new code MUST set it.
    :param parent_session_id: Echo of ``conversation_id`` for
        consumers that key on a dedicated "parent" field rather
        than the carrier ``conversation_id``. Always equal to
        ``conversation_id``; included for forward-compat with
        clients that may relay these events across stream
        boundaries.

    Category: **transient** (SSE-only). The corresponding durable
    record of "a child session exists" lives in the conversation
    store as the child conversation row itself
    (``parent_conversation_id`` foreign key) and the parent's
    tunneled ``function_call`` item — reconnecting clients
    discover children by walking the parent's persisted history,
    not by replaying this event.
    """

    type: Literal["session.created"]
    conversation_id: str
    child_session_id: str
    agent_id: str | None = None
    parent_session_id: str | None = None


class SessionSupersededEvent(_SSEEventBase):
    """
    This conversation was superseded by another and clients should
    follow to it.

    Emitted by ``_publish_session_superseded`` in
    ``omnigent/server/routes/sessions.py`` when the claude-native
    forwarder rotates a session away on a Claude ``/clear`` (the old
    conversation keeps its history but the live terminal moves to a
    fresh conversation — see ``_post_clear_supersession`` in
    ``omnigent/claude_native_forwarder.py``). A client actively viewing
    the superseded conversation auto-redirects to ``target_conversation_id``.

    Category: **transient** (SSE-only), live-only by design. There is no
    SSE replay: a client that connects after the rotation does not get
    this event. The durable counterpart is the persisted notice message
    appended to the old conversation (a ``message`` item linking to the
    new conversation), which a reloading client renders instead of being
    force-redirected.

    The wire shape is FLAT (not enveloped):
    ``{"type": "session.superseded", "conversation_id": <old>,
    "target_conversation_id": <new>, "reason": "clear"}``.

    :param type: Always ``"session.superseded"``.
    :param conversation_id: The superseded (old) conversation id this
        event rides the stream of, e.g. ``"conv_old"``.
    :param target_conversation_id: The conversation to follow to, e.g.
        ``"conv_new"``.
    :param reason: Why the session was superseded. Currently always
        ``"clear"`` (a Claude Code ``/clear``); kept as a field so the
        client can branch on future supersession causes.
    """

    type: Literal["session.superseded"]
    conversation_id: str
    target_conversation_id: str
    reason: Literal["clear"] = "clear"


class SessionBtwSidechatEvent(_SSEEventBase):
    """
    A Claude Code ``/btw`` side-chat exchange to show transiently.

    ``/btw`` opens an ephemeral side conversation whose answer Claude Code
    keeps only in its in-TUI overlay — it is never written to the session
    transcript. The claude-native forwarder scrapes the settled overlay
    from the pane and emits this event so the managed web UI can show the
    same ephemeral overlay (dismissed with Escape), WITHOUT adding a
    persisted side-chat turn to the main conversation.

    Category: **transient** (SSE-only), live-only by design. Nothing is
    persisted and there is no SSE replay, so a reload drops the overlay —
    matching the terminal, where Escape closes it and leaves no history.

    The wire shape is FLAT (not enveloped):
    ``{"type": "session.btw_sidechat", "conversation_id": <id>,
    "question": <str>, "answer": <str>, "truncated": <bool>}``.

    :param type: Always ``"session.btw_sidechat"``.
    :param conversation_id: The conversation whose stream this rides.
    :param question: The ``/btw`` request line as typed, e.g.
        ``"/btw is this backward compatible?"``.
    :param answer: The side-chat answer text.
    :param truncated: True when the pane clipped a longer answer; the web
        overlay notes it and points at the terminal for the full text.
    """

    type: Literal["session.btw_sidechat"]
    conversation_id: str
    question: str
    answer: str
    truncated: bool = False


class OutputTextDeltaEvent(_SSEEventBase):
    """
    Incremental assistant-text token emitted during streaming.

    Wire shape matches the existing raw-dict emit at
    ``omnigent/runtime/workflow.py:1352-1356``.

    :param type: Always ``"response.output_text.delta"``.
    :param delta: The text fragment for this chunk, e.g.
        ``"Hello"``.
    :param message_id: For native terminal streaming, the provider's stable
        per-message id, e.g. ``"2ca51d97-2f0f-493a-aed7-85a5b56c5747"``.
        ``None`` for ordinary in-process task streaming, where deltas group
        by the active response.
    :param index: 0-based chunk order within the message, e.g. ``3``.
        Used to suppress repeated chunks; ``None`` for in-process streaming.
    :param final: Optional provider completion marker for the message.
    """

    type: Literal["response.output_text.delta"]
    delta: str
    message_id: str | None = None
    index: int | None = None
    final: bool | None = None


class ToolOutputDeltaEvent(_SSEEventBase):
    """Incremental output from an in-progress function call.

    :param type: Always ``"response.function_call_output.delta"``.
    :param call_id: Function-call correlation id.
    :param delta: Command stdout/stderr fragment.
    """

    type: Literal["response.function_call_output.delta"]
    call_id: str
    delta: str


class ReasoningStartedEvent(_SSEEventBase):
    """
    Marker emitted once when a reasoning block begins.

    Fired even when the reasoning content itself is encrypted /
    redacted (so no delta events follow), letting clients render
    a "thinking…" indicator regardless of provider verification
    status. Wire shape matches ``omnigent/runtime/workflow.py:1350``.

    :param type: Always ``"response.reasoning.started"``.
    """

    type: Literal["response.reasoning.started"]


class ReasoningTextDeltaEvent(_SSEEventBase):
    """
    Incremental reasoning-text token (full chain-of-thought).

    Only emitted by providers that surface reasoning content
    (e.g. OpenAI o-series with appropriate verification). Wire
    shape matches ``omnigent/runtime/workflow.py:1358-1364``.

    :param type: Always ``"response.reasoning_text.delta"``.
    :param delta: The reasoning text fragment, e.g.
        ``"Considering the user's intent..."``.
    """

    type: Literal["response.reasoning_text.delta"]
    delta: str


class ReasoningSummaryTextDeltaEvent(_SSEEventBase):
    """
    Incremental reasoning-summary token.

    Emitted when ``reasoning.summary`` is configured on the
    request. Wire shape matches
    ``omnigent/runtime/workflow.py:1370-1373``.

    :param type: Always ``"response.reasoning_summary_text.delta"``.
    :param delta: The summary text fragment, e.g. ``"Will use
        the search tool to gather context."``.
    """

    type: Literal["response.reasoning_summary_text.delta"]
    delta: str


class OutputItemDoneEvent(_SSEEventBase):
    """
    A conversation output item completed during the turn.

    Carries any item type the conversation persists (message,
    function_call, function_call_output, reasoning, compaction,
    native_tool, …). The ``item`` payload's wire shape merges
    common fields (``id``, ``type``, ``status``) with the
    type-specific data fields — it is NOT nested as
    ``{type, data}``.

    :param type: Always ``"response.output_item.done"``.
    :param item: The completed item dict. Heterogeneous and
        item-type-specific; see
        ``omnigent/entities/conversation.py`` for the
        per-type ``*Data`` shapes that drive serialization.
        Example for a function_call item: ``{"id": "fc_abc123",
        "type": "function_call", "status": "action_required",
        "name": "search.web", "arguments": "{\\"q\\": \\"foo\\"}",
        "call_id": "call_xyz"}``.
    """

    type: Literal["response.output_item.done"]
    # ``dict[str, Any]`` because items are heterogeneous and
    # type-specific (their per-type ``*Data`` shapes already
    # live in entities/conversation.py via ITEM_TYPE_TO_DATA_CLS).
    # Modeling each variant here would duplicate that mapping;
    # consumers that need typed item data parse via
    # ``parse_item_data(item["type"], item)``.
    item: dict[str, Any]


class OutputFileDoneEvent(_SSEEventBase):
    """
    A streamed file output completed materializing.

    Emitted by ``_emit_file_annotation_events`` in
    ``omnigent/runtime/workflow.py`` once per file annotation in
    the assistant's output. ``filename`` and ``content_type`` are
    only populated when the originating annotation carried them.

    :param type: Always ``"response.output_file.done"``.
    :param file_id: Identifier of the materialized file,
        e.g. ``"file_abc123"``.
    :param filename: Original filename if the annotation supplied
        one, e.g. ``"report.pdf"``. ``None`` otherwise.
    :param content_type: MIME content type if the annotation
        supplied one, e.g. ``"application/pdf"``. ``None``
        otherwise.
    """

    type: Literal["response.output_file.done"]
    file_id: str
    filename: str | None = None
    content_type: str | None = None


class HeartbeatEvent(_SSEEventBase):
    """
    Keepalive event emitted on a fixed cadence during streaming.

    Lets consumers detect stalled producers via missed-interval
    timing. Cadence is set by ``_HEARTBEAT_INTERVAL_S`` in
    ``omnigent/runtime/workflow.py`` (15 seconds at the time of
    writing). Wire shape matches the existing emit at
    ``omnigent/runtime/workflow.py:4636-4639``.

    Per ``designs/SERVER_HARNESS_CONTRACT.md`` §Heartbeats, the
    event MAY carry timing metadata so consumers can do richer
    dead-detection than "did anything arrive":

    - ``server_time`` is the producer's wall-clock at emission,
      letting consumers detect clock drift between producer and
      consumer.
    - ``last_event_seq`` is the ``sequence_number`` of the most
      recent NON-heartbeat event (or ``None`` when this is the
      first heartbeat before any user-visible event), letting
      consumers detect dropped events on reconnect.

    Both fields are optional on the wire (``None`` round-trips as
    omitted) so older AP→harness pairs that pre-date the field
    addition still parse cleanly.

    :param type: Always ``"response.heartbeat"``.
    :param server_time: ISO 8601 UTC timestamp at emission, e.g.
        ``"2026-04-27T15:30:00Z"``. ``None`` when the producer
        chose not to populate it (legacy emitters).
    :param last_event_seq: Sequence number of the last non-
        heartbeat event seen on the same stream, e.g. ``42``.
        ``None`` before any user-visible event has fired (first
        heartbeat of the turn, before deltas land), or when the
        producer chose not to populate it.
    """

    type: Literal["response.heartbeat"]
    server_time: str | None = None
    last_event_seq: int | None = None


class SessionHeartbeatEvent(_SSEEventBase):
    """
    Idle-stream keepalive on ``GET /v1/sessions/{id}/stream``.

    Emitted by the session-stream route on a fixed cadence whenever
    the underlying publish queue has been quiet (no turn in flight,
    no resource events). Distinct from :class:`HeartbeatEvent`
    (``response.heartbeat``), which is per-turn and is driven by
    the runtime workflow while a response is producing output.

    Why this exists: the session stream stays open across many turns
    and through idle periods (waiting for the user to type). Without
    a periodic emit, intermediate proxies, OS-level sockets, and the
    client's SSE read-timeout can leave a half-open stream
    undetected for minutes after a network event (laptop sleep,
    Wi-Fi handoff). The heartbeat puts a regular byte on the wire
    so the client's read-timeout and the server's
    ``request.is_disconnected()`` check both fire promptly.

    Consumers MAY ignore the payload entirely (the bytes crossing
    the wire are sufficient). The optional ``server_time`` mirrors
    :class:`HeartbeatEvent` for symmetry and debugging.

    :param type: Always ``"session.heartbeat"``.
    :param server_time: ISO 8601 UTC timestamp at emission, e.g.
        ``"2026-05-25T10:30:00Z"``. ``None`` when the producer
        chose not to populate it.
    """

    type: Literal["session.heartbeat"]
    server_time: str | None = None


class PresenceViewer(BaseModel):
    """
    One user currently viewing a session (holding its SSE stream open).

    :param user_id: The viewer's authenticated identity,
        e.g. ``"alice@example.com"``. Never the reserved single-user
        ``"local"`` sentinel — presence only tracks distinct human
        actors (see ``attribution_user``).
    :param joined_at: ISO 8601 UTC timestamp of when the user joined,
        e.g. ``"2026-06-10T17:00:00Z"``. Stable across reconnects
        within the server's leave-grace window.
    :param idle: Whether every stream the user holds reports an idle
        (backgrounded) tab. The web greys idle viewers' avatars.
    """

    user_id: str
    joined_at: str
    idle: bool = False


class SessionPresenceEvent(_SSEEventBase):
    """
    The session's viewer list changed — full state, not a delta.

    Emitted on ``GET /v1/sessions/{id}/stream`` whenever a user
    joins, leaves (after the server-side grace window absorbs
    reconnect churn), or flips their idle aggregate, and once to
    each newly-connected stream as a snapshot-on-connect. Every
    event carries the COMPLETE viewer list so clients replace their
    state wholesale — missed events self-heal on the next event or
    reconnect. Viewers are scoped to the session *tree* (the root
    conversation and every sub-agent conversation under it), so a
    user on a sub-agent page and a user on the root page appear in
    each other's lists. See ``omnigent/server/presence.py`` and
    ``designs/UI/PRESENCE.md``.

    :param type: Always ``"session.presence"``.
    :param conversation_id: The conversation whose stream delivered
        this event — the root or a sub-agent conversation, e.g.
        ``"conv_abc123"``. Matches the streamed conversation (not
        necessarily the tree's root) so clients can guard events by
        the conversation they are viewing.
    :param viewers: All users currently viewing any conversation in
        the session tree (including the receiving user — the web
        filters self out for display), ordered by join time.
    """

    type: Literal["session.presence"]
    conversation_id: str
    viewers: list[PresenceViewer]


class ElicitationRequestParams(BaseModel):
    """
    Inner ``params`` block of a :class:`ElicitationRequestEvent`.

    The standard fields (``mode``, ``message``, ``requestedSchema``,
    ``url``) mirror MCP's ``ElicitRequestFormParams`` /
    ``ElicitRequestUrlParams`` byte-for-byte (Principle 8 — adopt
    MCP's wire shape verbatim where it overlaps). The
    AP-specific extensions (``phase``, ``policy_name``,
    ``content_preview``, ``target_session_id``) carry policy-engine
    context and mirrored-child routing for the consumer's renderer;
    MCP's ``extra="allow"`` config permits them under the same params
    block. Wire shape matches
    ``omnigent/runtime/policies/approval.py:175``.

    :param mode: MCP-standard discriminator. ``"form"`` collects
        structured input via ``requestedSchema``; ``"url"``
        directs upstream to an external URL for OAuth /
        out-of-band interaction.
    :param message: Human-readable prompt the consumer renders,
        e.g. ``"Approve running 'rm -rf /tmp/cache'?"``.
    :param requestedSchema: JSON-Schema dict for form mode (or
        ``None`` for url mode). camelCase preserved per MCP
        spec, e.g.
        ``{"type": "object", "properties": {"approve":
        {"type": "boolean"}}}``.
    :param url: External URL for url mode (or ``None`` for form
        mode), e.g. ``"https://oauth.example.com/authorize?..."``.
    :param phase: Omnigent policy-engine phase the elicitation
        belongs to, e.g. ``"pre_tool_use"``.
    :param policy_name: Omnigent policy that triggered the
        elicitation, e.g. ``"approve_shell_commands"``.
    :param content_preview: Truncated preview of the underlying
        request payload (≤1024 chars in current AP), for the
        consumer's renderer.
    :param target_session_id: AP session whose resolve endpoint owns
        this elicitation, e.g. ``"conv_child123"``. Present when a
        child/sub-agent prompt is mirrored into an ancestor stream;
        ``None`` means resolve against the current session.
    """

    mode: Literal["form", "url"] = "form"
    message: str
    requestedSchema: dict[str, Any] | None = None
    url: str | None = None
    # AP-specific extensions — allowed under MCP's
    # ``extra="allow"`` policy on the inner params object. Strict
    # MCP clients ignore unknown fields here.
    phase: str | None = None
    policy_name: str | None = None
    content_preview: str | None = None
    target_session_id: str | None = None

    # MCP's ElicitRequestParams uses ``extra="allow"``; mirror
    # that here so MCP-shaped passthrough (an MCP server's
    # ``elicitation/create`` traversing harness → Omnigent → client)
    # preserves any fields the MCP server added.
    model_config = ConfigDict(extra="allow")


class ElicitationRequestEvent(_SSEEventBase):
    """
    Synchronous request for a decision from upstream.

    Emitted by Omnigent (or, under the new contract, by a harness)
    when the LLM / a tool / a policy needs a verdict before
    proceeding. The consumer replies via
    ``POST /v1/sessions/{session_id}/events`` with
    ``type == "approval"`` and
    :class:`omnigent.server.schemas.ElicitationResult` fields in
    ``data``. This preserves MCP request/reply correlation by id
    without threading elicitations through PATCH.

    Wire shape matches the existing emit at
    ``omnigent/runtime/policies/approval.py:175``.

    :param type: Always ``"response.elicitation_request"``.
    :param elicitation_id: Unique correlation id for this
        request — appears in the consumer's approval event payload,
        e.g. ``"elicit_abc123"``.
    :param method: MCP method literal — always
        ``"elicitation/create"`` (the value of
        ``_MCP_ELICITATION_METHOD`` in
        ``omnigent/runtime/policies/approval.py``).
    :param params: The MCP-shaped params block carrying the
        prompt and (form-mode only) the requested schema.
    """

    type: Literal["response.elicitation_request"]
    elicitation_id: str
    # MCP method constant — kept as Literal so the discriminator
    # accepts only the MCP-standard value; harnesses that emit a
    # different method literal will fail validation loudly.
    method: Literal["elicitation/create"] = "elicitation/create"
    params: ElicitationRequestParams


class UiOpenFileEvent(_SSEEventBase):
    """Ask current session viewers to open a workspace-relative file; never replayed."""

    type: Literal["ui.open_file"]
    path: str


class BrowserActionRequestEvent(_SSEEventBase):
    """
    Request that the desktop renderer perform one browser action.

    Emitted by the server ``POST /v1/sessions/{id}/browser/action_request``
    route when a runner-side ``browser_*`` tool dispatch needs the
    Omnigent desktop app's embedded browser to act. The event fans out
    on the session stream to every subscribed renderer; each renderer
    first POSTs ``/browser/action_claim/{action_id}`` and only the
    winning claimant executes the action and POSTs the result back to
    ``/browser/action_result/{action_id}``. The claim lease prevents
    double execution when more than one renderer is subscribed.

    :param type: Always ``"browser.action_request"``.
    :param action_id: Unique correlation id for this request, e.g.
        ``"baction_abc123"``. Echoed on the claim and result routes.
    :param action: The browser action to perform — the ``browser_``
        tool name with the prefix stripped, e.g. ``"navigate"``,
        ``"snapshot"``, ``"click"``, ``"type"``, ``"screenshot"``.
    :param args: Action arguments forwarded from the tool call, e.g.
        ``{"url": "https://example.com"}``.
    """

    type: Literal["browser.action_request"]
    action_id: str
    action: str
    args: dict[str, Any]


class ElicitationResolvedEvent(_SSEEventBase):
    """
    Signal that a previously-published elicitation is no longer
    outstanding, even though no UI ``approval`` verdict was
    delivered through ``POST /v1/sessions/{id}/events``.

    Emitted by the runner when its own ``_pending_approvals``
    Future is popped without a verdict (the runner's wait timed
    out, the turn was cancelled, the harness exited) so the AP
    server's :mod:`omnigent.runtime.pending_elicitations`
    index can decrement the sidebar badge in lockstep with the
    underlying awaiter's lifecycle. Without this signal, the AP
    server has no way to learn that the prompt is dead and the
    badge stays stuck.

    Idempotent on the consumer side: the Omnigent server's index
    decrement is a no-op when the id isn't tracked, so the
    runner can fire-and-forget on every Future cleanup.

    :param type: Always ``"response.elicitation_resolved"``.
    :param elicitation_id: Correlation id of the elicitation
        being cleared, e.g. ``"elicit_abc123"``. Must match the
        id of a prior :class:`ElicitationRequestEvent`.
    :param action: Optional MCP verdict recorded when the
        resolution carried a human decision (``"accept"`` /
        ``"decline"`` / ``"cancel"``). ``None`` for resolutions
        without a verdict — timeout, severed wait, or a runner
        that predates verdict carriage — so consumers can say "no
        verdict was recorded" rather than guessing one.
    :param reason: Why a verdict-less resolution happened, when
        known. ``"unanswered"``: the hook stopped waiting (a severed
        poll never re-parked, the ask timed out) before anyone
        answered, so the prompt is gone rather than decided and the
        UI can say so instead of implying it was resolved elsewhere.
        ``None`` when a verdict is present or the reason is unknown.
    """

    type: Literal["response.elicitation_resolved"]
    elicitation_id: str
    action: Literal["accept", "decline", "cancel"] | None = None
    reason: Literal["unanswered"] | None = None


class PolicyDeniedEvent(_SSEEventBase):
    """
    Signal that a policy DENY was enforced on a native harness turn.

    A native harness (Claude Code, Codex, ...) routes each tool call and
    prompt through Omnigent's policy engine via the vendor command-hook
    (``POST /v1/sessions/{id}/policies/evaluate``). The DENY verdict is
    returned synchronously to that hook, so unlike the SDK/wrap path there is
    no stream-visible signal that a native action was blocked — only the
    *effect* (the blocked tool never runs). This event surfaces the decision
    itself on the session stream so observers (the web UI, the capability
    bench) can see a native DENY as a positive signal rather than infer it
    from an absence.

    Fire-and-forget and observational: it does not gate the turn (the hook
    response already did that) and carries no correlation id.

    :param type: Always ``"response.policy_denied"``.
    :param conversation_id: Session/conversation id the DENY applies to,
        e.g. ``"conv_abc123"``.
    :param reason: Human-readable deny reason from the deciding policy, e.g.
        ``"Blocked by policy."``.
    :param phase: The policy phase the DENY landed on, e.g. ``"tool_call"``.
    """

    type: Literal["response.policy_denied"]
    conversation_id: str
    reason: str = ""
    phase: str = ""


class CreatedEvent(_SSEEventBase):
    """
    Initial event emitted at the start of every streaming response.

    Carries the freshly-allocated
    :class:`ResponseObject` (status will
    be ``"queued"`` or ``"in_progress"`` depending on whether the
    task started immediately).

    :param type: Always ``"response.created"``.
    :param response: The newly-allocated response object.
    """

    type: Literal["response.created"]
    response: ResponseObject


class QueuedEvent(_SSEEventBase):
    """
    Optional event emitted between ``created`` and ``in_progress``
    for background tasks that are queued before they start.

    Foreground streaming responses skip this event.

    :param type: Always ``"response.queued"``.
    :param response: The response object with
        ``status="queued"``.
    """

    type: Literal["response.queued"]
    response: ResponseObject


class InProgressEvent(_SSEEventBase):
    """
    Event emitted once the task transitions to in-progress.

    Always follows ``response.created`` (and ``response.queued``
    for background tasks).

    :param type: Always ``"response.in_progress"``.
    :param response: The response object with
        ``status="in_progress"``.
    """

    type: Literal["response.in_progress"]
    response: ResponseObject


class CompletedEvent(_SSEEventBase):
    """
    Terminal event for a successfully completed turn.

    Carries the final
    :class:`ResponseObject`.

    :param type: Always ``"response.completed"``.
    :param response: The final response object with
        ``status="completed"``.
    """

    type: Literal["response.completed"]
    response: ResponseObject


class FailedEvent(_SSEEventBase):
    """
    Terminal event for a turn that ended with an error.

    Carries a :class:`FailedResponseObject`
    whose ``error`` field describes the failure. Response metadata may
    be absent when the failure occurs before response allocation.

    :param type: Always ``"response.failed"``.
    :param source: Where the fault originated -- ``"llm"`` for
        inference/context errors, ``"harness"`` for Claude Code/harness
        process failures, ``"execution"`` for runner configuration
        or infrastructure failures.
    :param response: The failure response object with ``status="failed"``
        and ``error`` populated.
    """

    type: Literal["response.failed"]
    source: Literal["llm", "execution", "tool", "harness"] = "execution"
    response: ResponseObject | FailedResponseObject


class CancelledEvent(_SSEEventBase):
    """
    Terminal event for a turn cancelled before completion.

    :param type: Always ``"response.cancelled"``.
    :param response: The final response object with
        ``status="cancelled"``.
    """

    type: Literal["response.cancelled"]
    response: ResponseObject


class IncompleteEvent(_SSEEventBase):
    """
    Terminal event for a turn that ended without completing
    (e.g. hit the iteration cap or token budget).

    :param type: Always ``"response.incomplete"``.
    :param response: The final response object with
        ``status="incomplete"`` and ``incomplete_details``
        populated describing the reason.
    """

    type: Literal["response.incomplete"]
    response: ResponseObject


class RetryErrorDetail(BaseModel):
    """
    Error block carried by :class:`RetryEvent` and :class:`ErrorEvent`.

    Mirrors the shape that ``llm_retry.py`` and ``tool_retry.py``
    emit today — flat ``code`` / ``message`` plus an optional
    ``detail`` for provider-specific structured fields.

    :param code: Stable error classifier, e.g. ``"timeout"``,
        ``"rate_limit"``.
    :param message: Human-readable summary, e.g.
        ``"Connection timed out after 30s"``.
    :param detail: Optional provider-specific structured fields
        (e.g. ``{"status_code": 429, "retry_after": 5}``);
        ``None`` when the classifier had no extra context.
    """

    code: str
    message: str
    detail: dict[str, Any] | None = None

    model_config = ConfigDict(extra="ignore")


class RetryEvent(_SSEEventBase):
    """
    A retryable failure was caught and a retry is scheduled.

    Emitted by ``omnigent/runtime/llm_retry.py`` (LLM calls)
    and ``omnigent/runtime/tool_retry.py`` (tool calls) before
    sleeping for the backoff delay. Wire shape matches
    ``llm_retry.py:329-340`` and ``tool_retry.py:168-180``.

    :param type: Always ``"response.retry"``.
    :param source: Origin of the retried failure — ``"llm"`` for
        LLM-call retries, ``"tool"`` for tool-call retries.
    :param tool_name: Tool identifier when ``source == "tool"``,
        e.g. ``"search.web"``. ``None`` for LLM retries.
    :param attempt: 1-based count of the upcoming attempt
        (i.e. attempt that will run AFTER this delay), e.g.
        ``2`` for the first retry.
    :param max_attempts: Total tries allowed by the retry policy,
        e.g. ``3``. Lets clients render "attempt 2 of 3".
    :param delay_seconds: Seconds the producer will sleep before
        retrying, rounded to two decimals, e.g. ``1.5``.
    :param error: Classified error description for the failure
        being retried.
    """

    type: Literal["response.retry"]
    source: Literal["llm", "tool"]
    tool_name: str | None = None
    attempt: int
    max_attempts: int
    delay_seconds: float
    error: RetryErrorDetail


class ErrorEvent(_SSEEventBase):
    """
    Non-recoverable error reported during the turn.

    Emitted from multiple sites in
    ``omnigent/runtime/workflow.py`` — terminal LLM failures
    (``_emit_llm_error_event``), execution timeouts
    (``_handle_execution_timeout``), and the agent-loop catch-all
    (``except Exception``). Wire shape matches those emits.

    :param type: Always ``"response.error"``.
    :param source: Origin of the error -- ``"llm"`` for LLM-call
        failures, ``"execution"`` for timeouts, ``"tool"`` for
        tool failures, ``"harness"`` for harness process failures.
    :param tool_name: Tool identifier when ``source == "tool"``;
        ``None`` for the other sources.
    :param error: Classified error description.
    """

    type: Literal["response.error"]
    source: Literal["llm", "execution", "tool", "harness"]
    tool_name: str | None = None
    error: RetryErrorDetail


class CompactionInProgressEvent(_SSEEventBase):
    """
    Conversation history is being compacted.

    Emitted by ``omnigent/runtime/compaction.py`` while a
    compaction step runs so clients can render a "summarizing
    history…" indicator. Wire shape matches ``compaction.py:765``.

    A long compaction is announced repeatedly (once per status poll), so
    clients must treat every event carrying the same ``started_at`` as one
    compaction — refreshing their indicator rather than stacking another.

    :param type: Always ``"response.compaction.in_progress"``.
    :param started_at: Unix epoch timestamp (seconds) when this compaction
        was first reported in progress. Stable across repeated progress
        events for the same compaction, so clients can anchor an elapsed
        counter to the true start — including after a page reload. ``None``
        when the emitter does not track it.
    """

    type: Literal["response.compaction.in_progress"]
    started_at: int | None = None


class CompactionCompletedEvent(_SSEEventBase):
    """
    Conversation history compaction has finished.

    Emitted after compaction completes — either by the server after
    ``compact_conversation_now()`` (explicit ``/compact``), or by a
    harness that compacted its own internal context. Clients that
    rendered a "Compacting…" spinner on
    :class:`CompactionInProgressEvent` should upgrade it to the
    permanent "Conversation compacted" marker on this event.

    When emitted by a harness, ``summary`` and ``summary_model``
    are populated so the runner can persist a compaction item for
    session resume. When emitted by the server's explicit
    ``/compact`` path, those fields are ``None``.

    :param type: Always ``"response.compaction.completed"``.
    :param total_tokens: Tiktoken estimate of the post-compaction
        message context size, e.g. ``8421``. Used by clients to
        update the context-ring immediately without waiting for the
        next ``response.completed`` usage report. ``None`` when
        token counting is unavailable.
    :param summary: Text summary of the compacted conversation,
        or ``None`` for server-side compaction (already persisted).
    :param summary_model: Model used for summarization, or ``None``
        if truncation-based or server-side.
    """

    type: Literal["response.compaction.completed"]
    total_tokens: int | None = None
    summary: str | None = None
    summary_model: str | None = None
    compacted_messages: list[dict[str, Any]] | None = None


class CompactionFailedEvent(_SSEEventBase):
    """
    Conversation history compaction failed.

    Emitted by ``omnigent/server/routes/sessions.py`` when
    ``compact_conversation_now()`` raises. Clients that rendered a
    "Compacting…" spinner on :class:`CompactionInProgressEvent`
    should dismiss it without leaving a permanent marker, since the
    conversation history was not modified.

    :param type: Always ``"response.compaction.failed"``.
    """

    type: Literal["response.compaction.failed"]


class ClientTaskCancelEvent(_SSEEventBase):
    """
    Server-side request that the client cancel a tunneled tool call.

    Emitted by ``omnigent/runtime/workflow.py`` when a parent
    cancellation needs to propagate to a long-running async client
    tool. Wire shape matches ``workflow.py:4258-4266``.

    :param type: Always ``"response.client_task.cancel"``.
    :param task_id: Identifier of the client-side task being
        cancelled, e.g. ``"resp_async_abc"``.
    :param call_id: Synthetic ``call_id`` the SDK uses to
        reconcile the local task; ``None`` when no pending tool
        call row exists for the task.
    """

    type: Literal["response.client_task.cancel"]
    task_id: str
    call_id: str | None = None


class SessionResourceCreatedEvent(_SSEEventBase):
    """
    A session resource was created.

    Emitted when a terminal is launched, a file is uploaded, or
    any other resource is materialized under a session. Wire shape
    is FLAT: ``{"type": "session.resource.created",
    "resource": <SessionResourceObject-like dict>}``.

    :param type: Always ``"session.resource.created"``.
    :param resource: The newly created resource object.
    """

    type: Literal["session.resource.created"]
    resource: dict[str, Any]


class SessionResourceDeletedEvent(_SSEEventBase):
    """
    A session resource was deleted.

    Emitted when a terminal is closed, a file is deleted, or
    any other resource is removed from a session.

    :param type: Always ``"session.resource.deleted"``.
    :param resource_id: Opaque id of the deleted resource.
    :param resource_type: Type of the deleted resource,
        e.g. ``"terminal"``, ``"file"``.
    :param session_id: Owning session/conversation id.
    """

    type: Literal["session.resource.deleted"]
    resource_id: str
    resource_type: str
    session_id: str


class SessionChildSessionUpdatedEvent(_SSEEventBase):
    """
    A child (sub-agent) session's status changed — pushed to the PARENT.

    Lets the parent's resource rail update a child's status without
    polling ``GET …/child_sessions``. Carries the full
    :class:`ChildSessionSummary` so the web patches its cache directly.

    :param type: Always ``"session.child_session.updated"``.
    :param conversation_id: The PARENT (carrier) session id.
    :param child_session_id: The child session id, e.g.
        ``"conv_child_abc123"``.
    :param child: A PARTIAL :class:`ChildSessionSummary` — the
        snapshot-on-connect sends the full summary, while live runner
        deltas carry only the fields that changed (a status delta omits
        ``last_message_preview``; a preview delta carries only it). The
        web merges present fields over the cached row, so the payload is
        an open dict rather than the strict model.
    """

    type: Literal["session.child_session.updated"]
    conversation_id: str
    child_session_id: str
    child: dict[str, Any]


class SessionChangedFilesInvalidatedEvent(_SSEEventBase):
    """
    The session's changed-files list may have changed — refetch it.

    A coarse "something changed" signal (per-file events aren't available
    for git-mode workspaces) emitted by the runner after a file-mutating
    tool. The web treats it as a refetch trigger for the changed-files
    panel; transient (not persisted — the REST list is source of truth).

    :param type: Always ``"session.changed_files.invalidated"``.
    :param session_id: Owning session/conversation id.
    :param environment_id: Environment whose changes were invalidated,
        e.g. ``"default"``.
    """

    type: Literal["session.changed_files.invalidated"]
    session_id: str
    # "default" is the canonical primary-environment id
    # (DEFAULT_ENVIRONMENT_ID); the changed-files panel only tracks that
    # environment, so it's the sole expected value, not an invented one.
    environment_id: str = "default"


class SessionTerminalActivityEvent(_SSEEventBase):
    """
    A terminal's pane produced output (runner-determined activity pulse).

    Powers the web "active" badge for any terminal without a client PTY
    attach — the runner's per-terminal pane watcher emits this when the
    pane content changes. Transient (a live pulse; not persisted, not in
    the connect snapshot).

    :param type: Always ``"session.terminal.activity"``.
    :param session_id: Owning session/conversation id.
    :param terminal_id: Opaque terminal resource id, e.g.
        ``"terminal_zsh_s1"``.
    """

    type: Literal["session.terminal.activity"]
    session_id: str
    terminal_id: str


class TurnStartedEvent(_SSEEventBase):
    """
    Emitted when the runner starts a new turn for a session.

    :param type: Fixed literal ``"turn.started"``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """

    type: Literal["turn.started"]
    session_id: str


class TurnCompletedEvent(_SSEEventBase):
    """
    Emitted when a turn finishes successfully with no pending work.

    :param type: Fixed literal ``"turn.completed"``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """

    type: Literal["turn.completed"]
    session_id: str


class TurnFailedEvent(_SSEEventBase):
    """
    Emitted when a turn fails due to an LLM error, timeout, or crash.

    :param type: Fixed literal ``"turn.failed"``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param error: Error details, e.g.
        ``{"message": "LLM timeout", "type": "TimeoutError"}``.
    """

    type: Literal["turn.failed"]
    session_id: str
    error: dict[str, Any] = Field(default_factory=dict)


class TurnCancelledEvent(_SSEEventBase):
    """
    Emitted when a turn is interrupted by the user or system.

    :param type: Fixed literal ``"turn.cancelled"``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """

    type: Literal["turn.cancelled"]
    session_id: str


# ServerStreamEvent: every event a stream consumer (AP-as-harness-
# client OR an external client of AP) may receive on either of
# AP's two SSE endpoints. Pydantic dispatches on the ``type``
# field via ``Field(discriminator="type")``; each variant's
# ``Literal[...]`` pins the correct branch.
#
# Usage:
#     from pydantic import TypeAdapter
#     from omnigent_client.wire import ServerStreamEvent
#     adapter = TypeAdapter(ServerStreamEvent)
#     event = adapter.validate_python(raw_dict)
#     # ``event`` is now the concrete typed model.
#
# Renamed from ``ResponseStreamEvent`` to disambiguate from the
# OpenAI SDK's identically-named type used inside
# ``omnigent.llms.types``.
ServerStreamEvent = Annotated[
    # ── Transient (SSE-only) — session.* lifecycle ─────────────
    SessionStatusEvent
    | SessionUsageEvent
    | SessionModelEvent
    | SessionTitleEvent
    | SessionReasoningEffortEvent
    | SessionCollaborationModeEvent
    | SessionPermissionModeEvent
    | SessionCodexApprovalModeEvent
    | SessionAgentChangedEvent
    | SessionTodosEvent
    | SessionTerminalPendingEvent
    | SessionSandboxStatusEvent
    | SessionMcpStartupEvent
    | SessionModelOptionsEvent
    | SessionInputConsumedEvent
    | SessionInterruptedEvent
    | SessionCreatedEvent
    | SessionSupersededEvent
    | SessionBtwSidechatEvent
    | SessionPresenceEvent
    # ── Transient (SSE-only) — session resource lifecycle ─────
    | SessionResourceCreatedEvent
    | SessionResourceDeletedEvent
    | SessionChildSessionUpdatedEvent
    | SessionChangedFilesInvalidatedEvent
    | SessionTerminalActivityEvent
    # ── Transient (SSE-only) — incremental token deltas ────────
    | OutputTextDeltaEvent
    | ToolOutputDeltaEvent
    | ReasoningStartedEvent
    | ReasoningTextDeltaEvent
    | ReasoningSummaryTextDeltaEvent
    # ── Persistent (POST + SSE replay) — wraps conv-store items
    | OutputItemDoneEvent
    # ── Transient (SSE-only) — file annotations / keepalive ────
    | OutputFileDoneEvent
    | HeartbeatEvent
    | SessionHeartbeatEvent
    # ── Transient (SSE-only) — synchronous decision request ────
    | ElicitationRequestEvent
    | ElicitationResolvedEvent
    # ── Transient (SSE-only) — embedded-browser action request ─
    | BrowserActionRequestEvent
    | UiOpenFileEvent
    # ── Transient (SSE-only) — native policy DENY signal ───────
    | PolicyDeniedEvent
    # ── Transient (SSE-only) — Responses-API turn lifecycle ────
    | CreatedEvent
    | QueuedEvent
    | InProgressEvent
    | CompletedEvent
    | FailedEvent
    | CancelledEvent
    | IncompleteEvent
    # ── Transient (SSE-only) — operational signals ─────────────
    | RetryEvent
    | ErrorEvent
    | CompactionInProgressEvent
    | CompactionCompletedEvent
    | CompactionFailedEvent
    | ClientTaskCancelEvent
    | TurnStartedEvent
    | TurnCompletedEvent
    | TurnFailedEvent
    | TurnCancelledEvent,
    Field(discriminator="type"),
]
