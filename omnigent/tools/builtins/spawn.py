"""Spawn and check tools for sub-agent lifecycle management.

SpawnTool launches sub-agents as independent tasks via the
TaskStore interface. CheckSubAgentsTool returns their current
status without blocking. CancelSubAgentTool stops a running
sub-agent. See designs/STEERABLE_SUBAGENTS.md for the full design.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from omnigent.entities import (
    Conversation,
    ConversationItem,
)
from omnigent.runtime import pending_elicitations
from omnigent.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    CLOSED_TITLE_INFIX,
    is_session_closed,
)
from omnigent.spec import AgentSpec
from omnigent.stores import ConversationStore
from omnigent.tools.base import Tool, ToolContext

# Maximum number of recent conversation items to include in
# check_sub_agents activity for non-completed sub-agents.

# Maximum characters per content field in activity items.
# Longer content is truncated with a " [truncated]" suffix.
# Enough to capture a meaningful tool call or result (e.g. a
# code snippet, search output, or structured JSON arguments)
# without bloating the parent's prompt. At 5 items × 2000 chars
# the activity section is bounded to ~10k chars.
_ACTIVITY_MAX_CHARS = 2000

# sys_session_get_history bounds. ``tail_items`` defaults to 10 (more
# context than ``check_task``'s 5 because peek is invoked
# explicitly by the LLM rather than auto-injected on a poll).
# The cap of 50 keeps the worst-case prompt addition bounded
# (50 items × _ACTIVITY_MAX_CHARS ≈ 100k chars) while still
# letting the LLM request a substantial slice for triage.
_HISTORY_DEFAULT_TAIL = 10
_HISTORY_MAX_TAIL = 50

# sys_session_close still rewrites the stored title internally to free
# the DB's ``(parent_conversation_id, title)`` unique slot. API display
# paths strip this marker and expose ``omnigent.closed=true`` instead.
_CLOSED_TITLE_INFIX = CLOSED_TITLE_INFIX


class SysSessionSendTool(Tool):
    """
    Send a message to a named sub-agent — auto-create-or-continue.

    Sub-agent sessions are separate Omnigent agent sessions (own
    conversation, visible in the session tree) — distinct from any
    built-in subagent/Task tool the wrapping harness provides.

    Two addressing modes:

    - **Named** — pass ``(agent, title)``. The first call with a
      pair creates the child conversation and starts a turn;
      subsequent calls with the same pair continue it (the
      sub-agent sees its full prior history plus the new ``args``).
    - **By session id** — pass ``session_id`` to post to an
      **existing** child session (e.g. one returned by
      ``sys_session_create``). This is a child-only write: the
      target must be a direct child of the caller
      (``parent_session_id == caller``), so an orchestrator can
      only drive sessions inside its own subtree.

    Exactly one of ``(agent + title)`` or ``session_id`` must be
    given, always with ``args``.

    Returns a JSON handle in the same shape as
    :class:`omnigent.runtime.workflow._AsyncToolHandle`:
    ``{task_id, kind: "sub_agent", agent, title, conversation_id,
    status, message}``. The result auto-delivers via the unified
    ``async_work_complete`` topic; the LLM can abort with
    ``sys_cancel_task`` if it wants to stop early.

    Errors:

    - ``unknown sub-agent type`` — ``agent`` is not one of the
      declared sub-agent names.
    - ``sub_agent_busy`` — the existing session has a non-terminal
      task already running. Wait for completion (it auto-delivers
      via the drain) or cancel before sending again.
    - ``model`` rejections — the optional ``args.model`` override is
      create-time-only and only valid for harnesses with model
      plumbing; sends that pass it to an existing session, an
      unplumbed harness, or with a malformed id return an error.

    There is no ``name_already_exists`` error in this merged tool
    — a pre-existing ``(agent, title)`` is the expected case
    (continuation), not a conflict. To force a fresh child for
    the same logical title, call :class:`SysSessionCloseTool`
    first to tombstone the existing one.

    :param sub_specs: Name-to-AgentSpec mapping for available
        sub-agents, e.g. ``{"researcher": AgentSpec(...)}``.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_send"``."""
        return "sys_session_send"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Send a message to a sub-agent session. Sub-agent sessions "
            "are separate Omnigent agent sessions (own conversation, "
            "visible in the session tree) — distinct from any built-in "
            "subagent/Task tool your harness provides. Two modes: pass "
            "(agent, title) to spawn-or-continue a named sub-agent (the "
            "first call with a pair creates it, later calls continue "
            "it); or pass session_id to post to an existing child "
            "session you created (e.g. via sys_session_create) — this "
            "is confined to your direct children. Provide exactly one "
            "of (agent + title) or session_id, always with args. "
            "Returns the child's output when its turn completes. To run "
            "multiple sessions in parallel, emit multiple "
            "sys_session_send tool_calls in the same response — they "
            "dispatch concurrently. To attach previously-uploaded files, "
            "pass their file ids via the object args form's 'file_ids' "
            "list on the first named (agent, title) send only; file_ids "
            "cannot be used with session_id or when continuing an existing "
            "named session."
        )

    def __init__(self, sub_specs: dict[str, AgentSpec]) -> None:
        """
        Initialize with the agent's available sub-agent specs.

        :param sub_specs: Name-to-AgentSpec mapping, e.g.
            ``{"researcher": AgentSpec(...)}``.
        """
        self._sub_specs = sub_specs

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema with dynamic ``agent`` enum.

        The ``agent`` enum is the list of sub-agent names the
        parent declares; the LLM can only dispatch declared
        types.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return _build_sys_session_send_schema(self._sub_specs)


