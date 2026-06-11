"""Pydantic models for the API layer — request/response shapes AND
SSE stream events.

This module is split into two sections, separated by a clearly marked
delineator further down:

1. Request and response body schemas for the JSON endpoints.
2. SSE event payload models — the discriminated union that every
   event the server emits over its SSE endpoints validates against.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import ConversationItem

# ── Shared ──────────────────────────────────────────────────────


class PaginatedList(BaseModel):
    """
    A paginated list response following cursor-based pagination.

    :param object: Fixed resource type, always ``"list"``.
    :param data: Page of results. Items are heterogeneous
        (``ResponseObject``, ``ConversationObject``, ``FileObject``,
        or dicts) and list is invariant, so no single concrete type
        satisfies all callers.
    :param first_id: ID of the first item in the page, or ``None``
        if the page is empty, e.g. ``"resp_abc123"``.
    :param last_id: ID of the last item in the page, or ``None``
        if the page is empty, e.g. ``"resp_xyz789"``.
    :param has_more: Whether more items exist beyond this page.
    """

    object: str = "list"
    # Any: items are heterogeneous (ResponseObject, ConversationObject,
    # FileObject, or dicts) and list is invariant, so no single concrete
    # type satisfies all callers.
    data: list[Any] = Field(default_factory=list)
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False


# ── Agents ──────────────────────────────────────────────────────


class MCPServerSummary(BaseModel):
    """
    Safe subset of an MCP server's configuration for API exposure.

    Secret-bearing fields (``headers``, ``env``) are intentionally
    excluded. This model is the wire shape returned inside
    :class:`AgentObject` so clients can display which MCP servers
    an agent is connected to without leaking credentials.

    :param name: Server name as declared in the agent spec,
        e.g. ``"github"``.
    :param transport: Transport type — ``"stdio"`` or ``"http"``.
    :param description: Optional free-text description from the
        spec, e.g. ``"GitHub MCP server"``. ``None`` when unset.
    :param url: HTTP(S) endpoint URL for ``transport="http"``
        servers, e.g. ``"https://mcp.example.com/sse"``. ``None``
        for stdio servers.
    :param command: Executable path for ``transport="stdio"``
        servers, e.g. ``"uvx"``. ``None`` for http servers.
    :param args: Command-line arguments for ``transport="stdio"``
        servers, e.g. ``["mcp-server-github"]``. Empty list
        when unset.
    """

    name: str
    transport: str
    description: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)


class SkillSummary(BaseModel):
    """
    Safe subset of a discovered skill for API exposure.

    Surfaces the skill name and one-line description so clients
    (e.g. the web composer's slash-command menu) can list which
    skills the session has access to. The full skill ``content``
    is intentionally omitted — it's only loaded server-side when
    the harness invokes the skill, and it can be large.

    :param name: Skill identifier as parsed from the SKILL.md
        frontmatter, e.g. ``"triage-issues"``. Lowercase
        kebab-case.
    :param description: One-line summary from the SKILL.md
        frontmatter, e.g. ``"Triage open GitHub issues in the
        repo."``.
    """

    name: str
    description: str


class PolicySummary(BaseModel):
    """
    Safe subset of a policy's spec for API exposure.

    Exposes the policy name, type, and phases so the UI can
    display which guardrails are active on an agent. The full
    policy body (prompt text, callable path, label conditions)
    is intentionally excluded — this is a summary for display,
    not a full spec.

    :param name: Policy name as declared in the agent spec,
        e.g. ``"block_long_sleep"``.
    :param type: Policy type discriminator — ``"function"``
        or ``"prompt"``.
    :param on: List of phase selectors the policy fires on,
        e.g. ``["tool_call"]`` or ``["request", "response"]``.
    :param description: Short detail string about the policy
        implementation. For function policies: the callable
        dotted path. For prompt policies: the first line of
        the prompt. ``None`` when not available.
    """

    name: str
    type: str
    on: list[str]
    description: str | None = None


class AgentObject(BaseModel):
    """
    API representation of a registered agent.

    :param id: Unique agent identifier, e.g. ``"ag_abc123"``.
    :param object: Fixed resource type, always ``"agent"``.
    :param name: Human-readable agent name,
        e.g. ``"research-agent"``.
    :param version: Monotonic version counter. Starts at 1,
        incremented on each update.
    :param description: Optional free-text description of the
        agent's purpose.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last update,
        or ``None`` if never updated.
    :param harness: The agent's harness/kind, e.g. ``"codex"``,
        ``"codex-native"``, or ``"claude-native"`` for
        ``executor.type: omnigent`` agents, otherwise the executor
        type (``"claude_sdk"``, ``"agents_sdk"``). ``None`` when the
        bundle cannot be loaded. Lets the Web UI Add Agent picker
        recognise an agent's kind (Codex vs Claude) without
        hardcoding by name slug.
    :param mcp_servers: MCP servers the agent is connected to
        (secret fields omitted). Empty list when the spec
        declares no MCP servers or when the bundle cannot be
        loaded.
    :param policies: Guardrails policies declared on the agent.
        Each entry summarises the policy name, type, and
        phases. Empty list when the spec declares no policies
        or when the bundle cannot be loaded.
    :param skills: Skills bundled in the agent spec
        (``skills/<name>/SKILL.md``). Lets the Web UI's
        new-session composer offer a slash-command menu before a
        session (and its runner) exists. Host-discovered skills
        are runner-owned, so they are NOT listed here — the
        session snapshot's ``skills`` field carries the merged
        set once a runner is bound. Empty list when the spec
        bundles no skills or when the bundle cannot be loaded.
    :param terminals: Terminal names declared in the spec's
        ``terminals:`` block, in declaration order, e.g.
        ``["shell"]``. The Web UI gates its "new terminal"
        affordance on this list (creation is only offered for
        agents with terminal access) and offers these names as
        the launchable choices. Empty list when the spec
        declares no terminals or when the bundle cannot be
        loaded.
    """

    id: str
    object: str = "agent"
    name: str
    version: int = 1
    description: str | None = None
    created_at: int
    updated_at: int | None = None
    harness: str | None = None
    mcp_servers: list[MCPServerSummary] = Field(default_factory=list)
    policies: list[PolicySummary] = Field(default_factory=list)
    skills: list[SkillSummary] = Field(default_factory=list)
    terminals: list[str] = Field(default_factory=list)


# ── Session Policies ───────────────────────────────────────────


class SessionPolicyObject(BaseModel):
    """
    API representation of a session-scoped policy.

    Returned by all CRUD endpoints under
    ``/v1/sessions/{session_id}/policies``.

    :param id: Opaque policy identifier, e.g. ``"spol_abc123"``.
        ``None`` for spec-declared policies that are not
        store-persisted.
    :param object: Fixed resource type, always
        ``"session.policy"``.
    :param name: Human-readable policy name,
        e.g. ``"block_non_feature_branch_push"``.
    :param type: Handler discriminator: ``"python"`` or
        ``"url"``.
    :param handler: Dotted import path (python) or HTTP URL
        (url), e.g. ``"github_mcp_policy.block_push"`` or
        ``"https://example.com/policies/eval"``.
    :param factory_params: Dict of kwargs passed to the handler
        when it is a factory function. ``None`` for direct
        callables and ``type="url"`` handlers.
    :param enabled: Whether the engine consults this policy.
    :param source: Origin of the policy: ``"session"`` for
        CRUD-created policies, ``"spec"`` for policies
        declared in the agent YAML. Spec policies cannot be
        patched or deleted.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last
        update, or ``None`` if never updated.
    """

    id: str | None
    object: str = "session.policy"
    name: str
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None
    enabled: bool = True
    source: str = "session"
    created_at: int
    updated_at: int | None = None


_DOTTED_PATH_RE = r"^[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)+$"


class CreateSessionPolicyRequest(BaseModel):
    """
    Request body for ``POST /v1/sessions/{session_id}/policies``.

    :param name: Human-readable policy name. Must be unique
        within the session, e.g.
        ``"block_non_feature_branch_push"``.
    :param type: Handler discriminator: ``"python"`` or
        ``"url"``.
    :param handler: Dotted import path (python) or HTTPS URL
        (url), e.g.
        ``"github_mcp_policy.block_non_misc_push"``
        or ``"https://example.com/policies/eval"``.
    :param factory_params: Optional dict of kwargs passed to the
        handler when it is a factory function. Only valid for
        ``type="python"``, e.g. ``{"limit": 10}``.
    """

    name: str
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_type_and_handler(self) -> CreateSessionPolicyRequest:
        """Reject unknown policy types and validate handler format.

        For ``type="url"``, requires an ``https://`` URL.
        For ``type="python"``, requires a valid dotted import path
        (at least two segments, e.g. ``"pkg.module"``).

        :returns: The validated request unchanged.
        :raises ValueError: If ``type`` is invalid, or ``handler``
            does not match the expected format for the type.
        """
        if self.type not in ("python", "url"):
            raise ValueError(f"type must be 'python' or 'url', got '{self.type}'")
        if self.type == "url":
            if not self.handler.startswith("https://"):
                raise ValueError("handler must be an https:// URL for type 'url'")
        elif self.type == "python":
            if not re.match(_DOTTED_PATH_RE, self.handler):
                raise ValueError(
                    "handler must be a valid dotted import path "
                    "(e.g. 'pkg.module.func') for type 'python'"
                )
        return self


class UpdateSessionPolicyRequest(BaseModel):
    """
    Request body for ``PATCH /v1/sessions/{session_id}/policies/{policy_id}``.

    All fields are optional; ``None`` fields are left unchanged.
    Unknown fields (including ``type``, which is immutable) are
    rejected with ``422``.

    :param name: New policy name. ``None`` leaves it unchanged.
    :param handler: New handler path or URL. ``None`` leaves it
        unchanged.
    :param enabled: New enabled flag. ``None`` leaves it
        unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    handler: str | None = None
    enabled: bool | None = None


# ── Default Policies ──────────────────────────────────────────────


class DefaultPolicyObject(BaseModel):
    """
    API representation of a server-wide default policy.

    Returned by all CRUD endpoints under ``/v1/policies``.

    :param id: Opaque policy identifier, e.g. ``"dpol_abc123"``.
    :param object: Fixed resource type, always
        ``"default_policy"``.
    :param name: Human-readable policy name,
        e.g. ``"block_non_feature_branch_push"``.
    :param type: Handler discriminator: ``"python"`` or
        ``"url"``.
    :param handler: Dotted import path (python) or HTTP URL
        (url), e.g. ``"github_mcp_policy.block_push"`` or
        ``"https://example.com/policies/eval"``.
    :param factory_params: Dict of kwargs passed to the handler
        when it is a factory function. ``None`` for direct
        callables and ``type="url"`` handlers.
    :param enabled: Whether the engine consults this policy.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last
        update, or ``None`` if never updated.
    :param created_by: User ID of the admin who created this
        policy, or ``None`` in single-user mode.
    """

    id: str
    object: str = "default_policy"
    name: str
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None
    enabled: bool = True
    created_at: int
    updated_at: int | None = None
    created_by: str | None = None


class CreateDefaultPolicyRequest(BaseModel):
    """
    Request body for ``POST /v1/policies``.

    :param name: Human-readable policy name. Must be globally
        unique, e.g. ``"block_non_feature_branch_push"``.
    :param type: Handler discriminator: ``"python"``, ``"url"``,
    :param handler: Dotted import path (python) or HTTPS URL
        (url), e.g.
        ``"github_mcp_policy.block_non_misc_push"``
        or ``"https://example.com/policies/eval"``.
    :param factory_params: Optional dict of kwargs passed to the
        handler when it is a factory function. Only valid for
        ``type="python"``, e.g. ``{"limit": 10}``.
    """

    name: str
    type: str
    handler: str
    factory_params: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_type_and_handler(self) -> CreateDefaultPolicyRequest:
        """Reject unknown policy types and validate handler format.

        Same validation rules as :class:`CreateSessionPolicyRequest`.

        :returns: The validated request unchanged.
        :raises ValueError: If ``type`` is invalid, or ``handler``
            does not match the expected format for the type.
        """
        if self.type not in ("python", "url"):
            raise ValueError(f"type must be 'python' or 'url', got '{self.type}'")
        if self.type == "url":
            if not self.handler.startswith("https://"):
                raise ValueError("handler must be an https:// URL for type 'url'")
        elif self.type == "python":
            if not re.match(_DOTTED_PATH_RE, self.handler):
                raise ValueError(
                    "handler must be a valid dotted import path "
                    "(e.g. 'pkg.module.func') for type 'python'"
                )
        return self


class UpdateDefaultPolicyRequest(BaseModel):
    """
    Request body for ``PATCH /v1/policies/{policy_id}``.

    All fields are optional; ``None`` fields are left unchanged.
    Unknown fields (including ``type``, which is immutable) are
    rejected with ``422``.

    :param name: New policy name. ``None`` leaves it unchanged.
    :param handler: New handler path or URL. ``None`` leaves it
        unchanged.
    :param enabled: New enabled flag. ``None`` leaves it
        unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    handler: str | None = None
    enabled: bool | None = None


# ── Files ───────────────────────────────────────────────────────


class FileObject(BaseModel):
    """
    API representation of an uploaded file.

    :param id: Unique file identifier, e.g. ``"file_abc123"``.
    :param object: Fixed resource type, always ``"file"``.
    :param filename: Original filename, e.g. ``"report.pdf"``.
    :param bytes: File size in bytes.
    :param created_at: Unix epoch timestamp of upload.
    """

    id: str
    object: str = "file"
    filename: str
    bytes: int
    created_at: int


# ── Session Resources ───────────────────────────────────────────


class SessionResourceObject(BaseModel):
    """
    API representation of a session-scoped resource handle.

    :param id: Opaque resource identifier, e.g. ``"default"`` or
        ``"terminal_bash_s1"``.
    :param object: Fixed resource type, always ``"session.resource"``.
    :param type: Resource kind, initially ``"environment"``,
        ``"terminal"``, or ``"file"``.
    :param session_id: Owning session/conversation id.
    :param name: Human-readable display name. Not required to be
        globally unique.
    :param metadata: Resource-type-specific metadata.
    :param environment: For terminal resources, the environment id the
        terminal actually runs in. Omitted for non-terminal resources.
    """

    id: str
    object: Literal["session.resource"]
    type: Literal["environment", "terminal", "file"]
    session_id: str
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    environment: str | None = None

    model_config = ConfigDict(extra="forbid", strict=True)


class SessionResourceListPage(BaseModel):
    """Strict runner resource-list wire contract."""

    object: Literal["list"]
    data: list[SessionResourceObject]
    first_id: str | None
    last_id: str | None
    has_more: bool

    model_config = ConfigDict(extra="forbid", strict=True)


class SessionResourcePaginatedList(BaseModel):
    """Public paginated list of session resources."""

    object: Literal["list"] = "list"
    data: list[SessionResourceObject] = Field(default_factory=list)
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False


# ── Conversations ───────────────────────────────────────────────


class ConversationObject(BaseModel):
    """
    API representation of a conversation.

    :param id: Unique conversation identifier,
        e.g. ``"conv_abc123"``.
    :param object: Fixed resource type, always
        ``"conversation"``.
    :param title: Optional user-assigned conversation title.
    :param created_at: Unix epoch timestamp of creation.
    :param updated_at: Unix epoch timestamp of the last
        update, e.g. ``1774118400``.
    :param labels: Session-scoped guardrails labels, mirroring
        the runtime ``Conversation.labels`` dict. Empty dict when
        the PolicyEngine hasn't written any labels yet. Exposed so
        the REPL's Ctrl+O debug overlay can render them at parity
        with the legacy ``omnigent run`` Ctrl+G overview.
    """

    id: str
    object: str = "conversation"
    title: str | None = None
    created_at: int
    updated_at: int
    labels: dict[str, str] = Field(default_factory=dict)


class ConversationDeleted(BaseModel):
    """
    Confirmation payload returned after deleting a conversation.

    :param id: ID of the deleted conversation,
        e.g. ``"conv_abc123"``.
    :param object: Fixed resource type, always
        ``"conversation.deleted"``.
    :param deleted: Always ``True``.
    """

    id: str
    object: str = "conversation.deleted"
    deleted: bool = True


class ConversationRef(BaseModel):
    """
    Lightweight reference to a conversation, used in request and
    response bodies where only the conversation ID is needed.

    :param id: Conversation identifier, e.g. ``"conv_abc123"``.
    """

    id: str


class ChildSessionSummary(BaseModel):
    """
    Summary of a sub-agent (child) session under a parent session.

    Powers ``GET /v1/sessions/{id}/child_sessions``. Lets the web /
    REPL debug surface enumerate sub-agent calls spawned from a
    parent session without parsing parent ``function_call_output``
    JSON handles (the legacy TUI Ctrl+O path). The endpoint is the
    canonical "historical truth" source; the existing transient
    ``session.created`` SSE event handles live incremental updates.

    Fields are derived from the child :class:`Conversation` plus its
    latest :class:`Task` (newest by ``created_at``).

    :param id: Child conversation/session identifier,
        e.g. ``"conv_child123"``.
    :param object: Fixed resource type, always
        ``"child_session"``.
    :param parent_session_id: Parent conversation id (echo of the
        route's ``session_id`` path parameter), e.g.
        ``"conv_parent987"``. Stable join key for clients that
        cache child rows across multiple parents.
    :param title: Sub-agent title, ``"{agent_type}:{session_name}"``
        as written by :func:`omnigent.tools.builtins.spawn._spawn_one`,
        e.g. ``"researcher:auth"``. ``None`` only for legacy /
        malformed rows; the spawn path always sets it.
    :param tool: UI-facing sub-agent label. For Omnigent-spawned
        children this is derived from the prefix of ``title`` before
        the first ``":"``, e.g. ``"researcher"``. For Codex-native
        children this is the Codex-assigned ``agent_nickname`` when
        available, then ``agent_role``, then ``"Codex"``. Falls back
        to the raw title for legacy / malformed rows; ``None`` only
        when ``title`` itself is ``None`` or empty.
    :param session_name: Sub-agent instance name, the suffix of
        ``title`` after the first ``":"``, e.g. ``"auth"``. ``None``
        if ``title`` is ``None`` or missing a colon.
    :param kind: Conversation kind discriminator, always
        ``"sub_agent"`` for rows surfaced by this endpoint.
    :param created_at: Unix epoch timestamp of child creation.
    :param updated_at: Unix epoch timestamp of the child's most
        recent update.
    :param agent_id: Agent id recorded on the latest task,
        e.g. ``"ag_abc123"``. ``None`` if the child has no tasks
        yet (rare — ``_spawn_one`` creates a task atomically with
        the conversation).
    :param agent_name: Agent type recorded on the latest task,
        e.g. ``"researcher"``. Mirrors the ``tool`` prefix in
        ``title`` and is provided alongside it because the title
        is a denormalized string while ``agent_name`` is the
        durable per-task value.
    :param current_task_id: Latest task id for the child
        (newest by ``created_at``), e.g. ``"task_abc123"``.
        ``None`` if no tasks exist.
    :param current_task_status: Status of the latest task,
        e.g. ``"completed"``, ``"in_progress"``, ``"failed"``.
        ``None`` if no tasks exist.
    :param busy: ``True`` when the child's session loop is live.
        Mirrors the algorithm used by ``GET /v1/sessions/{id}`` to
        compute ``status``: read the live in-memory cache first
        (``"running"``/``"waiting"`` → busy), and fall back to the
        latest task's status on cache miss (``"queued"`` /
        ``"in_progress"`` → busy). For NO_DBOS sessions the tasks
        table is not populated during active runs, so the cache
        consult is what keeps the rail's "Working" badge correct.
    :param labels: Session-scoped guardrails labels on the child
        conversation (mirrors :class:`ConversationObject.labels`).
    :param last_message_preview: Single-line preview of the most
        recent message item in the child's conversation, truncated
        to ~150 chars with a trailing ellipsis when longer. ``None``
        when the child has no message items yet (rare — the spawn
        tool immediately commits a user message). Lets the UI
        render a real-time "what's the sub-agent saying right now"
        line without fetching the child's full item history.
    :param pending_elicitations_count: Number of approval / input
        prompts the child is currently blocked on, read from the
        server's :mod:`omnigent.runtime.pending_elicitations`
        index. ``> 0`` means the sub-agent is parked awaiting user
        input — the Agents rail renders an "awaiting input" badge so
        a fanned-out sub-agent that needs attention is visible
        without opening its chat. Mirrors
        :attr:`SessionListItem.pending_elicitations_count`.
    """

    id: str
    object: str = "child_session"
    parent_session_id: str
    title: str | None = None
    tool: str | None = None
    session_name: str | None = None
    kind: str = "sub_agent"
    created_at: int
    updated_at: int
    agent_id: str | None = None
    agent_name: str | None = None
    current_task_id: str | None = None
    current_task_status: str | None = None
    busy: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    last_message_preview: str | None = None
    pending_elicitations_count: int = 0


# ── Responses ───────────────────────────────────────────────────


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
    """

    input_tokens: int = 0
    output_tokens: int = 0
    output_tokens_details: UsageDetails = Field(default_factory=UsageDetails)
    total_tokens: int = 0
    context_tokens: int | None = None
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model: str | None = None


class ErrorDetail(BaseModel):
    """
    Machine-readable error information attached to a failed response.

    :param code: Error code string, e.g. ``"server_error"``,
        ``"invalid_input"``.
    :param message: Human-readable error description.
    """

    code: str
    message: str


class IncompleteDetails(BaseModel):
    """
    Details explaining why a response is incomplete.

    :param reason: Reason the response stopped early, e.g.
        ``"max_output_tokens"``, ``"max_tool_calls"``.
    """

    reason: str


class CreateResponseRequest(BaseModel):
    """
    Internal request body the harness scaffold builds for each turn.

    Originally the ``POST /v1/responses`` request schema; that route
    was removed but the harness scaffold still synthesizes this shape
    internally to drive an executor turn.

    :param model: Agent name to invoke, e.g.
        ``"research-agent"``. Must match a registered agent.
    :param input: User input — either a plain string (converted
        to a single ``input_text`` block) or a list of content
        blocks, e.g.
        ``[{"type": "input_text", "text": "Hello"}]``.
    :param stream: If ``True``, return an SSE stream instead of
        blocking until completion.
    :param background: If ``True``, the task runs in the
        background and the caller may poll for results.
    :param store: Must be ``True`` (persisted responses). The
        server rejects ``False``.
    :param instructions: Per-request system instructions that
        override the agent's default instructions.
    :param previous_response_id: ID of the prior response in the
        conversation thread, e.g. ``"resp_abc123"``. Enables
        multi-turn continuation and steering.
    :param conversation: Explicit conversation reference for
        fork validation. Must match the conversation that owns
        ``previous_response_id``.
    :param reasoning: Reasoning configuration,
        e.g. ``{"effort": "medium"}``.
    :param model_override: Optional per-request LLM model override,
        e.g. ``"openai/gpt-5.4-mini"``. Distinct from ``model``
        (agent name). Substitutes for the spec's ``llm.model`` for
        this single request. Drives the REPL's ``/model`` command.
    :param context_management: Compaction strategy objects,
        e.g. ``[{"type": "compaction", ...}]``.
    :param temperature: Ignored — agent controls this. Silently
        dropped.
    :param top_p: Ignored — agent controls this. Silently
        dropped.
    :param tools: Optional list of client-specified tools in standard
        OpenAI function format. When the LLM invokes one, the
        ``function_call`` output items are returned to the caller (the
        response completes) rather than being executed server-side. The
        caller handles execution and continues via
        ``previous_response_id``. Returns 400 if any entry is malformed
        or missing ``function.name``, e.g.
        ``[{"type": "function", "function": {"name": "get_weather",
        "description": "...", "parameters": {...}}}]``.
    :param tool_choice: Ignored — agent controls this. Silently
        dropped.
    :param max_output_tokens: Ignored — agent controls this.
        Silently dropped.
    :param frequency_penalty: Ignored — agent controls this.
        Silently dropped.
    :param presence_penalty: Ignored — agent controls this.
        Silently dropped.
    :param parallel_tool_calls: Ignored — agent controls this.
        Silently dropped.
    :param max_tool_calls: Ignored — agent controls this.
        Silently dropped.
    :param top_logprobs: Ignored — agent controls this. Silently
        dropped.
    """

    # Optional when previous_response_id is set; server resolves the agent
    # from the prior task. Required for fresh conversations (no prior task).
    model: str | None = None
    # Heterogeneous content blocks (input_text, input_image, input_file)
    # or a plain string shorthand; shape varies by block type.
    input: str | list[dict[str, Any]]
    stream: bool = False
    background: bool = False
    store: bool = True
    instructions: str | None = None
    previous_response_id: str | None = None
    # Correlation id for a mid-turn injection (RUNNER_MESSAGE_INGEST.md
    # Part B). Stamped by the runner when it forwards a buffered message
    # as a live injection; echoed back by the executor adapter in an
    # ``injection.consumed`` marker once the executor actually consumes
    # the message, so the runner can drop the buffered copy and not
    # re-deliver it in a continuation turn. ``None`` for fresh turns.
    injection_id: str | None = None
    conversation: ConversationRef | None = None
    # Reasoning config, e.g. {"effort": "low"|"medium"|"high"}
    reasoning: dict[str, str] | None = None
    # Per-request LLM model override (distinct from ``model``, which
    # carries the agent name). See class docstring for semantics.
    model_override: str | None = None
    # Compaction strategy objects, e.g. [{"type": "compaction", ...}]
    context_management: list[dict[str, Any]] | None = None
    # Ignored fields — agent controls these; silently dropped.
    # Typed loosely because we only need to accept and discard them.
    temperature: float | None = None
    top_p: float | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | str | None = None
    max_output_tokens: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    parallel_tool_calls: bool | None = None
    max_tool_calls: int | None = None
    top_logprobs: int | None = None

    @model_validator(mode="after")
    def _require_model_for_new_conversations(self) -> CreateResponseRequest:
        """
        Enforce that ``model`` is provided when starting a fresh conversation.

        When ``previous_response_id`` is not set the server has no prior task
        from which to resolve the agent, so ``model`` is required. Omitting it
        produces a 422 at the API boundary rather than a cryptic runtime error
        deep in the route handler.

        :returns: ``self`` unchanged when the invariant holds.
        :raises ValueError: When ``model`` is ``None`` and
            ``previous_response_id`` is not set.
        """
        if self.model is None and not self.previous_response_id:
            raise ValueError("model is required when previous_response_id is not set")
        return self


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
    :param instructions: Per-request system instructions
        override, or ``None``.
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


class ToolResult(BaseModel):
    """
    A single tool result submitted by the client via PATCH.

    :param call_id: The tool call ID that this result
        corresponds to, e.g. ``"call_abc123"``.
    :param output: The tool's string output,
        e.g. ``'["paper1.pdf", "paper2.pdf"]'``.
    """

    call_id: str
    output: str


class ElicitationResult(BaseModel):
    """
    Consumer reply to an outstanding elicitation.

    Field names + semantics mirror MCP's ``ElicitResult`` verbatim.
    Omnigent clients deliver this shape inside the session-scoped
    ``approval`` event body, alongside the ``elicitation_id``
    correlation key.

    :param action: User action per MCP semantics. ``"accept"`` =
        approved (form submitted / confirmation given).
        ``"decline"`` = explicit refusal. ``"cancel"`` = dismissed
        without an explicit choice (also the verdict the server
        synthesizes on elicitation timeout).
    :param content: Form data when ``action == "accept"`` and the
        ``requestedSchema`` had fields. ``None`` (or omitted) for
        binary approve/reject elicitations and for ``decline`` /
        ``cancel`` actions. Values are restricted to JSON scalars
        and string lists per the MCP spec.
    """

    action: Literal["accept", "decline", "cancel"]
    # ``str | int | float | bool | list[str] | None`` mirrors MCP's
    # ElicitResult.content value type — keep them aligned so an MCP
    # client can bridge to our endpoint without translation.
    content: dict[str, str | int | float | bool | list[str] | None] | None = None


# ── Sessions (/v1/sessions) ────────────────────────────────────


class SessionEventInput(BaseModel):
    """
    A single client-submitted event/input item for a session.

    Used both as an element of ``initial_items`` on session
    creation and as the body of ``POST /v1/sessions/{id}/events``.
    Carries a discriminator (``type``) and a free-form ``data``
    payload whose shape is interpreted by the route layer based
    on ``type`` (e.g. user message, function-call output,
    approval, interrupt).

    :param model_override: Optional per-event LLM model override
        used when this event starts a fresh agent turn. Distinct
        from the session's bound agent; it substitutes for the
        agent spec's ``llm.model`` for that turn.
    :param type: Discriminator for the event/input kind, e.g.
        ``"message"``, ``"function_call_output"``, ``"interrupt"``.
    :param data: Type-specific payload. Shape varies by ``type``;
        for ``"message"`` this looks like
        ``{"role": "user", "content": [{"type": "input_text",
        "text": "Hello"}]}``. For ``"interrupt"`` this is
        typically ``{}``.
    :param tools: Optional OpenAI function-tool dicts registered
        when this event creates a new task. Mirrors
        :attr:`CreateResponseRequest.tools`, e.g. ``[{"type":
        "function", "function": {"name": "get_weather",
        "description": "...", "parameters": {...}}}]``. Ignored
        when the event steers into an active task: that task's
        tools are fixed at start time.
    """

    type: str
    # Heterogeneous payload; route layer validates the shape per ``type``.
    # Defaults to {} for payload-less control events (interrupt,
    # stop_session); item-typed events still fail loud per-type.
    data: dict[str, Any] = Field(default_factory=dict)
    model_override: str | None = None
    tools: list[dict[str, Any]] | None = None


class SessionGitOptions(BaseModel):
    """
    Git worktree options for ``POST /v1/sessions``.

    When present, the server creates a git worktree on the host for a
    new branch and starts the runner in that worktree instead of the
    picked directory. Requires ``host_id`` to be set (and therefore
    ``workspace``, which is interpreted as the source repository
    directory). See designs/SESSION_GIT_WORKTREE.md.

    :param branch_name: Name of the new branch to create and check
        out in the worktree, e.g. ``"feature/login"``. Validated
        against git ref-format rules; invalid names fail with
        ``invalid_input``.
    :param base_branch: Optional base ref to branch from, e.g.
        ``"main"`` or ``"origin/main"``. ``None`` branches from the
        source repository's current ``HEAD``.
    """

    branch_name: str
    base_branch: str | None = None


class SessionCreateRequest(BaseModel):
    """
    JSON request body for ``POST /v1/sessions``.

    Creates a new session bound to an existing agent (looked up by
    durable agent ID, NOT name) and optionally seeds its input queue.

    The Alpha runner-state bundled create flow adds a multipart shape
    to the same endpoint; this JSON body remains the existing
    session-create contract for clients that already uploaded an agent.

    :param agent_id: Durable identifier of the agent to bind,
        e.g. ``"ag_abc123"``. Must match a registered agent.
    :param initial_items: Initial queued events/inputs, typically a
        single user ``"message"``.
    :param title: Optional human-readable title for the session,
        e.g. ``"debugging auth flow"``.
    :param labels: Initial guardrails labels to set on the session.
    :param parent_session_id: Parent session for sub-agent spawns.
        When set, the server inherits the parent's ``runner_id``
        affinity and sets ``parent_conversation_id`` on the child
        conversation. ``None`` for top-level sessions.
    :param sub_agent_name: For sub-agent sessions, the sub-agent
        type name within the parent's spec tree, e.g.
        ``"summarizer"``. The runner uses this to load the correct
        sub-spec instead of the parent's. ``None`` for top-level.
    :param host_type: How the session's host is obtained.
        ``"external"`` (the default, and the pre-existing behavior):
        the session runs on a host the caller manages — either a
        host they registered via ``omnigent host`` (pass
        ``host_id``) or a caller-managed runner (no ``host_id``).
        ``"managed"``: the SERVER provisions a sandbox host from its
        ``sandbox:`` config and binds the session to it —
        ``host_id`` and ``workspace`` must NOT be set (the server
        chooses both). Provisioning happens in the BACKGROUND: the
        create returns immediately with ``host_id`` / ``workspace``
        still null, and they appear on the session snapshot once
        the sandbox host registers. A message posted before then
        waits for the launch to settle instead of failing with
        "no runner bound".
    :param host_id: Optional host to launch the runner on, e.g.
        ``"host_a1b2c3d4..."``. When set, the server triggers the
        host launch flow (generate binding token, write runner_id,
        send launch frame). ``None`` for CLI-initiated sessions.
        Must be ``None`` when ``host_type`` is ``"managed"``.
    :param workspace: Where the session works. For external hosts:
        an absolute path on the host where the runner should start,
        e.g. ``"/Users/corey/universe/src/foo"``. Required when
        ``host_id`` is set; the server validates that the path
        exists, falls within the agent's ``os_env.cwd`` boundary,
        and contains any subdirectory the agent expects (per
        designs/SESSION_WORKSPACE_SELECTION.md). Tilde paths
        (``~/foo``) and relative paths are rejected — the server
        does not expand ``~``. Optional for CLI-initiated sessions
        that record their starting cwd for display. For
        ``host_type: "managed"``: optionally a git repository URL
        with a ``#<branch>`` fragment, e.g.
        ``"https://github.com/org/repo#main"`` or
        ``"git@github.com:org/repo.git"`` — the server clones it
        inside the sandbox and the cloned directory becomes the
        stored session workspace (paths are rejected; ``None``
        gives an empty server-created workspace).
    :param git: Optional git worktree options. When set, the server
        creates a worktree for a new branch on the host and starts
        the runner in it; ``workspace`` is then interpreted as the
        source repository directory. Requires ``host_id``. ``None``
        starts the runner directly in ``workspace``. See
        designs/SESSION_GIT_WORKTREE.md.
    :param terminal_launch_args: Optional pass-through CLI args for a
        native terminal wrapper (claude / codex), e.g.
        ``["--permission-mode", "bypassPermissions"]`` (the web UI's
        permission-mode selector). Set at create-time so the runner has
        them on the session row before it auto-launches the terminal.
        Bounds (count / length) are validated server-side. ``None`` for
        non-native sessions. Mirrors the multipart create path
        (:class:`SessionCreateMetadata`). See
        designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    :param model_override: Optional per-session LLM model override to
        persist at create time, e.g. ``"databricks-claude-sonnet-4-6"``.
        Set by ``sys_session_send``'s per-dispatch ``model`` arg so the
        value is on the session row before the runner launches the
        harness (native CLIs read it as ``--model`` at terminal launch;
        SDK harnesses via the spawn env). Validated server-side against
        a conservative model-id charset. ``None`` = harness default.
    :param cost_control_mode_override: Optional per-session
        cost-control switch to persist at create time: ``"on"``
        activates the spec's configured cost-control mode, ``"off"``
        disables cost control for this session. ``None`` (the
        default) defers to the spec default. Set by the web UI's
        new-session "Cost Optimized" option; read by the cost-control
        advisor pipeline at turn start.
    """

    agent_id: str
    initial_items: list[SessionEventInput] = Field(default_factory=list)
    title: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    parent_session_id: str | None = None
    sub_agent_name: str | None = None
    host_type: Literal["external", "managed"] = "external"
    host_id: str | None = None
    workspace: str | None = None
    git: SessionGitOptions | None = None
    terminal_launch_args: list[str] | None = None
    model_override: str | None = None
    cost_control_mode_override: str | None = None

    @model_validator(mode="after")
    def _check_git_requires_host(self) -> SessionCreateRequest:
        """
        Reject ``git`` without ``host_id`` at validation time.

        Worktree creation runs on a host (the server has no
        filesystem), so ``git`` is meaningless without ``host_id``.
        Failing here returns 422 instead of letting the request reach
        the worktree path and fail late.

        :returns: The validated instance.
        :raises ValueError: If ``git`` is set but ``host_id`` is not.
        """
        if self.git is not None and self.host_id is None:
            raise ValueError("git worktree creation requires host_id")
        return self

    @model_validator(mode="after")
    def _check_managed_host_fields(self) -> SessionCreateRequest:
        """
        Enforce the per-``host_type`` workspace and host-id contract.

        A managed session's host is chosen by the server (sandbox
        provisioning), so a caller-supplied ``host_id`` is a
        contradiction. Its ``workspace``, when given, must be a git
        repository URL (optionally ``#<branch>``) the server clones
        into the sandbox — a path points at nothing in a sandbox that
        doesn't exist yet. Conversely, a repository-URL workspace on
        an external host is rejected: there, ``workspace`` is an
        absolute path on the host. Failing at validation returns a
        422 with the field named instead of silently ignoring the
        caller's intent.

        :returns: The validated instance.
        :raises ValueError: On ``"managed"`` + ``host_id``, a managed
            workspace that isn't a valid repository URL, or an
            external repository-URL workspace.
        """
        # Lazy import: schemas is imported by nearly every module, so
        # pulling the (FastAPI/click-importing) managed-hosts module in
        # at module scope would risk import cycles.
        from omnigent.server.managed_hosts import is_repo_workspace, parse_repo_workspace

        if self.host_type == "managed":
            if self.host_id is not None:
                raise ValueError(
                    "host_type 'managed' lets the server provision the host; "
                    "host_id must not be set"
                )
            if self.workspace is not None:
                try:
                    parse_repo_workspace(self.workspace)
                except ValueError as exc:
                    raise ValueError(
                        "host_type 'managed' takes a git repository URL "
                        f"(optionally '#<branch>') as workspace: {exc}"
                    ) from exc
        elif self.workspace is not None and is_repo_workspace(self.workspace):
            raise ValueError(
                "a repository-URL workspace requires host_type 'managed' — "
                "external hosts take an absolute path on the host"
            )
        return self


class SessionCreateMetadata(BaseModel):
    """
    Metadata JSON part for multipart ``POST /v1/sessions``.

    The uploaded agent tarball supplies the agent spec. This JSON
    part carries only session-level metadata so request metadata
    cannot disagree with the agent bundle.

    :param title: Optional human-readable title for the session,
        e.g. ``"debugging auth flow"``.
    :param labels: Initial guardrails labels to set on the
        session. Empty dict (the default) starts with no labels.
    :param reasoning_effort: Optional per-session reasoning-effort
        hint. Accepted metadata values are ``"none"``,
        ``"minimal"``, ``"low"``, ``"medium"``, ``"high"``,
        ``"xhigh"``, and ``"max"``. Provider-specific support is
        validated when a turn executes. ``None`` means use the agent
        default.
    :param host_id: Optional host to launch the runner on, e.g.
        ``"host_a1b2c3d4..."``. When set, the server generates a
        binding token, writes the expected runner_id to the session
        row, and sends a ``host.launch_runner`` frame to the host.
        ``None`` for CLI-initiated sessions where the caller
        manages runner spawning.
    :param workspace: Absolute path on the host where the runner
        should start, e.g. ``"/Users/corey/universe/src/foo"``.
        Required when ``host_id`` is set; validated against the
        uploaded agent's ``os_env.cwd`` boundary at session create
        (per designs/SESSION_WORKSPACE_SELECTION.md). Optional
        otherwise.
    :param terminal_launch_args: Optional pass-through CLI args for a
        native terminal wrapper (claude / codex), e.g.
        ``["--dangerously-skip-permissions"]``. Set at create-time so
        the runner has them before it boots. Bounds (count / length)
        are validated server-side. ``None`` for non-native sessions.
        See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    :param parent_session_id: Optional parent session id, e.g.
        ``"conv_abc123"``. When set, the new session is created as a
        sub-agent child of that session (``kind="sub_agent"``) and
        inherits the parent's runner binding for co-location. The
        caller must have READ access to the parent. ``None``
        creates a top-level session.
    """

    title: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: str | None = None
    host_id: str | None = None
    workspace: str | None = None
    terminal_launch_args: list[str] | None = None
    parent_session_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class CreatedSessionResponse(BaseModel):
    """
    Response body for multipart ``POST /v1/sessions``.

    :param session_id: Identifier of the newly created session,
        e.g. ``"conv_abc123"``.
    :param agent_id: Identifier of the session-scoped agent created
        from the uploaded bundle, e.g. ``"ag_abc123"``.
    :param agent_name: Agent name loaded from the uploaded bundle's
        spec, e.g. ``"code-assistant"``.
    """

    session_id: str
    agent_id: str
    agent_name: str


class SessionLabelsResponse(BaseModel):
    """
    Lightweight response body for ``GET /v1/sessions/{id}/labels``.

    :param id: Session identifier, e.g. ``"conv_abc123"``.
    :param labels: Session-scoped guardrails labels. Empty dict when
        no labels have been written.
    """

    id: str
    labels: dict[str, str] = Field(default_factory=dict)


# Stages of a managed-sandbox launch, in pipeline order: the sandbox
# is provisioned, the repository workspace is cloned into it (skipped
# when the session has no repo workspace), the in-sandbox host starts
# and registers, and the agent runner is launched on it. ``ready`` and
# ``failed`` are terminal.
SandboxLaunchStage = Literal[
    "provisioning",
    "cloning",
    "starting",
    "connecting",
    "ready",
    "failed",
]


class SandboxStatus(BaseModel):
    """
    Managed-sandbox launch progress for a ``host_type="managed"`` session.

    Carried on the session snapshot only while the session's
    background sandbox launch is in flight or has failed; ``None``
    for sessions without a managed launch and once the launch
    succeeds (the session then looks like any host-bound session).

    :param stage: Current launch stage, e.g. ``"provisioning"`` —
        one of :data:`SandboxLaunchStage`, in pipeline order:
        ``provisioning`` (creating the sandbox) → ``cloning``
        (cloning the repository workspace; skipped when the session
        has none) → ``starting`` (starting the in-sandbox host) →
        ``connecting`` (launching the agent runner) → ``ready`` /
        ``failed``.
    :param error: Failure detail when ``stage == "failed"``, e.g.
        ``"managed sandbox launch failed: spend limit reached"``.
        ``None`` otherwise.
    """

    stage: SandboxLaunchStage
    error: str | None = None


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


class SessionResponse(BaseModel):
    """
    API representation of a session.

    Returned by ``POST /v1/sessions``, ``GET /v1/sessions/{id}``,
    and ``PATCH /v1/sessions/{id}``.

    :param id: Unique session identifier (also the underlying
        conversation ID), e.g. ``"conv_abc123"``.
    :param agent_id: Durable identifier of the bound agent,
        e.g. ``"ag_abc123"``. Stable across renames of the
        agent.
    :param agent_name: Human-readable name of the bound agent,
        e.g. ``"research-agent"``. Loaded from the agent row at
        snapshot-build time. ``None`` when the agent row cannot
        be found (deleted or orphaned session).
    :param status: Session lifecycle status. One of
        ``"idle"`` (no loop running), ``"running"`` (loop
        executing), or ``"failed"`` (terminal failure).
    :param created_at: Unix epoch seconds of creation.
    :param title: Optional human-readable title, e.g.
        ``"debugging auth flow"``. ``None`` when unset.
    :param labels: Session-scoped guardrails labels. Empty dict
        when no labels have been written.
    :param runner_id: Runner currently bound to this session, e.g.
        ``"runner_abc123"``. ``None`` until a client binds one via
        ``PATCH /v1/sessions/{id}``.
    :param host_id: Host that launched (or should launch) the
        runner for this session, e.g. ``"host_a1b2c3d4..."``.
        ``None`` for CLI-initiated sessions.
    :param runner_online: Strict runner liveness — ``True`` iff a
        runner tunnel is currently registered for this session.
        This is the sole reachability signal: ``True`` means the
        client can chat normally. It does **not** fold in
        host-relaunch optimism (a dead runner on a live host reads
        ``False`` here, not ``True``) — the open-session view pairs
        it with ``host_online`` to decide what to show. ``None``
        when the server has no runner liveness lookup wired.
    :param host_online: Whether the session's host tunnel is live
        (status online and fresh within the host liveness TTL).
        ``None`` when the session has no ``host_id`` (CLI/local).
        Used only to choose what the open view shows when
        ``runner_online`` is ``False`` — host alive ⇒ "send a
        message to wake the runner"; host dead ⇒ "reconnect /
        fork". Never participates in the reachability decision.
    :param reasoning_effort: Per-session reasoning-effort hint.
        Accepted metadata values are ``"none"``, ``"minimal"``,
        ``"low"``, ``"medium"``, ``"high"``, ``"xhigh"``, and
        ``"max"``. Provider-specific support is validated when a
        turn executes. ``None`` means use the agent default.
    :param items: Committed conversation items in chronological
        order. Empty for a freshly created session.
    :param sub_agent_name: For sub-agent sessions, the sub-agent
        type name within the parent's spec tree, e.g.
        ``"summarizer"``. ``None`` for top-level sessions.
    :param parent_session_id: For sub-agent sessions, the parent
        conversation's id, e.g. ``"conv_parent987"``. ``None`` for
        top-level sessions. Lets clients identify a session as a
        child and link back to its parent without an extra
        round-trip — the same conversation row exposes this via
        ``parent_conversation_id`` internally.
    :param root_conversation_id: The id of this session's spawn-tree
        root, e.g. ``"conv_root1"``. Equals ``id`` for top-level
        sessions; for sub-agents it points at the top-level ancestor.
        Lets orchestration tools (e.g. ``sys_session_close``) confirm
        a target shares the caller's spawn tree over the REST path.
        ``None`` only when the underlying row predates the
        ``root_conversation_id`` column (not expected post-migration).
    :param permission_level: The requesting user's numeric
        permission level on this session: ``1`` = read, ``2`` =
        edit, ``3`` = manage. ``None`` when permissions are
        disabled (single-user mode without a permission store).
    :param llm_model: The LLM model identifier from the bound
        agent's spec, e.g. ``"anthropic/claude-sonnet-4-6"``.
        ``None`` when the agent has no explicit ``llm:`` block or
        the agent cannot be looked up.
    :param harness: The bound agent's canonical harness, e.g.
        ``"claude-sdk"`` or ``"openai-agents"``. Lets the client
        render the active credential for the correct provider
        family instead of inferring it from the model string (which
        is wrong when the agent declares no model). ``None`` when
        the agent cannot be looked up.
    :param model_override: Per-session LLM model override,
        e.g. ``"claude-opus-4-7"``. ``None`` means no override is
        active (the agent's ``llm_model`` applies). Set via
        ``PATCH /v1/sessions/{id}`` or the REPL's ``/model``
        command; both write the same column so the ap-web UI and
        the TUI stay in sync.
    :param cost_control_mode_override: Per-session cost-control
        switch: ``"on"`` activates the spec's configured cost-control
        mode, ``"off"`` disables cost control for this session.
        ``None`` means no override is active (the spec default
        applies). Set at create time or via
        ``PATCH /v1/sessions/{id}`` (the web "Cost Optimized"
        toggle); read by the cost-control advisor pipeline.
    :param context_window: The model's context window size in tokens
        as looked up server-side from litellm's registry (or from the
        ``AP_CONTEXT_WINDOW_OVERRIDE`` env var), e.g. ``200_000``.
        ``None`` when the model is not in litellm's registry and no
        override is set.
    :param last_total_tokens: Total token count (input + output) from
        the most recently completed task's ``usage``, e.g. ``45231``.
        ``None`` when no task has completed yet. Lets clients seed
        their context-ring on conversation resume without waiting for
        the next ``response.completed`` SSE event.
    :param total_cost_usd: Cumulative LLM spend for this session in
        USD, e.g. ``0.42``. ``None`` when the session is **unpriced**
        — no turn has been priced yet (the model is absent from the
        pricing catalog, or no usage has been recorded) — so clients
        render "—" rather than a misleading ``$0.00``. Server-computed
        (cache-aware for relay/codex, exact billing for claude-native),
        the same total the cost-budget policy gates on. Lets clients
        seed their cost indicator on resume without waiting for the
        next ``session.usage`` SSE event.
    :param usage_by_model: Per-model breakdown of the same subtree usage,
        keyed by the raw harness model id, e.g.
        ``{"claude-sonnet-4-6": ModelUsage(input_tokens=12000, ...)}``.
        ``None`` when no per-model usage has been recorded (older sessions
        recorded before this field existed, or before the first turn). Lets
        the UI show which models a session spent its tokens / budget on.
    :param last_task_error: Error details from the most recently
        failed task. Only present when ``status == "failed"`` and
        the task stored an error. Lets clients display the failure
        reason on historical load without relying on the transient
        ``response.error`` SSE event (which may have been emitted
        before the web client subscribed). Format mirrors the
        ``RetryErrorDetail`` SSE shape:
        ``{"code": "executor_error", "message": "..."}``.
        ``None`` in all other cases.
    :param external_session_id: Runtime-native session id this
        conversation wraps, e.g. a Claude Code session uuid for
        ``omnigent claude`` sessions. ``None`` for regular
        AP-only conversations. Populated by the wrapper bridge.
    :param terminal_launch_args: Pass-through CLI args the native
        terminal wrapper (claude / codex) was launched with, e.g.
        ``["--dangerously-skip-permissions"]``. ``None`` for
        non-native sessions or a native session launched with none.
        Lets the launcher reproduce the command on resume.
    :param pending_elicitations: Outstanding approval prompts on
        this session at the moment the snapshot was built — the
        original ``response.elicitation_request`` event dicts.
        Lets the UI render the ApprovalCard on cold load, since
        the live SSE stream has no replay and a prompt emitted
        before the user opened the chat would otherwise vanish.
        Empty list when no prompts are outstanding. Sourced from
        the Omnigent server's in-memory
        :mod:`omnigent.runtime.pending_elicitations` index.
    :param pending_inputs: Un-consumed web-composer user messages on
        native-terminal (claude-native / codex-native) sessions at
        snapshot time, each ``{"pending_id", "content"}``. Native
        sessions don't persist a web message at POST time (the
        transcript forwarder is the single writer), so a client that
        posted then navigated away / rebound would lose its optimistic
        bubble; replaying these re-hydrates it. Empty list otherwise.
        Sourced from the in-memory
        :mod:`omnigent.runtime.pending_inputs` index.
    :param workspace: Absolute path on disk where the runner cd's,
        e.g. ``"/Users/corey/universe/src/foo"``. Set when the
        session was bound to a host workspace at create-time, or
        when the CLI captured ``os.getcwd()`` at session-create.
        Always ``None`` when not yet validated against a host. When a
        git worktree was created for the session, this is the
        worktree directory path.
    :param git_branch: Git branch checked out in the session's
        worktree, e.g. ``"feature/login"``. Set only when the
        session was created with a server-created git worktree;
        ``None`` otherwise. The Web UI uses a non-``None`` value to
        offer the "delete local branch" cleanup checkbox on session
        delete. See designs/SESSION_GIT_WORKTREE.md.
    :param archived: Whether the session is archived. Archived
        sessions are hidden from the default sidebar listing and
        surface only behind the "Show archived" toggle. ``False``
        for normal sessions. Toggled via ``PATCH /v1/sessions/{id}``.
    :param todos: Current Claude Code todo list items for
        ``omnigent claude`` sessions, as raw dicts from Claude's
        todo JSON file. Each dict has ``content``, ``status``,
        and ``activeForm`` keys. Empty list for non-claude-native
        sessions or when no todos have been reported yet. Sourced
        from the Omnigent server's in-memory ``_session_todos_cache``.
    :param skills: Skills the bound agent has access to — the
        merged result of the agent spec's bundled ``skills``
        and the host-scope skills discovered along the agent
        workdir / ``~/.claude/skills/`` (subject to the spec's
        ``skills_filter``). Mirrors what the TUI passes to the
        runner at startup. Empty list when the agent spec
        cannot be loaded, or when bundled + host discovery
        yields nothing.
    :param terminal_pending: ``True`` while the runner is auto-creating
        a terminal-first session's terminal (claude-native /
        codex-native), so the Web UI shows a spinner on the Terminal
        pill instead of a silent greyed-out button. Cleared to
        ``False`` once the terminal lands or auto-create fails; from
        then on the client relies purely on whether a terminal resource
        exists. Sourced from the Omnigent server's in-memory
        ``_session_terminal_pending_cache`` at snapshot build time, so a
        client connecting mid-spin-up still sees the spinner.
    :param sandbox_status: Managed-sandbox launch progress while the
        session's background sandbox launch is in flight or has
        failed — see :class:`SandboxStatus`. ``None`` for sessions
        without a managed launch and once the launch succeeds.
        Sourced from the Omnigent server's in-memory
        ``_session_sandbox_status_cache`` at snapshot build time, so
        a client opening the session mid-launch sees the current
        stage.
    """

    id: str
    agent_id: str
    agent_name: str | None = None
    status: Literal["idle", "running", "failed"]
    created_at: int
    title: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    runner_id: str | None = None
    host_id: str | None = None
    runner_online: bool | None = None
    host_online: bool | None = None
    reasoning_effort: str | None = None
    items: list[ConversationItem] = Field(default_factory=list)
    permission_level: int | None = None
    sub_agent_name: str | None = None
    parent_session_id: str | None = None
    root_conversation_id: str | None = None
    llm_model: str | None = None
    harness: str | None = None
    model_override: str | None = None
    cost_control_mode_override: str | None = None
    context_window: int | None = None
    last_total_tokens: int | None = None
    total_cost_usd: float | None = None
    usage_by_model: dict[str, ModelUsage] | None = None
    last_task_error: dict[str, str] | None = None
    external_session_id: str | None = None
    terminal_launch_args: list[str] | None = None
    pending_elicitations: list[dict[str, Any]] = Field(default_factory=list)
    # Un-consumed web-composer user messages on native-terminal
    # sessions at snapshot time, each ``{"pending_id", "content"}``.
    # Replayed so a client that posted a message then navigated away /
    # rebound re-hydrates the optimistic bubble (the live SSE stream
    # has no replay). Empty for non-native sessions, which persist the
    # message at POST time and thus already carry it in ``items``.
    # Source: :mod:`omnigent.runtime.pending_inputs`.
    pending_inputs: list[dict[str, Any]] = Field(default_factory=list)
    workspace: str | None = None
    git_branch: str | None = None
    archived: bool = False
    todos: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[SkillSummary] = Field(default_factory=list)
    terminal_pending: bool = False
    sandbox_status: SandboxStatus | None = None


class UpdateSessionRequest(BaseModel):
    """
    Request body for ``PATCH /v1/sessions/{id}``.

    The Alpha runner-state pivot makes this endpoint the mutable
    session affinity primitive when ``runner_id`` is provided. The
    server validates that the runner is online, then replaces
    ``conversations.runner_id``. Existing session metadata updates
    remain supported for clients that update title, labels, or
    reasoning effort through the sessions API.

    :param runner_id: Identifier of a registered runner, e.g.
        ``"runner_abc123"``. ``None`` leaves runner binding
        unchanged.
    :param title: New title, e.g. ``"debugging auth flow"``.
        ``None`` leaves unchanged.
    :param labels: Guardrails labels to upsert. Merges with existing
        labels; keys not present are left untouched.
    :param reasoning_effort: Per-session reasoning-effort hint.
        Accepted metadata values are ``"none"``, ``"minimal"``,
        ``"low"``, ``"medium"``, ``"high"``, ``"xhigh"``, and
        ``"max"``. Provider-specific support is validated when a
        turn executes. Clear aliases such as ``"default"`` remove
        the session override. ``None`` leaves unchanged.
    :param model_override: Per-session LLM model override, e.g.
        ``"claude-opus-4-7"``. The value is forwarded as-is to the
        executor at turn start; the server does not enumerate valid
        models. Clear aliases such as ``"default"``, ``"off"``, or
        ``"reset"`` remove the override (matching the REPL's
        ``/model`` semantics). ``None`` leaves unchanged.
    :param cost_control_mode_override: Per-session cost-control
        switch: ``"on"`` activates the spec's configured cost-control
        mode, ``"off"`` disables cost control for this session.
        Explicit JSON ``null`` clears the override back to the spec
        default; omitting the field leaves it unchanged (``"off"`` is
        a real value here, so the field's *presence* — not a clear
        alias — is the clear signal, unlike ``model_override``).
    :param external_session_id: Runtime-native session id captured
        by a wrapper bridge (e.g. Claude Code's session uuid for
        ``omnigent claude`` sessions). Idempotent on same-value
        writes; the server rejects attempts to overwrite an
        already-set different value with ``invalid_input`` to
        surface programmer errors. ``None`` leaves unchanged.
    :param terminal_launch_args: Per-session native-terminal
        pass-through args, e.g. ``["--dangerously-skip-permissions"]``.
        A list (including ``[]``) replaces the stored value wholesale
        — resume is last-write-wins, never an append. Bounds (count /
        length) are validated server-side. ``None`` leaves unchanged.
    :param silent: When ``True``, persist metadata changes but skip
        the runner-side side effects — specifically the
        claude-native ``/effort`` and ``/model`` slash-command
        forwards into the tmux pane. Used by automatic bind-time
        handoffs (ap-web's sticky-pref apply on session switch, the
        REPL's pre-create ``/model`` snapshot) where injecting a
        visible slash command into a freshly-spawned pane would
        render as an unexpected "Command model X" item before the
        user has sent anything. Default ``False`` preserves the
        user-driven picker / ``/model`` behaviour where the live
        forward IS the desired feedback.
    :param archived: New archived state. ``True`` archives (hides the
        session from the default sidebar listing), ``False`` unarchives,
        ``None`` leaves unchanged. Owner-only (unlike ``title``, which
        needs only edit access).
    """

    runner_id: str | None = None
    title: str | None = None
    labels: dict[str, str] | None = None
    reasoning_effort: str | None = None
    model_override: str | None = None
    cost_control_mode_override: str | None = None
    external_session_id: str | None = None
    terminal_launch_args: list[str] | None = None
    archived: bool | None = None
    silent: bool = False

    model_config = ConfigDict(extra="forbid")


class SessionForkRequest(BaseModel):
    """
    Request body for ``POST /v1/sessions/{source_id}/fork``.

    Creates a deep copy of an existing session's items into a new
    session. All fields are optional.

    :param title: Title for the forked session. When ``None``, the
        server derives ``"Fork of <source_title>"``.
    :param agent_id: Built-in agent to bind the fork to, switching it
        away from the source's agent/harness (e.g. fork a Claude session
        into a Codex one, or a Claude-SDK session into Claude Code). When
        ``None``, the fork keeps the source's agent. Must be a built-in
        agent (one listed by ``GET /v1/agents``).
    :param up_to_response_id: Truncation point for the copied history,
        e.g. ``"resp_abc123"``. When set, only items up to and including
        the last item of that response are copied — items after it are
        dropped from the fork. When ``None`` (default), the full history
        is copied.
    """

    title: str | None = None
    agent_id: str | None = None
    up_to_response_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class SessionSwitchAgentRequest(BaseModel):
    """
    Request body for ``POST /v1/sessions/{id}/switch-agent``.

    Rebinds an existing session in place to a different agent/harness,
    keeping the same session (transcript, comments, files, workspace).
    Unlike fork, no new session is created.

    :param agent_id: Built-in agent to switch the session to, e.g.
        ``"ag_builtin_codex"``. Must be a built-in agent (one listed by
        ``GET /v1/agents``) and different from the session's current
        agent.
    """

    agent_id: str

    model_config = ConfigDict(extra="forbid")


class SessionListItem(BaseModel):
    """
    Lightweight session summary for ``GET /v1/sessions`` list responses.

    Same shape as :class:`SessionResponse` minus ``items``.

    :param id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param agent_id: Durable identifier of the bound agent.
    :param agent_name: Human-readable name of the bound agent,
        e.g. ``"research-agent"``. ``None`` when the agent row
        cannot be found.
    :param status: Derived session lifecycle status.
    :param created_at: Unix epoch seconds of creation.
    :param updated_at: Unix epoch seconds of last update.
    :param title: Optional human-readable title.
    :param labels: Session-scoped guardrails labels.
    :param runner_id: Runner currently bound to the session.
    :param host_id: Host that launched the runner for this session.
    :param runner_online: Strict runner liveness — ``True`` iff a
        runner tunnel is currently registered for this session.
        Matches ``GET /health``'s ``runner_online`` value. Strict:
        a dead runner on a live host reads ``False`` here (no
        host-relaunch optimism folded in), unlike the legacy
        conflated value. ``None`` when the server has no runner
        liveness lookup wired.
    :param host_online: Whether the session's host tunnel is live
        (status online and fresh within the host liveness TTL).
        ``None`` when the session has no ``host_id`` (CLI/local).
        Distinguishes "runner down but host can relaunch" from
        "host offline" for the open-session view; not used by the
        sidebar.
    :param reasoning_effort: Per-session reasoning-effort hint.
    :param permission_level: The requesting user's numeric
        permission level on this session: ``1`` = read, ``2`` =
        edit, ``3`` = manage. ``None`` when permissions are
        disabled.
    :param owner: The user_id of the session owner, or ``None``
        when permissions are disabled. Included so the sidebar
        can display the owner without a separate API call.
    :param external_session_id: Runtime-native session id this
        conversation wraps, e.g. a Claude Code session uuid for
        ``omnigent claude`` sessions. ``None`` for regular
        AP-only conversations. Lets the sidebar / picker render
        a runtime badge without a follow-up GET.
    :param pending_elicitations_count: Number of approval prompts
        currently waiting on this session. Powers the sidebar's
        "needs attention" badge so a user with several sessions
        running can tell which ones are blocked on them without
        opening each chat. Sourced from the Omnigent server's in-memory
        :mod:`omnigent.runtime.pending_elicitations` index,
        which mirrors every ``response.elicitation_request`` event
        passing through ``session_stream`` and decrements when a
        verdict is dispatched. ``0`` when the session has no
        outstanding elicitations.
    :param workspace: Absolute path on disk where the runner cd's,
        e.g. ``"/Users/corey/universe/src/foo"``. ``None`` for
        sessions that haven't been bound to a host workspace.
    :param git_branch: Git branch checked out in the session's
        worktree, e.g. ``"feature/login"``. Set only when the
        session was created with a server-created git worktree;
        ``None`` otherwise. The Web UI uses a non-``None`` value to
        offer the "delete local branch" cleanup checkbox on session
        delete. See designs/SESSION_GIT_WORKTREE.md.
    :param archived: Whether the session is archived. Archived
        sessions are returned by ``GET /v1/sessions`` only when the
        request passes ``include_archived=true``; the sidebar groups
        them into a dedicated "Archived" section. ``False`` for
        normal sessions.
    :param comments_count: Total number of review comments (any
        status) on this session. Together with
        ``comments_updated_at`` it forms a change fingerprint: an
        add or edit bumps the timestamp, a delete changes the count,
        so the web client can invalidate its cached comment list
        when either field changes in a ``WS /v1/sessions/updates``
        frame. ``0`` when the session has no comments or the server
        has no comment store wired.
    :param comments_updated_at: Unix epoch **microseconds** of the
        most recently mutated comment on this session (max
        ``updated_at`` across its comments). Microsecond precision
        keeps back-to-back mutations within one second
        distinguishable while staying an exact integer in JavaScript;
        clients only compare it for change. ``None`` when the session
        has no comments or the server has no comment store wired.
    """

    id: str
    agent_id: str
    agent_name: str | None = None
    status: Literal["idle", "running", "failed"]
    created_at: int
    updated_at: int
    title: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    runner_id: str | None = None
    host_id: str | None = None
    runner_online: bool | None = None
    host_online: bool | None = None
    reasoning_effort: str | None = None
    permission_level: int | None = None
    owner: str | None = None
    external_session_id: str | None = None
    pending_elicitations_count: int = 0
    workspace: str | None = None
    git_branch: str | None = None
    archived: bool = False
    comments_count: int = 0
    comments_updated_at: int | None = None


# ── Permissions ────────────────────────────────────────────────────


class GrantPermissionRequest(BaseModel):
    """
    Request body for ``PUT /v1/sessions/{id}/permissions``.

    :param user_id: The user to grant access to, e.g.
        ``"alice@example.com"`` or ``"__public__"`` for public
        read access.
    :param level: Numeric permission level: ``1`` = read,
        ``2`` = edit, ``3`` = manage.
    """

    user_id: str
    level: int = Field(ge=1, le=3)


class PermissionObject(BaseModel):
    """
    API representation of a session permission grant.

    :param user_id: The grantee, e.g. ``"alice@example.com"``.
    :param conversation_id: The session, e.g.
        ``"conv_abc123"``.
    :param level: Numeric permission level (1=read, 2=edit,
        3=manage).
    """

    user_id: str
    conversation_id: str
    level: int


# ─────────────────────────────────────────────────────────────────────
# STREAM EVENTS — typed Pydantic union for SSE event boundary
# ─────────────────────────────────────────────────────────────────────
#
# Single source of truth for the omnigent SSE event stream. Every
# event the server emits over its two SSE endpoints is modeled below
# as a Pydantic class with a ``type: Literal[...]`` discriminator, and
# the ``ServerStreamEvent`` annotated union routes raw event dicts to
# the right concrete model. Server, runtime, REPL/TUI, and SDK all
# import these names from this module so wire-name renames and
# payload changes are a one-edit change.
#
# The SSE endpoint is:
#
# * ``GET /v1/sessions/{id}/stream`` — session live-tail (multiplexes
#   the underlying response stream and surfaces queue/interrupt
#   semantics).
#
# Two event families coexist:
#
# * ``session.*`` — session-scoped lifecycle events
#   (:class:`SessionStatusEvent`, :class:`SessionInputConsumedEvent`,
#   :class:`SessionInterruptedEvent`, :class:`SessionCreatedEvent`).
# * ``response.*`` — pass-through Responses-API events emitted by the
#   executor; the session stream multiplexes them unchanged.
#
# Channel split (per ``designs/session_rearchitecture.md`` §3 "Two
# channels"). Each event variant is conceptually either *transient*
# or *persistent*:
#
# * Transient (SSE-only) — text/reasoning deltas, turn lifecycle
#   events, ``session.*`` lifecycle events, retry/heartbeat/error
#   signals, ``approval_required``. Fire-and-forget on the SSE
#   stream — NOT persisted.
# * Persistent (POST + SSE replay) — assistant messages, tool calls,
#   tool results, and compaction summaries. Persist-then-publish is
#   enforced inside ``_persist_and_stream``.
#
# Wire-shape note: the server today emits some events with a flat
# shape (``{"type": ..., <fields>}``) and others with a nested
# ``{"type": ..., "data": {...}}`` envelope. The Pydantic models
# below match the wire shapes verbatim — see each model's docstring
# for the emit site reference.
# ─────────────────────────────────────────────────────────────────────


# ── Module-level constants (rule 34) ──────────────────────────────

# Forward-compatibility note: every event model uses ``ConfigDict(
# extra="ignore")`` so harnesses (or AP) can add new fields to an
# event in a future contract revision without breaking older
# parsers — see ``designs/SERVER_HARNESS_CONTRACT.md`` §Validation
# discipline (loose by default).


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


# ── Session-scoped events (session.*) ──────────────────────────────


class SessionStatusEvent(_SSEEventBase):
    """
    Session lifecycle status transition.

    Emitted by the runtime / session route handler at every
    transition between ``running`` / ``waiting`` / ``idle`` /
    ``failed``. Wire shape is
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
    :param status: New session status. ``"idle"`` (no loop
        running), ``"running"`` (loop executing), ``"waiting"``
        (parent turn parked on the async-work drain), or
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

    Category: **transient** (SSE-only). Status is rederived on
    reconnect from the cached last-relayed turn lifecycle event
    or by re-querying the runner; not persisted by the runtime.
    """

    type: Literal["session.status"]
    conversation_id: str
    status: Literal["idle", "running", "waiting", "failed"]
    response_id: str | None = None
    error: ErrorDetail | None = None


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
    Active-model update from a terminal-backed integration.

    Emitted after an ``external_model_change`` POST from the
    ``omnigent claude`` transcript forwarder when the model is
    switched inside the Claude Code terminal (a ``/model`` command or
    the in-TUI picker). Lets the web model picker reflect a TUI-side
    switch without a reload.

    :param type: Always ``"session.model"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param model: Tier alias the session is now on, e.g. ``"opus"`` —
        Claude Code's version-agnostic alias, matching the picker's
        vocabulary (not a pinned ``"claude-opus-4-8"`` id).

    Category: **transient** (SSE-only). The server also writes
    ``model_override`` on the conversation, so on reconnect clients
    restore the selection from the snapshot's ``model_override`` rather
    than from a replayed event.
    """

    type: Literal["session.model"]
    conversation_id: str
    model: str


class SessionAgentChangedEvent(_SSEEventBase):
    """
    Bound-agent change on a live session.

    Emitted by the switch-agent route after the session's agent binding
    is rewritten in place. Connected clients re-derive their cached
    session state (harness presentation labels, bound agent id/name)
    from a fresh snapshot — the chat UI's native-vs-SDK message
    lifecycle depends on those labels, so a stale cache drops the first
    post-switch message (it reappears only when the transcript
    round-trip lands).

    :param type: Always ``"session.agent_changed"``.
    :param conversation_id: Session identifier, e.g. ``"conv_abc123"``.
    :param agent_id: The session-scoped clone now bound to the session,
        e.g. ``"ag_abc123"``.
    :param agent_name: Display name of the agent the session now runs,
        e.g. ``"claude-native-ui"``. Deliberately the clean target-agent
        name — not the clone row's ``"… (switch ag_…)"`` disambiguation
        name — because clients render it verbatim.

    Category: **transient** (SSE-only). The switch is persisted on the
    conversation row, so on reconnect clients read the new binding from
    the session snapshot rather than from a replayed event.
    """

    type: Literal["session.agent_changed"]
    conversation_id: str
    agent_id: str
    agent_name: str


class SessionTodosEvent(_SSEEventBase):
    """
    Todo-list update from a Claude Code terminal-backed session.

    Emitted after an ``external_session_todos`` POST from the
    ``omnigent claude`` transcript forwarder, which captures todo
    updates via ``PostToolUse``/``TodoWrite`` hook events from Claude
    Code and forwards them to the Omnigent server. Lets ap-web render a
    live todo panel in the right column without polling.

    :param type: Always ``"session.todos"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.
    :param todos: Current todo items read from Claude's todo file.
        Each entry is a raw dict with ``content`` (str),
        ``status`` (``"pending"`` | ``"in_progress"`` |
        ``"completed"``), and ``activeForm`` (str, the gerund form)
        keys, e.g. ``[{"content": "Fix the bug", "status":
        "in_progress", "activeForm": "Fixing the bug"}]``.

    Category: **transient** (SSE-only). On reconnect, clients seed
    the panel from the session snapshot's ``todos`` field, which is
    populated by ``_session_todos_cache`` at snapshot build time.
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

    Together they allow ap-web to show a spinner on the Terminal pill
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


class SessionSkillsEvent(_SSEEventBase):
    """
    Signal that a session's runner-owned skills have resolved.

    Skills are discovered against the bound runner's filesystem and
    fetched off the session-snapshot hot path: the snapshot kicks a
    single background fetch (``_load_runner_skills`` in
    ``omnigent/server/routes/sessions.py``) and serves ``[]`` until
    it lands. This event fires the moment that background fetch
    populates the per-session skills cache, so a connected web client
    can re-read the snapshot and fill its slash-command menu instead
    of waiting for the next bind.

    Carries no payload beyond the conversation id — it is a "skills
    are ready, re-read the snapshot" nudge, mirroring the
    invalidate-then-refetch shape used by
    :class:`SessionChangedFilesInvalidatedEvent`. The snapshot's
    ``skills`` field (now cache-backed) stays the source of truth.

    :param type: Always ``"session.skills"``.
    :param conversation_id: Session identifier,
        e.g. ``"conv_abc123"``.

    Category: **transient** (SSE-only). On reconnect, clients seed
    the menu from the session snapshot's ``skills`` field, which is
    populated by the runner-skills cache at snapshot build time.
    """

    type: Literal["session.skills"]
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


# ── Response pass-through events (response.*) ──────────────────────


class OutputTextDeltaEvent(_SSEEventBase):
    """
    Incremental assistant-text token emitted during streaming.

    Wire shape matches the existing raw-dict emit at
    ``omnigent/runtime/workflow.py:1352-1356``.

    :param type: Always ``"response.output_text.delta"``.
    :param delta: The text fragment for this chunk, e.g.
        ``"Hello"``.
    :param message_id: For terminal-observed streaming (claude-native),
        the vendor's stable per-message id, e.g.
        ``"2ca51d97-2f0f-493a-aed7-85a5b56c5747"``. Lets the web UI scope
        an in-flight buffer to one assistant message and reconcile it
        against the final item. ``None`` for ordinary in-process task
        streaming, where deltas already group by the active response.
    :param index: 0-based chunk order within the message, e.g. ``3``.
        ``None`` when not terminal-observed streaming.
    :param final: ``True`` on the last chunk of a terminal-observed
        message; ``None`` otherwise. Signals the web UI that no further
        chunks for ``message_id`` will arrive.
    """

    type: Literal["response.output_text.delta"]
    delta: str
    message_id: str | None = None
    index: int | None = None
    final: bool | None = None


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


class InjectionConsumedEvent(_SSEEventBase):
    """
    Runner-internal marker: a mid-turn injection was consumed.

    Emitted by the executor adapter (``_watch_injections``) once the
    inner executor accepts a live mid-turn injection into the running
    turn. It rides the harness→runner turn stream and is intercepted by
    the runner's proxy_stream relay: the runner drops the buffered copy
    of the matching message so it is NOT re-delivered as a continuation
    turn (RUNNER_MESSAGE_INGEST.md Part B). This event is **never**
    published to the client session stream or relayed upstream — it is
    purely a runner-internal exactly-once handshake.

    :param type: Always ``"injection.consumed"``.
    :param injection_id: Correlation id the runner stamped on the
        forwarded injection, e.g. ``"inj_ab12cd34ef56"``. Matches the
        ``injection_id`` on the buffered message the runner drops.
    """

    type: Literal["injection.consumed"]
    injection_id: str


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
    """

    type: Literal["response.elicitation_resolved"]
    elicitation_id: str


class CreatedEvent(_SSEEventBase):
    """
    Initial event emitted at the start of every streaming response.

    Carries the freshly-allocated
    :class:`omnigent.server.schemas.ResponseObject` (status will
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
    :class:`omnigent.server.schemas.ResponseObject`.

    :param type: Always ``"response.completed"``.
    :param response: The final response object with
        ``status="completed"``.
    """

    type: Literal["response.completed"]
    response: ResponseObject


class FailedEvent(_SSEEventBase):
    """
    Terminal event for a turn that ended with an error.

    Carries the final
    :class:`omnigent.server.schemas.ResponseObject` whose
    ``error`` field describes the failure.

    :param type: Always ``"response.failed"``.
    :param response: The final response object with
        ``status="failed"`` and ``error`` populated.
    """

    type: Literal["response.failed"]
    response: ResponseObject


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
    :param source: Origin of the error — ``"llm"`` for LLM-call
        failures, ``"execution"`` for timeouts, ``"tool"`` for
        tool failures (currently emitted by retry exhaustion paths).
    :param tool_name: Tool identifier when ``source == "tool"``;
        ``None`` for the other sources.
    :param error: Classified error description.
    """

    type: Literal["response.error"]
    source: Literal["llm", "execution", "tool"]
    tool_name: str | None = None
    error: RetryErrorDetail


class CompactionInProgressEvent(_SSEEventBase):
    """
    Conversation history is being compacted.

    Emitted by ``omnigent/runtime/compaction.py`` while a
    compaction step runs so clients can render a "summarizing
    history…" indicator. Wire shape matches ``compaction.py:765``.

    :param type: Always ``"response.compaction.in_progress"``.
    """

    type: Literal["response.compaction.in_progress"]


class CompactionCompletedEvent(_SSEEventBase):
    """
    Conversation history compaction has finished.

    Emitted by ``omnigent/server/routes/sessions.py`` after
    ``compact_conversation_now()`` returns successfully. Clients
    that rendered a "Compacting…" spinner on
    :class:`CompactionInProgressEvent` should upgrade it to the
    permanent "Conversation compacted" marker on this event.

    :param type: Always ``"response.compaction.completed"``.
    :param total_tokens: Tiktoken estimate of the post-compaction
        message context size, e.g. ``8421``. Used by clients to
        update the context-ring immediately without waiting for the
        next ``response.completed`` usage report. ``None`` when
        token counting is unavailable.
    """

    type: Literal["response.compaction.completed"]
    total_tokens: int | None = None


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


# ── Session resource lifecycle events (Phase 1d) ─────────────────────


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


# ── Discriminated union ─────────────────────────────────────────────


# ServerStreamEvent: every event a stream consumer (AP-as-harness-
# client OR an external client of AP) may receive on either of
# AP's two SSE endpoints. Pydantic dispatches on the ``type``
# field via ``Field(discriminator="type")``; each variant's
# ``Literal[...]`` pins the correct branch.
#
# Usage:
#     from pydantic import TypeAdapter
#     from omnigent.server.schemas import ServerStreamEvent
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
    | SessionAgentChangedEvent
    | SessionTodosEvent
    | SessionTerminalPendingEvent
    | SessionSandboxStatusEvent
    | SessionSkillsEvent
    | SessionInputConsumedEvent
    | SessionInterruptedEvent
    | SessionCreatedEvent
    | SessionPresenceEvent
    # ── Transient (SSE-only) — session resource lifecycle ─────
    | SessionResourceCreatedEvent
    | SessionResourceDeletedEvent
    | SessionChildSessionUpdatedEvent
    | SessionChangedFilesInvalidatedEvent
    | SessionTerminalActivityEvent
    # ── Transient (SSE-only) — incremental token deltas ────────
    | OutputTextDeltaEvent
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


# Frozen set of every wire ``type`` literal across the union.
# Derived from :data:`ServerStreamEvent` so adding a new event variant
# to the union automatically updates the drift-detection set — there
# is no second list to keep in sync.
#
# ``ServerStreamEvent`` is ``Annotated[A | B | ..., Field(...)]``;
# ``get_args`` returns ``(A | B | ..., FieldInfo)``. The first element
# is the union, whose own ``get_args`` yields the variant classes.
_KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    # ``model_fields["type"].annotation`` is the ``Literal[...]``
    # carried by each variant; ``.__args__[0]`` extracts the
    # single string literal.
    cls.model_fields["type"].annotation.__args__[0]
    for cls in get_args(get_args(ServerStreamEvent)[0])
)


def is_known_event(name: str) -> bool:
    """
    Return whether ``name`` is a wire ``type`` literal in the union.

    Used by the drift-detection test
    (``tests/server/test_stream_events.py``): integration tests
    patch :func:`omnigent.runtime.session_stream.publish` to
    call this on every emitted ``event["type"]``; any string not
    in the union fails the test, catching new emissions that
    bypassed the source of truth.

    :param name: Candidate wire name to check, e.g.
        ``"response.output_text.delta"``.
    :returns: ``True`` if ``name`` is the ``type`` literal of a
        :data:`ServerStreamEvent` variant, ``False`` otherwise.
    """
    return name in _KNOWN_EVENT_TYPES


# Events the harness may emit on its per-turn SSE stream that are
# runner-internal: the runner intercepts and consumes them (matching by
# ``type`` on the raw frame, see ``omnigent/runner/app.py`` proxy_stream)
# and never relays them to clients. They are deliberately NOT part of the
# public :data:`ServerStreamEvent` union / openapi. This alias types the
# scaffold's per-turn event queue, which carries both the public events and
# these internal markers. See ``designs/RUNNER_MESSAGE_INGEST.md`` Part B.


class PolicyEvaluationRequestEvent(_SSEEventBase):
    """
    Runner-internal marker: harness requests policy evaluation.

    Emitted by the executor adapter before or after an LLM call so
    the runner can evaluate ``LLM_REQUEST`` / ``LLM_RESPONSE``
    policies on the Omnigent server. The runner intercepts this event in
    ``proxy_stream``, calls the Omnigent server's
    ``POST /sessions/{id}/policies/evaluate`` endpoint, and posts
    the verdict back to the harness as a ``policy_verdict`` inbound
    event. This event is **never** relayed to external clients —
    it is purely a runner↔harness handshake.

    :param type: Always ``"policy_evaluation.requested"``.
    :param evaluation_id: Unique correlation id for this
        evaluation, e.g. ``"poleval_abc123"``. The runner echoes
        it back in the ``policy_verdict`` inbound event so the
        scaffold can resolve the correct parked Future.
    :param phase: Proto-style phase string, e.g.
        ``"PHASE_LLM_REQUEST"`` or ``"PHASE_LLM_RESPONSE"``.
    :param data: Event data dict passed to the Omnigent server's
        policy evaluate endpoint, e.g.
        ``{"model": "gpt-4o", "messages_count": 42}``.
    """

    type: Literal["policy_evaluation.requested"]
    evaluation_id: str
    phase: str
    data: dict[str, Any]


HarnessStreamEvent = ServerStreamEvent | InjectionConsumedEvent | PolicyEvaluationRequestEvent
