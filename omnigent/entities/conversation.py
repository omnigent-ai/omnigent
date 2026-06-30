"""Conversation entities — conversation, items, and item data types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Attachment path markers the native executors prepend to prompt text
# ("[Attached: /tmp/.../x.png]" from claude-native's _content_to_text,
# "[Attached file: /tmp/...]" from codex-native's _file_block_to_input_item).
# Those markers round-trip through the vendor transcript as user-message
# text, so without filtering them a session started with an image is
# titled by a temp-file path instead of what the user typed. Matched per
# line by synthesize_conversation_title; keep in sync with
# omnigent/inner/claude_native_executor.py and
# omnigent/inner/codex_native_executor.py.
_ATTACHMENT_MARKER_RE = re.compile(r"^\[Attached(?: file)?: .+\]$")

# ── Conversation ──────────────────────────────────────


@dataclass
class Conversation:
    """
    A conversation grouping related turns.

    :param id: Unique conversation identifier,
        e.g. ``"conv_abc123"``.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last
        update (item append, title change, etc.).
    :param title: Optional user-assigned title. Phase 4 named
        sub-agents store ``"<type>:<name>"`` here so the partial
        unique index on ``(parent_conversation_id, title)`` can
        enforce uniqueness within a parent.
    :param kind: Conversation type. ``"default"`` for
        user-initiated, ``"sub_agent"`` for sub-agent
        execution conversations.
    :param parent_conversation_id: Phase 4 — for child
        sub-agent conversations, points at the owning parent
        conversation. ``None`` for top-level conversations.
    :param root_conversation_id: For child sub-agent
        conversations, the id of the root (top-level) conversation
        in the spawn tree. Equal to ``id`` for top-level
        conversations. Powers O(1) tree-scoped lookups so any
        agent in a tree can peek at any other by
        ``conversation_id`` without walking the parent chain.
    :param agent_id: Foreign key to the agent bound to this
        conversation at creation time. ``None`` only for legacy
        rows or callers that cannot bind a conversation.
    :param runner_id: Runner the conversation is pinned to (hard
        affinity per ``designs/RUNNER.md`` §5). ``None`` until
        the first dispatch claims a runner; thereafter every
        subsequent dispatch routes to this runner while it is
        online (or fails with ``runner_unavailable`` if not).
    :param host_id: Host that launched (or should launch) the
        runner for this session. Set when a session is created
        from the Web UI targeting a specific host. ``None`` for
        sessions started via ``omnigent run`` (the CLI
        orchestrates runner spawning directly). Used for
        retry-on-reconnect: if the server restarts before the
        runner connects, the server re-sends the launch request
        to this host.
    :param labels: Session-scoped guardrails labels persisted
        in ``conversation_labels``. Populated by
        :meth:`ConversationStore.get_conversation` via a JOIN;
        empty dict when no labels have been written yet. Labels
        survive conversation_items compaction by design
        (POLICIES.md §6.3) — the two tables are
        independent.
    :param session_state: Mutable per-conversation key/value
        store used by policy callables to accumulate state
        across turns (e.g. running counters, audit trails).
        Persisted as a JSON column on the ``conversations``
        table and loaded by the policy engine builder at
        workflow start. Empty dict when no state has been
        written yet.
    :param session_usage: Cumulative LLM token usage for the
        session. Shape: ``{"input_tokens": N, "output_tokens": M,
        "total_tokens": T, "total_cost_usd": C}`` plus an optional
        nested ``"by_model"`` sub-dict keyed by the raw harness model
        id, each holding the same per-bucket token keys (and
        ``total_cost_usd`` when that model's turns were priced), e.g.
        ``{"by_model": {"claude-sonnet-4-6": {"input_tokens": N, ...}}}``.
        Typed ``dict[str, Any]`` (not ``dict[str, float]``) to admit the
        nested ``by_model`` object. Persisted as a JSON column and
        loaded by the policy engine builder at workflow start. Empty
        dict when no LLM calls have been recorded yet.
    :param reasoning_effort: Per-session reasoning-effort hint,
        e.g. ``"high"``. ``None`` means use the agent default.
        Set at session creation via ``POST /v1/sessions`` metadata
        and mutable thereafter via ``PATCH /v1/sessions/{id}``
        (alongside the runner-binding primitive of the Alpha
        runner-state design). Both paths validate the value against
        the supported set; invalid values fail with ``invalid_input``.
    :param model_override: Per-session LLM model override,
        e.g. ``"claude-opus-4-7"``. ``None`` means use the agent
        default from the spec's ``llm.model``. Mutable via
        ``PATCH /v1/sessions/{id}`` and the REPL's ``/model``
        command. Mirrors the persistence shape of
        ``reasoning_effort`` so the web UI and the TUI stay
        in sync — both read it from the session snapshot and
        write it through the same PATCH endpoint.
    :param cost_control_mode_override: Per-session cost-control
        switch: ``"on"`` activates the spec's configured cost-control
        mode, ``"off"`` disables cost control for this session, and
        ``None`` (unset) defers to the spec default. Set at session
        creation via ``POST /v1/sessions`` and mutable via
        ``PATCH /v1/sessions/{id}`` (the web "Cost Optimized"
        toggle). Read by the cost-control advisor pipeline at turn
        start; mirrors the persistence shape of ``model_override``.
    :param harness_override: Per-session harness override for the
        bound agent's brain, e.g. ``"pi"`` or ``"openai-agents"``.
        ``None`` means use the harness declared in the agent spec
        (``executor.config.harness``). Set at session creation via
        ``POST /v1/sessions`` (the new-chat harness picker) and
        immutable thereafter — the runner spawns the harness on the
        first turn, so a later switch would orphan the running
        process. Only valid for ``executor.type: omnigent`` agents;
        the create route validates against ``OMNIGENT_HARNESSES``.
        Sub-agent sessions never *inherit* the parent brain's override,
        so e.g. polly's workers keep their declared harnesses when the
        brain is overridden. A sub-agent session MAY, however, carry its
        own create-time override when ``sys_session_send`` supplied an
        allowlisted ``args.harness`` (gated by the sub-agent spec's
        ``executor.config.allowed_harnesses``); that value is set on the
        child's own row, not inherited.
    :param sub_agent_name: For sub-agent sessions (``kind="sub_agent"``),
        the sub-agent type name within the parent's spec tree,
        e.g. ``"summarizer"``. The runner uses this to resolve the
        sub-agent's :class:`AgentSpec` from the parent's
        ``sub_agents`` list instead of using the parent's spec
        directly. ``None`` for top-level sessions. Replaces
        ``task.agent_name`` from the removed task store
        (RUNNER_SUBAGENT_DISPATCH.md).
    :param external_session_id: Runtime-native session id this
        conversation wraps, e.g. Claude Code's session uuid for
        ``omnigent claude`` sessions. ``None`` for regular
        AP-only conversations. Populated by the wrapper bridge
        from the underlying runtime and used by ``--resume`` to
        recover the external session's prior transcript on a
        fresh runner. Generic across runtimes — at most one
        external session per conversation.
    :param terminal_launch_args: Pass-through CLI args for a native
        terminal wrapper (claude / codex), e.g.
        ``["--dangerously-skip-permissions"]``. ``None`` for
        non-native sessions, or a native session launched with no
        extra args. Set at session create (so the runner has them
        before it boots) and updated on resume via
        ``PATCH /v1/sessions/{id}`` (last-write-wins). The runner
        reconstructs the terminal launch command from these plus the
        harness binary; the command and all bridge / Omnigent / auth wiring
        stay runner-owned and are never stored here. A flat list (not
        a dict) is deliberate — there is no key for a user to smuggle
        internal wiring through. See
        designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    :param workspace: Absolute path on disk where the runner cd's,
        e.g. ``"/Users/corey/universe/src/foo"``. Required when
        ``host_id`` is set (enforced by a check constraint at the
        DB layer); optional for CLI-launched sessions that record
        their starting cwd for display. Stored as the canonicalized
        realpath returned by ``host.stat`` at session-create time;
        symlinks are pre-resolved so the agent's ``os_env.cwd``
        boundary check cannot be smuggled past. Immutable after
        creation — see designs/SESSION_WORKSPACE_SELECTION.md. When
        a git worktree was created for the session, this is the
        worktree directory path rather than the picked source repo.
    :param git_branch: Git branch checked out in the session's
        worktree, e.g. ``"feature/login"``. Set only when the
        session was created with a server-created git worktree (the
        ``git`` block of ``POST /v1/sessions``); ``None`` otherwise.
        ``git_branch IS NOT NULL`` is the gate for offering worktree
        cleanup on session delete. See
        designs/SESSION_GIT_WORKTREE.md.
    :param archived: Whether the session is archived. Archived
        sessions are hidden from the default ``GET /v1/sessions``
        listing (and the sidebar), surfacing only when the caller
        passes ``include_archived=True``. ``False`` for normal
        sessions; toggled via ``PATCH /v1/sessions/{id}``.
    """

    id: str
    created_at: int
    updated_at: int
    root_conversation_id: str
    title: str | None = None
    kind: str = "default"
    parent_conversation_id: str | None = None
    agent_id: str | None = None
    runner_id: str | None = None
    host_id: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    session_state: dict[str, Any] = field(default_factory=dict)
    session_usage: dict[str, Any] = field(default_factory=dict)
    reasoning_effort: str | None = None
    model_override: str | None = None
    cost_control_mode_override: str | None = None
    harness_override: str | None = None
    sub_agent_name: str | None = None
    external_session_id: str | None = None
    terminal_launch_args: list[str] | None = None
    workspace: str | None = None
    git_branch: str | None = None
    archived: bool = False


# ── Conversation item data types ───────────────────────


class MessageData(BaseModel):
    """
    Data for a message item (user or assistant).

    :param role: ``"user"`` or ``"assistant"``.
    :param content: Heterogeneous content blocks, e.g.
        ``[{"type": "input_text", "text": "Hello"}]``.
    :param agent: Agent name (required for assistant messages,
        absent for user). Serialized as ``"model"`` in JSON.
    :param is_meta: ``True`` for durable context that must be
        replayed to agents but hidden from user-facing transcripts,
        e.g. injected skill instructions. Defaults to ``False``
        and is omitted from serialized payloads in that case.
    :param interrupted: ``True`` when an assistant message is a
        durable partial response from an interrupted external-native
        turn, e.g. Codex ``turn/completed`` with status
        ``"interrupted"``. Defaults to ``False`` and is omitted from
        serialized payloads in that case.
    """

    role: Literal["user", "assistant"]
    # Heterogeneous content blocks (input_text, output_text, input_image, etc.)
    content: list[dict[str, Any]]
    agent: str | None = Field(default=None, serialization_alias="model")
    is_meta: bool = Field(default=False, exclude_if=lambda value: value is False)
    interrupted: bool = Field(default=False, exclude_if=lambda value: value is False)

    @model_validator(mode="after")
    def check_agent_for_assistant(self) -> MessageData:
        """
        Validate that assistant messages have an agent and user
        messages do not.

        :returns: The validated instance.
        :raises ValueError: If an assistant message is missing
            ``agent``.
        """
        if self.role == "assistant" and self.agent is None:
            raise ValueError("assistant messages require 'agent'")
        return self


def synthesize_conversation_title(
    content: list[dict[str, Any]],
    *,
    limit: int = 60,
) -> str | None:
    """
    Derive a one-line conversation title from message content blocks.

    Non-text blocks (``input_image``, ``input_file``) are skipped, and
    lines matching the native executors' attachment path markers
    (:data:`_ATTACHMENT_MARKER_RE`) are dropped so attachments never
    leak temp-file paths into the title.

    :param content: Message content blocks, e.g.
        ``[{"type": "input_text", "text": "Hello"}]``.
    :param limit: Max chars before truncating with an ellipsis.
    :returns: Collapsed/truncated title, or ``None`` when no
        usable text is present.
    """
    parts: list[str] = []
    for block in content:
        if block.get("type") == "input_text":
            text = block.get("text")
            if isinstance(text, str):
                kept_lines = [
                    line
                    for line in text.splitlines()
                    if not _ATTACHMENT_MARKER_RE.match(line.strip())
                ]
                parts.append("\n".join(kept_lines))
    collapsed = " ".join(" ".join(parts).split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


class FunctionCallData(BaseModel):
    """
    Data for a function_call item.

    :param agent: Agent name. Serialized as ``"model"`` in JSON.
    :param name: Tool function name, e.g. ``"search.web"``.
    :param arguments: JSON-encoded arguments string.
    :param call_id: Unique call identifier from the LLM,
        e.g. ``"call_abc123"``.
    """

    agent: str = Field(serialization_alias="model")
    name: str
    arguments: str
    call_id: str


class FunctionCallOutputData(BaseModel):
    """
    Data for a function_call_output item.

    :param call_id: The call_id this output corresponds to,
        e.g. ``"call_abc123"``.
    :param output: The tool's string result.
    """

    call_id: str
    output: str


class ErrorData(BaseModel):
    """
    Data for a persisted error banner item.

    These items mirror ``response.error`` events so clients can render
    the same error banner after reconnect / refresh. They are listed in
    :data:`NON_CONTENT_ITEM_TYPES` because they are operator-visible
    transcript metadata, not content the next agent turn should receive.

    :param source: Error source, e.g. ``"execution"``.
    :param code: Stable error classifier, e.g.
        ``"native_terminal_start_failed"``.
    :param message: Human-readable error message, e.g.
        ``"Native Codex requires the 'codex' CLI on PATH."``.
    """

    source: Literal["llm", "execution", "tool"]
    code: str
    message: str

    @field_validator("code", "message")
    @classmethod
    def require_non_empty_text(cls, value: str) -> str:
        """
        Validate that required error text is present.

        :param value: Error code or message value, e.g.
            ``"native_terminal_start_failed"``.
        :returns: The stripped non-empty value.
        :raises ValueError: If the value is empty or whitespace-only.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("error code and message must be non-empty")
        return stripped


class ReasoningData(BaseModel):
    """
    Data for a reasoning item.

    :param agent: Agent name. Serialized as ``"model"`` in JSON.
    :param summary: Summary text blocks,
        e.g. ``[{"type": "summary_text", "text": "..."}]``.
    :param content: Raw reasoning content blocks, or ``None`` if
        redacted.
    :param encrypted_content: Encrypted reasoning content, or
        ``None``.
    """

    agent: str = Field(serialization_alias="model")
    # Summary text blocks, e.g. [{"type": "summary_text", "text": "..."}]
    summary: list[dict[str, str]]
    # Raw reasoning content blocks; nullable (may be redacted).
    content: list[dict[str, str]] | None = None
    encrypted_content: str | None = None


class CompactionData(BaseModel):
    """
    Data payload for a compaction summary item.

    Stored as a conversation item of ``type="compaction"``.
    The summary covers all items from the start of the
    conversation (or the previous compaction item) through
    the item identified by ``last_item_id``.

    :param summary: The LLM-generated summary text covering
        all conversation items up through ``last_item_id``,
        e.g. ``"User asked to analyze a dataset. Agent loaded
        data.csv and computed statistics."``.
    :param last_item_id: The item ID (inclusive) of the last
        conversation item covered by this summary, e.g.
        ``"msg_abc123"``. Items at positions <= this item are
        summarized and do not need to be loaded for prompt
        construction.
    :param model: The model used to generate the summary,
        e.g. ``"openai/gpt-4o"``.
    :param token_count: Approximate token count of the summary
        text, for budget tracking, e.g. ``342``.
    """

    summary: str
    last_item_id: str
    model: str | None = None
    token_count: int
    compacted_messages: list[dict[str, Any]] | None = None
    window_id: int | None = None


class NativeToolData(BaseModel):
    """
    A provider-native tool output item (e.g. ``web_search_call``).

    These are executed server-side by the LLM provider and returned
    as opaque dicts. Agent-plane persists and replays them so the
    LLM sees its own tool results on subsequent iterations.

    :param item: The raw dict from the Responses API output, e.g.
        ``{"type": "web_search_call", "id": "ws_abc",
        "status": "completed", "action": {...}}``.
    """

    item: dict[str, Any]


class ResourceEventData(BaseModel):
    """Data payload for a persisted resource lifecycle event.

    These items are written to the conversation store when a
    session resource is created or deleted, so reconnecting
    clients can discover resource history without replaying the
    live SSE stream.  The agent loop filters them out of the
    LLM's message context (they are metadata, not conversation
    content).

    :param event_type: The SSE event type literal, e.g.
        ``"session.resource.created"`` or
        ``"session.resource.deleted"``.
    :param resource_id: Opaque id of the affected resource,
        e.g. ``"terminal_bash_s1"`` or ``"file_abc123"``.
    :param resource_type: Kind of resource, e.g.
        ``"terminal"``, ``"file"``, ``"environment"``.
    :param resource: Full resource object dict for ``created``
        events. ``None`` for ``deleted`` events.
    """

    event_type: str
    resource_id: str
    resource_type: str
    resource: dict[str, Any] | None = None


class TerminalCommandData(BaseModel):
    """
    Data payload for a runner-side terminal command (``!cmd``) observed
    in a harness transcript (today: Claude Code's embedded TUI).

    Listed in :data:`NON_CONTENT_ITEM_TYPES` so the agent loop never
    injects this as phantom content into the LLM's message history.

    Claude Code writes two sibling transcript records per ``!cmd``
    invocation: one ``<bash-input>`` record and one combined
    ``<bash-stdout>``/``<bash-stderr>`` record. Each maps to one
    ``terminal_command`` item with ``kind="input"`` or
    ``kind="output"`` respectively.

    :param kind: ``"input"`` for the command text, ``"output"`` for
        the combined stdout/stderr result.
    :param input: The raw command string, e.g. ``"pwd"``. Present when
        ``kind="input"``, ``None`` otherwise.
    :param stdout: Captured stdout text. Present when ``kind="output"``,
        ``None`` otherwise.
    :param stderr: Captured stderr text. Present when ``kind="output"``,
        ``None`` otherwise.
    """

    kind: Literal["input", "output"]
    input: str | None = None
    stdout: str | None = None
    stderr: str | None = None


class RoutingDecisionData(BaseModel):
    """
    Data payload for an intelligent model-router decision item.

    Emitted by the runner's per-turn cost advisor at the START of an
    advised turn (see :func:`omnigent.runner.cost_advisor`) and persisted
    as a display-only transcript item so the model the router chose shows
    in the conversation flow the moment the turn begins. Listed in
    :data:`NON_CONTENT_ITEM_TYPES` so the agent loop's history filter
    skips it — the brain never sees (or answers) its own router note. The
    runner's harness-input builder also drops every non
    message/function_call type, a second guarantee it stays out of the
    model's context.

    :param model: The concrete brain model the router chose, e.g.
        ``"databricks-claude-opus-4-8"``.
    :param tier: The difficulty tier the router assigned, one of
        ``"cheap"`` / ``"medium"`` / ``"expensive"``, e.g.
        ``"expensive"``.
    :param applied: ``True`` when the brain actually ran on
        :attr:`model` this turn (optimize mode, no user pin); ``False``
        when the router only WOULD have picked it (advise/shadow mode, or
        a user model pin won) — the UI renders "would have picked".
    :param rationale: The router's one-line explanation, shown as muted
        secondary text, e.g. ``"Multi-file refactor needs deep
        reasoning."``.
    """

    model: str
    tier: Literal["cheap", "medium", "expensive"]
    applied: bool
    rationale: str

    @field_validator("model")
    @classmethod
    def require_non_empty_model(cls, value: str) -> str:
        """
        Validate that the router named a non-empty model.

        :param value: The chosen model id, e.g.
            ``"databricks-claude-opus-4-8"``.
        :returns: The stripped non-empty model id.
        :raises ValueError: If the model id is empty or whitespace-only —
            a routing decision with no model is meaningless to render.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("routing_decision model must be non-empty")
        return stripped


class SlashCommandData(BaseModel):
    """
    Data payload for a slash-command invocation observed in a
    harness transcript (today: Claude Code's embedded TUI).

    Listed in :data:`NON_CONTENT_ITEM_TYPES` so the agent loop's
    history filter skips it — a downstream LLM never sees this as a
    phantom tool call. Field names mirror ``function_call`` so the
    web renderer can reuse the tool-card layout.

    :param agent: Harness/agent name, e.g. ``"claude-native-ui"``.
        Serialized as ``"model"`` for parity with other items.
    :param kind: ``"skill"`` for plugin/Skill invocations,
        ``"command"`` for surfaced CLI built-ins (``/effort``,
        ``/clear``, ``/compact``, ``/model``, ``/ultrareview``).
        The web renderer uses this to pick the prefix label and
        icon. Defaults to ``"skill"`` so persisted items predating
        this field deserialize without backfill.
    :param name: Command name with leading ``/`` stripped, e.g.
        ``"dev-productivity:simplify"``.
    :param arguments: Raw ``<command-args>`` text. Empty when none.
    :param output: ``<local-command-stdout>`` text when present,
        else ``None`` (the common case — Skills act via the next
        assistant turn, not stdout).
    """

    agent: str = Field(serialization_alias="model")
    kind: Literal["skill", "command"] = "skill"
    name: str
    arguments: str
    output: str | None = None


ItemData = (
    MessageData
    | FunctionCallData
    | FunctionCallOutputData
    | ErrorData
    | ReasoningData
    | CompactionData
    | NativeToolData
    | ResourceEventData
    | RoutingDecisionData
    | SlashCommandData
    | TerminalCommandData
)

ITEM_TYPE_TO_DATA_CLS: dict[str, type[BaseModel]] = {
    "message": MessageData,
    "function_call": FunctionCallData,
    "function_call_output": FunctionCallOutputData,
    "error": ErrorData,
    "reasoning": ReasoningData,
    "compaction": CompactionData,
    "native_tool": NativeToolData,
    "resource_event": ResourceEventData,
    "routing_decision": RoutingDecisionData,
    "slash_command": SlashCommandData,
    "terminal_command": TerminalCommandData,
}

# Item types that are metadata / lifecycle events — not content
# the agent loop should include in the LLM's message context.
# Used by _sync_history and _load_initial_history to filter.
NON_CONTENT_ITEM_TYPES: frozenset[str] = frozenset(
    {
        "compaction",
        "error",
        "resource_event",
        "routing_decision",
        "slash_command",
        "terminal_command",
    }
)


def parse_item_data(item_type: str, raw: dict[str, Any]) -> ItemData:
    """
    Parse a raw dict into the appropriate ItemData model.

    Used by store implementations when deserializing from DB.

    :param item_type: The item type string, e.g. ``"message"``,
        ``"function_call"``.
    :param raw: The raw dict from the DB ``data`` column.
    :returns: A validated ItemData instance.
    :raises ValueError: If ``item_type`` is unknown.
    """
    cls = ITEM_TYPE_TO_DATA_CLS.get(item_type)
    if cls is None:
        raise ValueError(f"unknown item type: {item_type!r}")
    return cls(**raw)  # type: ignore[return-value]


def _validate_type_matches_data(item_type: str, data: ItemData) -> None:
    """
    Validate that ``data`` is the correct model for ``item_type``.

    :param item_type: The declared type string, e.g. ``"message"``.
    :param data: The data model instance to validate.
    :raises ValueError: If ``item_type`` is unknown or ``data`` is
        the wrong model.
    """
    expected = ITEM_TYPE_TO_DATA_CLS.get(item_type)
    if expected is None:
        raise ValueError(f"unknown item type: {item_type!r}")
    if not isinstance(data, expected):
        raise ValueError(
            f"item type {item_type!r} requires {expected.__name__}, got {type(data).__name__}"
        )


# ── Conversation items ─────────────────────────────────


class NewConversationItem(BaseModel):
    """
    An item that has not yet been persisted. No ID or timestamp.

    :param type: Item type, e.g. ``"message"``,
        ``"function_call"``.
    :param response_id: The task/response ID this item belongs to.
    :param data: The typed data payload (MessageData, etc.).
    :param created_by: Identity of the human actor who authored this
        item (e.g. ``"alice@example.com"``), or ``None`` for
        agent/tool/system-generated items and single-user mode.
        Mirrors the comment ``created_by`` contract.
    """

    type: str
    response_id: str
    data: ItemData
    created_by: str | None = None

    @model_validator(mode="after")
    def check_type_matches_data(self) -> NewConversationItem:
        """
        Ensure ``type`` field is consistent with ``data`` model.

        :returns: The validated instance.
        :raises ValueError: If ``type`` does not match ``data``.
        """
        _validate_type_matches_data(self.type, self.data)
        return self


class ConversationItem(BaseModel):
    """
    A persisted item with a store-assigned ID.

    :param id: Store-assigned item ID, e.g. ``"msg_abc123"``.
    :param type: Item type, e.g. ``"message"``,
        ``"function_call"``.
    :param status: Item status, e.g. ``"completed"``.
    :param response_id: The task/response ID this item belongs to.
    :param created_at: Unix epoch timestamp of creation.
    :param data: The typed data payload (MessageData, etc.).
    :param created_by: Identity of the human actor who authored this
        item, or ``None`` for agent/tool/system items and single-user
        mode. Lets owner and collaborator messages be distinguished.
    """

    id: str
    type: str
    status: str
    response_id: str
    created_at: int
    data: ItemData
    created_by: str | None = None

    @model_validator(mode="after")
    def check_type_matches_data(self) -> ConversationItem:
        """
        Ensure ``type`` field is consistent with ``data`` model.

        :returns: The validated instance.
        :raises ValueError: If ``type`` does not match ``data``.
        """
        _validate_type_matches_data(self.type, self.data)
        return self

    def to_api_dict(self) -> dict[str, Any]:
        """
        Render the item as the flat, JSON-safe shape defined by API.md.

        Common fields (``id``, ``response_id``, ``type``, ``status``)
        come from the item; type-specific fields (``role``,
        ``content``, ``model``, ``name``, ``arguments``, …) come
        from ``self.data`` and are spread onto the top level.
        ``exclude_none=True`` drops absent optional fields (e.g.
        ``model`` on user messages) so they don't show up in the
        output. The return value contains only JSON-serializable
        primitives, so callers can pass it straight to
        :func:`json.dumps` without a custom encoder.

        Single source of truth for the flatten-for-API shape: both
        the ``/v1/sessions`` and ``/v1/conversations`` routes (via
        :func:`_to_api_item`) and the ``check_task`` tool
        (via :func:`_get_recent_activity_for_task`) consume it.

        :returns: Flat dict, e.g. for an assistant message::

            {"id": "msg_abc", "response_id": "resp_xyz",
             "type": "message", "status": "completed",
             "role": "assistant",
             "content": [{"type": "output_text", "text": "hi"}],
             "model": "databricks-gpt-5-4"}
        """
        return {
            "id": self.id,
            "response_id": self.response_id,
            "type": self.type,
            "status": self.status,
            **self.data.model_dump(exclude_none=True, by_alias=True),
            # created_by is present only for human-authored items;
            # omitted (not null) for agent/tool/system messages so the
            # two stay distinguishable, matching exclude_none above.
            **({"created_by": self.created_by} if self.created_by is not None else {}),
        }