def _spec_opts_into_harness_override(spec: Any) -> bool:
    """
    Return ``True`` if a sub-agent spec opts into the ``args.harness`` override.

    The override is allowlist-gated (design D.4): a sub-agent advertises it
    only when its ``executor.config.allowed_harnesses`` declares a non-empty
    allowlist. This mirrors the dispatch-side opt-in read in
    ``omnigent/runner/tool_dispatch.py`` (``_subagent_allowed_harnesses``) so
    the schema gate and the runtime guard agree on what "opted in" means.
    Specs without the opt-in keep the base ``{input, purpose, model}`` args
    contract.

    :param spec: A sub-agent :class:`AgentSpec` (or structural equivalent).
    :returns: ``True`` when the spec declares a non-empty allowlist.
    """
    executor = getattr(spec, "executor", None)
    config = getattr(executor, "config", None)
    if isinstance(config, dict):
        raw_allowed: Any = config.get("allowed_harnesses")
    elif config is not None:
        raw_allowed = getattr(config, "allowed_harnesses", None)
    else:
        raw_allowed = None
    if not isinstance(raw_allowed, (list, tuple, set, frozenset)):
        return False
    return any(isinstance(entry, str) and entry for entry in raw_allowed)


def _build_sys_session_send_schema(
    sub_specs: dict[str, AgentSpec],
) -> dict[str, Any]:
    """
    Build the OpenAI function schema for ``sys_session_send``.

    The ``agent`` parameter's enum is dynamic — derived from the
    keys of ``sub_specs`` so the LLM only sees the sub-agents
    the parent agent actually declares. When ``sub_specs`` is
    empty (the agent declares no sub-agents), the named-mode
    ``agent`` / ``title`` parameters are omitted entirely and the
    schema advertises the ``session_id`` mode only — an empty
    enum would be both unusable and invalid for some providers.

    :param sub_specs: Name-to-AgentSpec mapping; may be empty.
    :returns: OpenAI function-format schema dict.
    """
    named_mode_properties: dict[str, Any] = {}
    if sub_specs:
        type_enum = sorted(sub_specs.keys())
        type_descriptions = {
            name: (spec.description or f"Sub-agent {name!r}.") for name, spec in sub_specs.items()
        }
        type_desc_text = "\n".join(f"  {name}: {desc}" for name, desc in type_descriptions.items())
        named_mode_properties = {
            "agent": {
                "type": "string",
                "enum": type_enum,
                "description": (
                    "Named mode: the sub-agent type to "
                    "spawn-or-continue. Must be one of the "
                    "declared sub-agent names. Pair with "
                    "'title'; omit when using 'session_id'. "
                    f"Available types:\n{type_desc_text}"
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    "Named mode: a unique-within-this-parent "
                    "label for the sub-agent session, e.g. "
                    "'auth' or 'payments'. Lets later turns "
                    "reuse the same conversation via another "
                    "sys_session_send call with the same "
                    "title. Titles must be distinct under one "
                    "parent for the same agent. Pair with "
                    "'agent'; omit when using 'session_id'."
                ),
            },
        }
    description = (
        SysSessionSendTool.description()
        if sub_specs
        else (
            "Send a message to an existing child session you created "
            "(e.g. via sys_session_create), identified by session_id. "
            "Child sessions are separate Omnigent agent sessions (own "
            "conversation, visible in the session tree) — not your "
            "harness's built-in subagent/Task tool, which remains the "
            "right choice for quick in-context delegation. "
            "Confined to your direct children. Returns the child's "
            "output when its turn completes. To run multiple sessions "
            "in parallel, emit multiple sys_session_send tool_calls in "
            "the same response — they dispatch concurrently."
        )
    )
    # ``args.harness`` is allowlist-gated (design D.4): advertise it only when
    # at least one declared sub-agent opts in via
    # ``executor.config.allowed_harnesses``. Specs without the opt-in keep the
    # base {input, purpose, model} args object, so the orchestrator never sees a
    # harness knob it can't use. The dispatch-side guard in tool_dispatch.py
    # re-enforces the opt-in per child (and the server create route does too).
    harness_opt_in = any(_spec_opts_into_harness_override(spec) for spec in sub_specs.values())
    harness_property: dict[str, Any] = (
        {
            "harness": {
                "type": "string",
                "description": (
                    "Optional harness override for "
                    "this sub-agent session, e.g. "
                    "'opencode-native'. Applies only "
                    "when this send CREATES the "
                    "session AND the sub-agent spec "
                    "allowlists it via "
                    "executor.config.allowed_harnesses; "
                    "otherwise rejected. Omitted = the "
                    "sub-agent's declared harness."
                ),
            }
        }
        if harness_opt_in
        else {}
    )
    args_description = (
        (
            "The user-input message to send to the sub-agent. The sub-agent "
            "treats this as the first user turn in its conversation. Pass a "
            "plain string for the normal contract, or pass "
            "{input, purpose, model, harness, cost_budget} when a spec-level "
            "policy requires explicit dispatch metadata, a per-dispatch model "
            "override, an allowlisted harness override, or a per-subagent "
            "cost budget."
        )
        if harness_opt_in
        else (
            "The user-input message to send to the sub-agent. The sub-agent "
            "treats this as the first user turn in its conversation. Pass a "
            "plain string for the normal contract, or pass "
            "{input, purpose, model, cost_budget} when a spec-level policy "
            "requires explicit dispatch metadata, a per-dispatch model "
            "override, or a per-subagent cost budget."
        )
    )
    return {
        "type": "function",
        "function": {
            "name": SysSessionSendTool.name(),
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    **named_mode_properties,
                    "session_id": {
                        "type": "string",
                        "description": (
                            "By-session-id mode: post to an existing "
                            "child session (e.g. one returned by "
                            "sys_session_create), e.g. 'conv_abc123'. "
                            "Must be a direct child of the calling "
                            "session. Use instead of agent + title."
                        ),
                    },
                    "args": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "input": {
                                        "type": "string",
                                        "description": (
                                            "The user-input message to send to the sub-agent."
                                        ),
                                    },
                                    "purpose": {
                                        "type": "string",
                                        "description": (
                                            "Optional dispatch metadata. "
                                            "Some agent specs use guardrails "
                                            "that require this field to "
                                            "classify headless helper work, "
                                            "e.g. 'implement', 'review', "
                                            "'explore', or 'search'."
                                        ),
                                    },
                                    "model": {
                                        "type": "string",
                                        "description": (
                                            "Optional model the sub-agent "
                                            "harness should run, e.g. a "
                                            "databricks-* endpoint name or "
                                            "a harness-native model id. "
                                            "Applies only when this send "
                                            "CREATES the sub-agent session; "
                                            "omitted = the harness default."
                                        ),
                                    },
                                    "file_ids": {
                                        "type": "array",
                                        "items": {"type": "string", "minLength": 1},
                                        "minItems": 1,
                                        "uniqueItems": True,
                                        "description": (
                                            "Optional list of file ids for "
                                            "files you previously uploaded. "
                                            "Accepted only on the first named "
                                            "(agent, title) send, when the "
                                            "sub-agent session is created. "
                                            "Cannot be used with session_id "
                                            "or when continuing an existing "
                                            "named session."
                                        ),
                                    },
                                    **harness_property,
                                    "cost_budget": {
                                        "type": "object",
                                        "properties": {
                                            "max_cost_usd": {
                                                "type": "number",
                                                "description": (
                                                    "Optional hard limit in USD. "
                                                    "Blocks tool calls once exceeded "
                                                    "on expensive models."
                                                ),
                                            },
                                            "ask_thresholds_usd": {
                                                "type": "array",
                                                "items": {"type": "number"},
                                                "description": (
                                                    "Optional soft warning checkpoints "
                                                    "in USD. The subagent asks for "
                                                    "approval the first time spend "
                                                    "crosses each threshold (each must "
                                                    "be < max_cost_usd if both are set)."
                                                ),
                                            },
                                        },
                                        "description": (
                                            "Optional per-subagent cost budget configuration "
                                            "with max_cost_usd (hard limit) and/or "
                                            "ask_thresholds_usd (soft checkpoints). At least "
                                            "one must be set. Applies only when this send "
                                            "creates the session; ignored on continuation sends."
                                        ),
                                    },
                                },
                                "required": ["input"],
                                "additionalProperties": False,
                            },
                        ],
                        "description": args_description,
                    },
                },
                # Only ``args`` is universally required; the
                # (agent + title) vs session_id mode split is
                # enforced in the runner handler (the schema can't
                # express "exactly one of two groups" portably across
                # providers).
                "required": ["args"],
                "additionalProperties": False,
            },
        },
    }


# ── Spawn implementation ──────────────────────────────


def _resolve_parent_conversation_id(ctx: ToolContext) -> str:
    """
    Return the conversation_id of the tool invocation's parent session.

    Phase 4: child sub-agent conversations point at their
    immediate parent (not the root). For nested sub-agents (a
    sub-agent calling sys_session_send), this returns the
    spawning sub-agent's own conversation, so
    ``sys_session_list`` from inside that sub-agent surfaces its
    own children rather than the root's.

    :param ctx: The tool execution context; ``ctx.conversation_id``
        is the canonical session identifier set by the workflow when
        dispatching tools.
    :returns: The conversation_id.
    :raises RuntimeError: If ``ctx.conversation_id`` is ``None`` —
        means the tool was invoked outside of an active workflow.
    """
    if ctx.conversation_id is None:
        raise RuntimeError(
            "spawn tools require a conversation_id in ToolContext — "
            "must run inside an active workflow"
        )
    return ctx.conversation_id


# ── Phase 4: continue helper (consumed by SysSessionSendTool) ──


# ── Phase 4: sys_session_list ──────────────────────────


class SysSessionListTool(Tool):
    """
    List sub-agents under this conversation, plus accessible sessions.

    Returns two views:

    - ``sub_agents`` — the named ``(agent, title)`` children (and, for
      a child caller, its parent/siblings) under this conversation. The
      LLM uses these to decide which pairs already exist (so a follow-up
      ``sys_session_send`` continues rather than spawns) and to grab each
      child's ``conversation_id`` for ``sys_session_get_history`` /
      ``sys_session_get_info`` / ``sys_session_close``.
    - ``sessions`` — a **global** view of every session the caller can
      access (bounded by the server's per-user permission model), each
      with its status and runner connectivity. An optional
      ``agent_name`` filter narrows this list. This powers
      orchestration: discovering sessions to inspect
      (``sys_agent_get`` / ``sys_session_get_info``) or drive
      (``sys_session_send`` by ``session_id``).

    The global ``sessions`` view is populated only on the runner
    (REST) path, where the server enforces permissions; the in-process
    path returns ``sub_agents`` with an empty ``sessions`` list.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_list"``."""
        return "sys_session_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List sessions in two views. 'sub_agents': the named "
            "(agent, title) children under this conversation (and your "
            "parent/siblings) — use their conversation_id to read "
            "history, get info, or close. 'sessions': a global list of "
            "every session "
            "you can access, each with status + runner connectivity, "
            "for orchestration (inspect via sys_agent_get / "
            "sys_session_get_info, or drive via sys_session_send by "
            "session_id). Pass agent_name to filter the global list to "
            "sessions running that agent."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict; an optional ``agent_name`` filter.
        """
        return {
            "type": "function",
            "function": {
                "name": SysSessionListTool.name(),
                "description": SysSessionListTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": (
                                "Optional: filter the global 'sessions' "
                                "list to sessions whose bound agent has "
                                "this name, e.g. 'researcher'. Does not "
                                "affect the 'sub_agents' view."
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Return the named sub-agents under the caller's conversation.

        In-process path: returns the ``sub_agents`` children view with an
        empty ``sessions`` list — the global, permission-bounded session
        listing is only available on the runner (REST) path, which has
        the caller's identity and the server's permission checks (this
        path has neither). ``arguments`` is ignored here.

        :param arguments: Ignored on the in-process path (the global
            ``agent_name`` filter only applies to the REST ``sessions``
            view).
        :param ctx: Server-side execution context.
        :returns: JSON ``{"sub_agents": [{"agent": ..., "title": ...,
            "conversation_id": ...}, ...], "sessions": []}``.
        """
        del arguments
        from omnigent.runtime import get_conversation_store

        parent_conversation_id = _resolve_parent_conversation_id(ctx)
        conv_store = get_conversation_store()
        children = conv_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_conversation_id,
            # 100 is a safe ceiling — agents that need more named
            # sub-agents than this are an antipattern; the LLM
            # would lose track regardless.
            limit=100,
        )
        result: list[dict[str, str]] = []
        for child in children.data:
            # Title is "<agent>:<title>" — split into the LLM-
            # friendly fields. Skip rows whose title doesn't
            # match the convention (defensive — Phase-3
            # anonymous spawns left None titles, but those have
            # NULL parent_conversation_id and won't appear in
            # this query at all). Also skip closed rows so they
            # never re-surface to the LLM.
            if child.title is None or ":" not in child.title:
                continue
            if is_session_closed(child.labels, child.title):
                continue
            sa_agent, _, sa_title = child.title.partition(":")
            result.append(
                {
                    "agent": sa_agent,
                    "title": sa_title,
                    "conversation_id": child.id,
                }
            )
        # ``sessions`` (the global, permission-bounded view) is empty on
        # the in-process path — it has no caller identity to scope by.
        # The runner (REST) path populates it via GET /v1/sessions.
        return json.dumps({"sub_agents": result, "sessions": []})


class SysSessionGetInfoTool(Tool):
    """
    Return a single session's metadata snapshot (no transcript).

    A **global read**: resolves against any session the caller is
    permitted to access (bounded by the server's per-user permission
    model), not just the caller's spawn subtree. Reports lifecycle
    status, title, agent binding (id + name), runner binding and live
    connectivity, host, reasoning effort, effective model, parent
    linkage, workspace / git branch, and the count of outstanding
    approval prompts. For the conversation transcript, use
    ``sys_session_get_history`` instead.

    ``session_id`` is optional — when omitted, the caller's own
    session is described.

    Runner-dispatched: the runner proxies ``GET /v1/sessions/{id}``
    (plus a best-effort ``GET /v1/runners/{id}/status`` for live
    connectivity) and projects the result. Returns
    ``session_not_found`` when the id is unknown and ``access_denied``
    when the server refuses the read.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_get_info"``."""
        return "sys_session_get_info"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Return a session's metadata: lifecycle status, title, "
            "agent binding (id/name), runner binding + connectivity, "
            "host, reasoning effort, model, parent session, workspace, "
            "and outstanding approval prompts. Global read — any "
            "session you can access. Pass session_id to target another "
            "session; omit it to describe your own. Metadata only — "
            "use sys_session_get_history for the conversation transcript."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict; ``session_id`` is optional.
        """
        return {
            "type": "function",
            "function": {
                "name": SysSessionGetInfoTool.name(),
                "description": SysSessionGetInfoTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The session (conversation_id) to "
                                "describe, e.g. 'conv_abc123'. Get this "
                                "from sys_session_list or a prior "
                                "sys_session_send handle. Omit to "
                                "describe the calling session itself."
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }


class SysSessionShareTool(Tool):
    """
    Grant another user (or the public) access to a session.

    Enabled by the spec's top-level ``agent_session_sharing:`` flag
    (:class:`omnigent.spec.types.SharePolicy`), which is its sole gate:
    ``none`` leaves the tool unregistered, ``non-public`` allows
    granting named users, and ``public`` additionally allows the
    ``__public__`` sentinel (anonymous read of the full transcript).
    ``allow_public`` carries that last tier into the tool so it can
    both advertise and refuse public grants when the policy is
    ``non-public``.

    ``session_id`` is optional — when omitted, the caller's own session
    is shared, which is the common case ("share this session with X").
    ``user_id`` is the grantee's email, or (when ``allow_public``) the
    sentinel ``"__public__"`` for anonymous read-only access. ``level``
    is ``"read"`` (default), ``"edit"``, or ``"manage"``; the server
    caps public grants at read.

    Runner-dispatched: the runner proxies ``PUT
    /v1/sessions/{id}/permissions`` using its authenticated server
    client, so the grant runs with the session user's own identity and
    is subject to the server's permission checks (the caller needs
    manage-level access — which the session owner has). Returns
    ``access_denied`` when the server refuses and ``session_not_found``
    for an unknown id.

    :param allow_public: Whether ``__public__`` grants are permitted —
        ``True`` only when the spec's ``agent_session_sharing:`` flag is
        ``public``. Reflected in the schema and hard-enforced by the runner.
    """

    def __init__(self, allow_public: bool) -> None:
        """
        :param allow_public: ``True`` when the spec's
            ``agent_session_sharing:`` policy is ``public`` — permits
            granting the ``__public__`` sentinel.
            ``False`` for ``non-public`` (named users only).
        """
        self._allow_public = allow_public

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_share"``."""
        return "sys_session_share"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Share a session with another user by granting them access "
            "(level 'read' default, 'edit', or 'manage'). Omit "
            "session_id to share the calling session itself, or pass it "
            "to share another session you manage. Requires manage-level "
            "access (the session owner has it)."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        The ``user_id`` description reflects ``allow_public``: it only
        advertises the ``__public__`` sentinel when public grants are
        permitted, so the model isn't told about an option the runner
        would reject.

        :returns: Dict with ``"type": "function"`` and a ``"function"``
            sub-dict; ``user_id`` is required, ``level`` and
            ``session_id`` optional.
        """
        if self._allow_public:
            user_id_desc = (
                "Grantee's email, e.g. 'alice@example.com', or the "
                "sentinel '__public__' for anonymous read-only access "
                "(anyone with the link)."
            )
        else:
            user_id_desc = (
                "Grantee's email, e.g. 'alice@example.com'. "
                "Public/anonymous sharing is not enabled for this agent."
            )
        return {
            "type": "function",
            "function": {
                "name": SysSessionShareTool.name(),
                "description": SysSessionShareTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": user_id_desc,
                        },
                        "level": {
                            "type": "string",
                            "enum": ["read", "edit", "manage"],
                            "description": (
                                "Permission level to grant. Defaults to "
                                "'read'. Public grants are capped at "
                                "'read' regardless of this value."
                            ),
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The session (conversation_id) to share, "
                                "e.g. 'conv_abc123'. Omit to share the "
                                "calling session itself."
                            ),
                        },
                    },
                    "required": ["user_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysSessionCreateTool(Tool):
    """
    Create a child session from an existing agent or a local bundle.

    The child is a separate Omnigent agent session — its own
    conversation, visible in the session tree, optionally a different
    registered agent — not the wrapping harness's built-in
    subagent/Task tool (which remains the right choice for quick
    in-context helpers).

    A **child-only write**: the new session's ``parent_session_id`` is
    forced to the caller's own session, so an orchestrator can only spawn
    sessions inside its own subtree — never a top-level or sibling
    session. The child inherits the caller's runner (co-location), so it
    starts executing as soon as a message is queued.

    Two addressing modes — exactly one of ``agent_id`` or
    ``config_path`` must be given:

    - **By agent id** — an existing agent the caller can see (a
      built-in/template or session-bound entry from ``sys_agent_list``
      — both row kinds carry an ``agent_id`` — or the agent bound to
      an accessible session via ``sys_agent_get``). Proxies the JSON
      ``POST /v1/sessions`` create. The direct path for any
      already-registered agent: no bundle download or re-upload.
    - **By config path** — a NEW agent uploaded from local disk: an
      agent config YAML, agent directory, or pre-built ``.tar.gz``
      bundle inside the caller's working directory (e.g. one authored
      with ``sys_os_write``). The runner bundles the source and
      proxies the multipart ``POST /v1/sessions`` create, registering
      a fresh session-scoped agent.

    An optional ``message`` is queued as the child's first user turn;
    an optional ``title`` labels the session.

    Returns a handle ``{conversation_id, agent_id, title, status}``. The
    session runs asynchronously — use ``sys_session_get_history`` /
    ``sys_session_get_info`` to monitor it, or ``sys_session_send`` (with
    the returned ``conversation_id``) to drive it further.

    Runner-dispatched: both modes force ``parent_session_id`` to the
    caller.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_create"``."""
        return "sys_session_create"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Create a child session from an agent. This launches a "
            "separate Omnigent agent session — its own conversation, "
            "visible in the session tree, optionally a different "
            "registered agent. It is not your harness's built-in "
            "subagent/Task tool: for quick in-context helpers (parallel "
            "exploration, scoped reads) prefer your native subagent "
            "tool if you have one; use sys_session_create to launch "
            "another registered agent or a durable, independently "
            "visible session. Two modes — provide "
            "exactly one: agent_id launches an existing agent (any "
            "agent_id from sys_agent_list's builtins or session_agents, "
            "or from sys_agent_get); config_path uploads a new agent "
            "from a local agent config YAML, agent directory, or "
            ".tar.gz bundle in your working directory (e.g. authored "
            "with sys_os_write) and launches it. Always use agent_id "
            "for an agent that already exists — never download and "
            "re-upload its bundle. Optionally queue an initial user "
            "message. The new session is always a child of the calling "
            "session (you cannot create top-level or sibling sessions). "
            "Returns {conversation_id, agent_id, title, status}; the "
            "session runs asynchronously — monitor it with "
            "sys_session_get_history / sys_session_get_info or drive it "
            "with sys_session_send."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict. Exactly one of ``agent_id`` /
            ``config_path`` is required — the mode split is enforced
            in the runner handler (the schema can't express
            "exactly one of two fields" portably across providers).
        """
        return {
            "type": "function",
            "function": {
                "name": SysSessionCreateTool.name(),
                "description": SysSessionCreateTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": (
                                "Existing-agent mode: the agent to "
                                "launch, e.g. 'ag_abc123'. Get it from "
                                "sys_agent_list (both builtins and "
                                "session_agents rows carry it) or "
                                "sys_agent_get (a session's agent). "
                                "Use instead of config_path."
                            ),
                        },
                        "config_path": {
                            "type": "string",
                            "description": (
                                "New-agent mode: path to a local agent "
                                "config YAML, agent directory, or "
                                ".tar.gz bundle, relative to your "
                                "working directory, e.g. "
                                "'.omnigent/agent-configs/helper.yaml'. "
                                "Uploads it as a fresh agent and "
                                "launches the child from it. Use "
                                "instead of agent_id."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "Optional human-readable label for the "
                                "new session, e.g. 'auth refactor'."
                            ),
                        },
                        "message": {
                            "type": "string",
                            "description": (
                                "Optional first user message to queue "
                                "for the child. Omit to create an idle "
                                "session and drive it later via "
                                "sys_session_send."
                            ),
                        },
                    },
                    # Only the always-optional fields are listed in
                    # ``required`` (none): the agent_id-vs-config_path
                    # mode split is enforced in the runner handler.
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }


# ── Check / result helpers ────────────────────────────


def _project_activity_item(
    item: ConversationItem,
) -> dict[str, str | None]:
    """
    Project a conversation item into a compact dict.

    Handles three item types: messages (user/assistant text),
    function calls (tool name + args), and function call
    outputs (tool name + result). All content fields are
    truncated to ``_ACTIVITY_MAX_CHARS``.

    :param item: A conversation item from the sub-agent's
        conversation.
    :returns: A compact dict with ``role``, ``type``, and
        content fields.
    """
    # Convert Pydantic model to dict so .get() works uniformly
    # across all data types (MessageData, FunctionCallData, etc.).
    data = item.data.model_dump()
    if item.type == "function_call":
        return {
            "role": "assistant",
            "type": "tool_call",
            "name": data.get("name"),
            "args": _truncate(
                data.get("arguments", ""),
            ),
        }
    if item.type == "function_call_output":
        return {
            "role": "tool",
            "type": "tool_result",
            "name": data.get("name"),
            "content": _truncate(
                data.get("output", ""),
            ),
        }
    # Message item — extract role and text content.
    role = data.get("role", "unknown")
    text_parts: list[str] = []
    for block in data.get("content", []):
        if isinstance(block, dict):
            text = block.get("text") or block.get("output_text")
            if text:
                text_parts.append(text)
        elif isinstance(block, str):
            text_parts.append(block)
    return {
        "role": role,
        "type": "text",
        "content": _truncate("\n".join(text_parts)),
    }


def _truncate(text: str) -> str:
    """
    Truncate text to ``_ACTIVITY_MAX_CHARS``.

    :param text: The input string.
    :returns: The original string if short enough, or a
        truncated version with ``" [truncated]"`` suffix.
    """
    if len(text) <= _ACTIVITY_MAX_CHARS:
        return text
    return text[:_ACTIVITY_MAX_CHARS] + " [truncated]"


# ── 13a: sys_session_get_history / sys_session_close ─────────


def _find_open_child_by_title(
    *,
    parent_conversation_id: str,
    sa_agent: str,
    sa_title: str,
    conv_store: ConversationStore,
) -> Conversation | None:
    """
    Look up an open named child conversation by ``(agent, title)``.

    Closed children are excluded by the bare ``"<agent>:<title>"``
    title match: ``sys_session_close`` rewrites the stored title to
    include the legacy closed suffix so the original composite title
    no longer matches. Returns ``None`` when no open child matches.

    :param parent_conversation_id: The parent's conversation id, e.g.
        ``"conv_abc123"``.
    :param sa_agent: The sub-agent type (e.g. ``"researcher"``) — the
        ``agent`` argument on the LLM-facing surface.
    :param sa_title: The session title (e.g. ``"auth-flow"``) — the
        ``title`` argument on the LLM-facing surface.
    :param conv_store: Conversation store the lookup runs against.
    :returns: The matching :class:`Conversation` or ``None``.
    """
    composite = f"{sa_agent}:{sa_title}"
    children = conv_store.list_conversations(
        kind="sub_agent",
        parent_conversation_id=parent_conversation_id,
        title=composite,
        limit=1,
    )
    return next(
        (c for c in children.data if not is_session_closed(c.labels, c.title)),
        None,
    )


@dataclass
class _SessionResolution:
    """
    Outcome of resolving a ``sys_session_*`` tool's arguments
    against the live store state.

    Bundles every value the call site needs after validation:
    parsed args (so optional fields like ``tail_items`` are
    available without re-parsing), the resolved conversation
    store handle (saves a duplicate ``get_conversation_store()``
    round-trip), the caller's tree-root id (used in error
    payloads), and the resolved target :class:`Conversation`.

    The target child's ``(agent, title)`` are derived on demand
    via :meth:`agent_title_from_title` so call sites that need
    them for error/result shaping don't carry a duplicate copy
    of state that lives on ``child``.

    :param args: Full parsed arguments dict — call sites read any
        optional fields (e.g. ``tail_items``) from here.
    :param conv_store: Conversation store the resolution ran
        against; reused by the caller for follow-up reads/writes.
    :param child: The matching :class:`Conversation` row.
    """

    args: dict[str, Any]
    conv_store: ConversationStore
    child: Conversation


@dataclass(frozen=True)
class _AgentTitle:
    """
    Decomposed sub-agent identity recovered from a conversation title.

    :param agent: Sub-agent name (the part before the first ``":"`` in
        the stored title), e.g. ``"researcher"``.
    :param title: LLM-facing session title with any tombstone marker
        stripped, e.g. ``"draft-1"``.
    """

    agent: str
    title: str


@dataclass(frozen=True)
class _CallerTree:
    """
    Caller location in the spawn tree.

    :param conversation_id: The caller's own conversation id.
    :param root_id: Id of the spawn tree's root conversation. Equals
        ``conversation_id`` for top-level callers; for sub-agents it
        points at the top-level ancestor.
    """

    conversation_id: str
    root_id: str


def _agent_title_from_conversation(child: Conversation) -> _AgentTitle:
    """
    Split a child conversation's stored title into agent + title.

    Named sub-agents persist ``"<agent>:<title>"`` in
    ``Conversation.title`` (and internally rewrite to
    ``"<agent>:<title>:closed:<conv_id>"`` when closed). Both forms
    split on the first ``":"`` to recover the LLM-facing components.

    :param child: The child :class:`Conversation`. Must have a
        non-empty title containing at least one ``":"``.
    :returns: An :class:`_AgentTitle` with the closed marker stripped
        from the title side when present.
    :raises RuntimeError: If the title is missing or doesn't contain
        a ``":"`` separator — both indicate a framework invariant
        broken upstream (sub-agent conversations are always created
        with ``"<agent>:<title>"``). Failing loud here surfaces the
        bug at its source instead of letting empty fields propagate
        into JSON results and rebuilt tombstone titles.
    """
    if not child.title or ":" not in child.title:
        raise RuntimeError(
            f"sub-agent conversation {child.id!r} has malformed title "
            f"{child.title!r} — expected '<agent>:<title>' format"
        )
    sa_agent, _, remainder = child.title.partition(":")
    sa_title, _, _closed_marker = remainder.partition(_CLOSED_TITLE_INFIX)
    return _AgentTitle(agent=sa_agent, title=sa_title)


def _resolve_caller_tree(ctx: ToolContext) -> _CallerTree:
    """
    Resolve the caller's conversation and its tree's root.

    :param ctx: Active tool execution context (carries the caller
        task id).
    :returns: A :class:`_CallerTree` describing the caller. For
        top-level callers ``root_id`` equals ``conversation_id``;
        for sub-agents it points at the spawn tree's root.
    :raises RuntimeError: If the caller task or its conversation
        row is missing — both are framework invariants.
    """
    from omnigent.runtime import get_conversation_store

    caller_conv_id = _resolve_parent_conversation_id(ctx)
    caller_conv = get_conversation_store().get_conversation(caller_conv_id)
    if caller_conv is None:
        raise RuntimeError(
            f"caller conversation {caller_conv_id!r} not found — "
            "framework invariant broken (a tool ran without a "
            "live conversation row)"
        )
    # ``root_conversation_id`` is NOT NULL post-migration
    # d8e2f3b4c910 — every row has a populated root.
    return _CallerTree(
        conversation_id=caller_conv_id,
        root_id=caller_conv.root_conversation_id,
    )


def _resolve_session_call(
    *,
    arguments: str,
    ctx: ToolContext,
    tool_name: str,
) -> _SessionResolution | str:
    """
    Validate ``sys_session_*`` arguments and look up the target child by id.

    Encapsulates the validation + lookup pipeline shared by
    :class:`SysSessionGetHistoryTool` and :class:`SysSessionCloseTool`:
    parse JSON arguments, validate ``conversation_id`` is a
    non-empty string, fetch the conversation, and verify it
    lives in the caller's spawn tree (matched by
    ``root_conversation_id``). On any failure, returns the
    JSON error string the LLM would receive from invoking the
    tool directly — call sites just propagate it.

    :param arguments: Raw JSON arguments string from the LLM.
    :param ctx: The tool's :class:`ToolContext`.
    :param tool_name: The calling tool's name (for error
        messages, e.g. ``"sys_session_get_history"``).
    :returns: A :class:`_SessionResolution` on success; a JSON
        error string when the call should fail.
    """
    from omnigent.runtime import get_conversation_store

    args = _parse_session_args(
        arguments,
        required=("conversation_id",),
        tool_name=tool_name,
    )
    if isinstance(args, str):
        return args
    target_id = args["conversation_id"]
    if not isinstance(target_id, str) or not target_id:
        return json.dumps({"error": f"{tool_name} requires a non-empty 'conversation_id' string"})

    caller = _resolve_caller_tree(ctx)
    conv_store = get_conversation_store()
    target = conv_store.get_conversation(target_id)
    if target is None:
        return json.dumps(
            {
                "error": "session_not_found",
                "conversation_id": target_id,
            }
        )
    if target.root_conversation_id != caller.root_id:
        # Tree-scoping is enforced here rather than at the route
        # layer because peek/close are LLM-facing tools, not HTTP
        # endpoints — the only authority the caller has is its
        # own spawn tree.
        return json.dumps(
            {
                "error": "session_out_of_tree",
                "conversation_id": target_id,
                "message": (
                    "target conversation is not part of the "
                    "caller's session tree; peek/close are scoped "
                    "to the caller's root."
                ),
            }
        )
    if target.parent_conversation_id is None:
        # Top-level conversations don't carry the
        # ``<agent>:<title>`` invariant on ``title`` that
        # downstream :func:`_agent_title_from_conversation`
        # depends on. Refuse here with a typed error rather
        # than letting the title parse blow up.
        return json.dumps(
            {
                "error": "session_not_a_sub_agent",
                "conversation_id": target_id,
                "message": (
                    "target conversation is a top-level "
                    "conversation, not a sub-agent session; "
                    "peek/close only operate on sub-agents."
                ),
            }
        )
    return _SessionResolution(
        args=args,
        conv_store=conv_store,
        child=target,
    )


def _busy_check_or_none(
    *,
    child_conv_id: str,
) -> str | None:
    """
    Return a busy-error JSON string if the child session is actively running.

    The tasks table has been removed. Busy state is now determined from
    the relay-fed ``_session_status_cache`` in the sessions route module.
    This helper always returns ``None`` (the session-level busy check now
    lives in the sessions route's ``_session_status_cache``); it is
    retained to preserve the call-site contract for tools that invoke it.

    :param child_conv_id: The child conversation id, e.g. ``"conv_abc123"``.
    :returns: Always ``None`` — busy detection based on the tasks table is
        no longer available at the tool layer.
    """
    return None


def _clamp_tail_items(raw: Any) -> int | str:
    """
    Validate + clamp the ``tail_items`` argument for peek.

    The schema's ``maximum`` keyword is advisory (LLM providers
    don't all enforce schema validation), so the handler does the
    clamp itself rather than trusting the input. Non-integer or
    sub-1 inputs return a JSON error string suitable for
    returning verbatim to the LLM.

    :param raw: The raw value from the parsed args dict; may be
        an int, a string the LLM sent, or anything else if the
        provider passed the JSON value through untyped.
    :returns: An int in ``[1, _HISTORY_MAX_TAIL]`` on success; a
        JSON error string on failure.
    """
    try:
        tail_items = int(raw)
    except (TypeError, ValueError):
        return json.dumps({"error": f"tail_items must be an integer, got {raw!r}"})
    if tail_items < 1:
        return json.dumps({"error": "tail_items must be >= 1"})
    return min(tail_items, _HISTORY_MAX_TAIL)


class SysSessionGetHistoryTool(Tool):
    """
    Return the recent conversation items (history) of a session.

    The LLM uses ``sys_session_get_history`` to inspect another
    session's recent activity without sending a new turn or waiting on
    a poll. The target is identified by ``conversation_id`` (obtained
    from ``sys_session_list``, ``sys_agent_list``, or a prior
    ``sys_session_send`` handle).

    A **global read**: on the runner (REST) path it reads any session
    the caller is permitted to access (bounded by the server's per-user
    permission model via the auth-gated ``GET /items`` endpoint). The
    in-process path is tree-scoped (it has no caller identity), reading
    only sessions sharing the caller's ``root_conversation_id``.

    Item content is projected through the same compact activity
    format used by ``check_task`` recent_activity: tool calls,
    tool results, and message text, each truncated to
    ``_ACTIVITY_MAX_CHARS``.

    Returns ``session_not_found`` when the conversation_id does
    not exist, ``session_out_of_tree`` when the server denies the read
    (in-process: a different spawn tree).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_get_history"``."""
        return "sys_session_get_history"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Read the most recent items from a session's conversation "
            "without sending input. Global read — any session you can "
            "access (not just sub-agents in your spawn tree), bounded "
            "by the server's per-user permission model. Returns the "
            "tail of conversation items (assistant/user messages, tool "
            "calls, tool results) in chronological order. Returns "
            "session_not_found if conversation_id is unknown, or "
            "session_out_of_tree if the server denies read access."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": SysSessionGetHistoryTool.name(),
                "description": SysSessionGetHistoryTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {
                            "type": "string",
                            "description": (
                                "The target session's "
                                "conversation_id. Get this from "
                                "sys_session_list, sys_agent_list, "
                                "or a prior sys_session_send handle. "
                                "Any session you can access — need "
                                "not be in your spawn tree."
                            ),
                        },
                        "tail_items": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _HISTORY_MAX_TAIL,
                            "description": (
                                f"Number of recent items to return. "
                                f"Defaults to {_HISTORY_DEFAULT_TAIL}; "
                                f"clamped to {_HISTORY_MAX_TAIL} to keep "
                                "prompt size bounded."
                            ),
                        },
                    },
                    "required": ["conversation_id"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Look up the target sub-agent and return its recent items.

        :param arguments: JSON-encoded arguments string, e.g.
            ``'{"conversation_id": "conv_abc123", "tail_items": 5}'``.
        :param ctx: Server-side execution context.
        :returns: JSON ``{"conversation_id": ..., "agent": ...,
            "title": ..., "items": [...]}`` on success;
            ``{"error": "...", ...}`` on failure.
        """
        resolution = _resolve_session_call(
            arguments=arguments,
            ctx=ctx,
            tool_name=SysSessionGetHistoryTool.name(),
        )
        if isinstance(resolution, str):
            return resolution
        tail_items = _clamp_tail_items(
            resolution.args.get("tail_items", _HISTORY_DEFAULT_TAIL),
        )
        if isinstance(tail_items, str):
            return tail_items
        page = resolution.conv_store.list_items(
            resolution.child.id,
            limit=tail_items,
            order="desc",
        )
        # ``list_items(order="desc")`` returns newest-first; reverse
        # to chronological order so the LLM reads top-to-bottom.
        items: list[dict[str, Any]] = [
            _project_activity_item(item) for item in reversed(page.data)
        ]
        # A parked elicitation never lands in the conversation store
        # (it lives only in the pending-elicitations index), so without
        # this a peek on a sub-agent blocked on AskUserQuestion would
        # show no sign it needs input. Append the index's outstanding
        # prompts after the stored tail — they are the most recent
        # thing the sub-agent did.
        items.extend(
            pending_elicitations.project_for_peek(event)
            for event in pending_elicitations.snapshot_for(resolution.child.id)
        )
        labelled = _agent_title_from_conversation(resolution.child)
        return json.dumps(
            {
                "conversation_id": resolution.child.id,
                "agent": labelled.agent,
                "title": labelled.title,
                "items": items,
            }
        )


class SysSessionCloseTool(Tool):
    """
    Tombstone any sibling sub-agent session in the same spawn tree.

    The LLM uses ``sys_session_close`` to declare that a
    sub-agent conversation is finished. The child's title is
    rewritten so future ``sys_session_send`` calls with the same
    ``(agent, title)`` no longer find it and create a fresh
    child instead. Tree-scoping is enforced by the tool on both
    dispatch paths — the in-process path here and the runner's
    REST path (``_session_close_via_rest``): callers can only
    close sub-agent conversations sharing their
    ``root_conversation_id``. Because close is a write, this gate
    is stricter than the bare per-user edit permission the
    underlying PATCH route enforces — edit access to a session in
    a *different* spawn tree is not enough to close it.

    The close marker is non-destructive: the child conversation's
    items remain in the store and can still be read by id (e.g. via
    the server REST API). User-input write paths reject closed
    children, and the ``(parent, title)`` lookup path is closed off.

    Refuses to tombstone a session whose child has a non-terminal
    task in flight (returns ``sub_agent_busy``) — closing during a
    live turn would leave a running child orphaned from the
    parent's tracking. The LLM should wait for the in-flight task
    to drain (or call ``sys_cancel_task``) before closing.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_close"``."""
        return "sys_session_close"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Tombstone a sibling sub-agent session in the same "
            "spawn tree so future sys_session_send calls with the "
            "same (agent, title) create a fresh child rather than "
            "continuing this one. Returns session_not_found if "
            "conversation_id is unknown, session_out_of_tree if it "
            "isn't part of the caller's tree, or sub_agent_busy if "
            "the child has a non-terminal task in flight."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": SysSessionCloseTool.name(),
                "description": SysSessionCloseTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "conversation_id": {
                            "type": "string",
                            "description": (
                                "The target sub-agent's "
                                "conversation_id. Get this from "
                                "sys_session_list or from a "
                                "prior sys_session_send handle. "
                                "Must be in the caller's spawn "
                                "tree."
                            ),
                        },
                    },
                    "required": ["conversation_id"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Look up the target sub-agent and tombstone it.

        :param arguments: JSON-encoded arguments string, e.g.
            ``'{"conversation_id": "conv_abc123"}'``.
        :param ctx: Server-side execution context.
        :returns: JSON ``{"closed": true, "conversation_id": ...,
            "agent": ..., "title": ...}`` on success;
            ``{"error": "...", ...}`` on failure.
        """
        resolution = _resolve_session_call(
            arguments=arguments,
            ctx=ctx,
            tool_name=SysSessionCloseTool.name(),
        )
        if isinstance(resolution, str):
            return resolution
        busy_error = _busy_check_or_none(child_conv_id=resolution.child.id)
        if busy_error is not None:
            return busy_error
        labelled = _agent_title_from_conversation(resolution.child)
        # Re-build the tombstoned title from the parsed components so
        # the marker lands in the canonical position even if the
        # original title used uncommon characters around the colon.
        new_title = f"{labelled.agent}:{labelled.title}{_CLOSED_TITLE_INFIX}{resolution.child.id}"
        resolution.conv_store.update_conversation(resolution.child.id, title=new_title)
        resolution.conv_store.set_labels(
            resolution.child.id,
            {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
        )
        return json.dumps(
            {
                "closed": True,
                "conversation_id": resolution.child.id,
                "agent": labelled.agent,
                "title": labelled.title,
            }
        )


def _parse_session_args(
    arguments: str,
    *,
    required: tuple[str, ...],
    tool_name: str,
) -> dict[str, Any] | str:
    """
    Parse + validate the JSON arguments for a ``sys_session_*`` tool.

    Mirrors ``_parse_sys_session_send_args`` but is reusable
    across the new ``sys_session_*`` family with caller-supplied
    required-field lists. The error JSON it returns is the same
    shape the LLM sees on bad arguments today.

    :param arguments: Raw JSON string from the LLM, e.g.
        ``'{"agent": "researcher", "title": "auth", "args": "..."}'``.
    :param required: Required argument names (e.g.
        ``("agent", "title", "args")``); each must appear in the
        parsed object or the call returns an error.
    :param tool_name: The calling tool's name (e.g.
        ``"sys_session_get_history"``). Used only for richer error
        messages — the LLM otherwise can't tell which tool's
        validator complained when the same shape recurs.
    :returns: Parsed dict on success; JSON error string on
        failure (handed back verbatim to the LLM).
    """
    try:
        args = json.loads(arguments) if arguments else {}
    except (json.JSONDecodeError, TypeError) as exc:
        return json.dumps({"error": f"invalid arguments to {tool_name}: {exc}"})
    if not isinstance(args, dict):
        return json.dumps(
            {"error": f"{tool_name} arguments must be a JSON object"},
        )
    for field_name in required:
        if field_name not in args:
            return json.dumps(
                {"error": f"{tool_name} missing required field: {field_name}"},
            )
    return args
