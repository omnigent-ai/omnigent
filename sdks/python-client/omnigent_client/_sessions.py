"""Sessions namespace — create, snapshot, post events, interrupt, stream.

Targets the server's ``/v1/sessions`` route family. This is a thin
client over the snapshot + live-tail SSE contract documented in
``server/API.md``: callers ``create()`` a session from an agent
bundle, optionally
``post_event()`` more inputs, ``stream()`` the live events, and
``get()`` a snapshot to reconcile on reconnect. There is no replay —
the server intentionally does not buffer past events.

The SDK-side ``Session`` dataclass in this module mirrors
:class:`omnigent.server.schemas.SessionResponse`. Note that the
``Session`` class exported from :mod:`omnigent_client._session` is
an unrelated higher-level ``/v1/responses`` chat helper; the two
concepts share a name because the server route is ``/v1/sessions``
and the chat helper predates the new route. To avoid surfacing the
collision in the public namespace we deliberately do NOT re-export
this module's ``Session`` from :mod:`omnigent_client.__init__` —
callers obtain it via ``client.sessions.create()``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import TypeAdapter

from omnigent.server.schemas import ServerStreamEvent

from ._child_status import child_summary_busy
from ._errors import raise_for_status, require_json_object, response_body

# Default recursion cap for the sub-agent tree helpers. Mirrors web's
# ``MAX_TREE_DEPTH`` and the REPL's ``_MAX_SUBAGENT_TREE_DEPTH`` so the SDK
# rollup, the CLI ``↓`` tree, and the web Agents rail all walk the same depth.
_DEFAULT_SUBTREE_DEPTH = 3

# Adapter that validates a single SSE ``data:`` payload against the
# typed discriminated union. Built once at module load — TypeAdapter
# caches the validator. ``ServerStreamEvent`` is a Pydantic-discriminated
# union, so the result of ``validate_python`` is one of the concrete
# event subclasses (CreatedEvent, OutputTextDeltaEvent, …) — see
# :mod:`omnigent.server.schemas`.
_SERVER_STREAM_EVENT_ADAPTER: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)

# ── Module-level constants (rule 34) ─────────────────────────────────

_log = logging.getLogger("omnigent_client.sessions")

# Wire literal for the interrupt event ``type`` discriminator. Mirrors
# ``_INTERRUPT_TYPE`` in ``omnigent/server/routes/sessions.py``;
# kept as a module-level constant so :meth:`SessionsNamespace.interrupt`
# matches a single named symbol rather than an inline string.
_INTERRUPT_TYPE: str = "interrupt"


@dataclass(frozen=True)
class SessionEventInput:
    """
    Client-side mirror of :class:`omnigent.server.schemas.SessionEventInput`.

    Used as the body of ``POST /v1/sessions/{id}/events``. Frozen
    because the dataclass is
    a value object — callers should construct a new instance to model
    a new event rather than mutate an existing one.

    :param type: Discriminator for the event/input kind, e.g.
        ``"message"``, ``"function_call_output"``, ``"interrupt"``.
    :param data: Type-specific payload. Shape varies by ``type``; for
        ``"message"`` this looks like
        ``{"role": "user", "content": [{"type": "input_text",
        "text": "Hello"}]}``. For ``"interrupt"`` this is typically
        ``{}``.
    """

    type: str
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SessionEventInput:
        """
        Parse a :class:`SessionEventInput` from a JSON dict.

        :param raw: Raw JSON dict from the server. Must contain
            both ``type`` and ``data`` fields — the server schema
            (``server.schemas.SessionEventInput``) requires both.
        :returns: A typed :class:`SessionEventInput`.
        :raises KeyError: If ``type`` or ``data`` is missing from
            ``raw``. Failing loud on missing fields surfaces
            server/client schema drift instead of silently
            substituting an empty dict.
        :raises TypeError: If ``data`` is not a dict.
        """
        data = raw["data"]
        if not isinstance(data, dict):
            raise TypeError(
                f"SessionEventInput.data must be a dict, got {type(data).__name__}: {data!r}"
            )
        return cls(type=str(raw["type"]), data=data)


@dataclass(frozen=True)
class Session:
    """
    Client-side mirror of :class:`omnigent.server.schemas.SessionResponse`.

    Returned by :meth:`SessionsNamespace.create` and
    :meth:`SessionsNamespace.get`. Frozen because the dataclass models
    a single point-in-time snapshot — to observe state changes the
    caller fetches a new snapshot via :meth:`SessionsNamespace.get`.

    Note: distinct from :class:`omnigent_client._session.Session`
    (re-exported as ``omnigent_client.Session``), which is a
    higher-level chat helper over ``/v1/responses``. See this module's
    docstring for the rationale on why we do NOT re-export this class
    publicly.

    :param id: Unique session identifier (also the underlying
        conversation id), e.g. ``"conv_abc123"``.
    :param agent_id: Durable identifier of the bound agent, e.g.
        ``"ag_abc123"``. Stable across renames of the agent.
    :param agent_name: Human-readable name of the bound agent, e.g.
        ``"polly"``. Changes when the session is switched to a
        different agent in place (``POST .../switch-agent``), so
        attached clients can refresh their displayed agent label.
        ``None`` when the server couldn't resolve the agent row.
    :param status: Session lifecycle status. One of ``"idle"``,
        ``"running"``, or ``"failed"``.
    :param created_at: Unix epoch seconds of creation.
    :param title: Optional human-readable title, e.g.
        ``"debugging auth flow"``. ``None`` when unset.
    :param labels: Session-scoped guardrails labels. Empty dict
        when no labels have been written.
    :param runner_id: Runner currently bound to this session, e.g.
        ``"runner_abc123"``. ``None`` until the client binds one.
    :param reasoning_effort: Per-session reasoning-effort hint,
        e.g. ``"high"``. ``None`` means use the agent default.
    :param items: Committed conversation items in chronological
        order as raw dicts. Empty for a freshly created session.
    :param llm_model: The LLM model identifier from the bound
        agent's spec, e.g. ``"anthropic/claude-sonnet-4-6"``.
        ``None`` when the agent has no explicit ``llm:`` block.
    :param harness: The bound agent's canonical harness, e.g.
        ``"claude-sdk"`` or ``"openai-agents"``. Lets the REPL show
        the active credential for the correct provider family
        instead of guessing it from the model. ``None`` when
        unavailable.
    :param model_override: Per-session LLM model override, e.g.
        ``"claude-opus-4-7"``. ``None`` when no override is active
        and the agent's ``llm_model`` applies. Set via the REPL's
        ``/model`` command or the web model picker; both write
        the same column so the surfaces stay in sync.
    :param context_window: Context window size in tokens looked up
        server-side from litellm, e.g. ``200_000``. ``None`` when
        the model is not in litellm's registry.
    :param last_total_tokens: Provider-reported total tokens (input +
        output) from the most recently completed task, e.g. ``45231``.
        ``None`` when no task has completed yet. Used to seed the
        context-ring on resume without waiting for the next response.
    :param last_task_error: Error details from the most recently failed
        task, e.g. ``{"code": "executor_error", "message": "..."}``
        ``None`` when no task has failed.
    :param external_session_id: Runtime-native session id this
        conversation wraps (e.g. Claude Code's session uuid for
        ``omnigent claude`` sessions). ``None`` for regular AP-only
        conversations.
    :param archived: Whether the session is archived. Archived
        sessions are hidden from the default ``list`` listing and
        returned only when ``include_archived=True``. ``False`` for
        normal sessions.
    """

    id: str
    agent_id: str
    status: str
    created_at: int
    agent_name: str | None = None
    title: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    runner_id: str | None = None
    reasoning_effort: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    llm_model: str | None = None
    harness: str | None = None
    model_override: str | None = None
    context_window: int | None = None
    last_total_tokens: int | None = None
    last_task_error: dict[str, str] | None = None
    external_session_id: str | None = None
    archived: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Session:
        """
        Parse a :class:`Session` from a JSON dict.

        :param raw: Raw JSON dict from the server. Must contain
            ``id``, ``agent_id``, ``status``, and ``created_at``.
        :returns: A typed :class:`Session`.
        :raises KeyError: If a required field is missing.
        """
        items_raw = raw.get("items", [])
        labels_raw = raw.get("labels", {})
        raw_cw = raw.get("context_window")
        raw_ltt = raw.get("last_total_tokens")
        return cls(
            id=str(raw["id"]),
            agent_id=str(raw["agent_id"]),
            status=str(raw["status"]),
            created_at=int(raw["created_at"]),
            agent_name=raw.get("agent_name"),
            title=raw.get("title"),
            labels=labels_raw if isinstance(labels_raw, dict) else {},
            runner_id=raw.get("runner_id"),
            reasoning_effort=raw.get("reasoning_effort"),
            items=items_raw if isinstance(items_raw, list) else [],
            llm_model=raw.get("llm_model"),
            harness=raw.get("harness"),
            model_override=raw.get("model_override"),
            context_window=int(raw_cw) if raw_cw is not None else None,
            last_total_tokens=int(raw_ltt) if raw_ltt is not None else None,
            last_task_error=raw.get("last_task_error"),
            external_session_id=raw.get("external_session_id"),
            archived=bool(raw.get("archived", False)),
        )


@dataclass(frozen=True)
class SessionListItem:
    """
    Lightweight session summary from ``GET /v1/sessions``.

    Same shape as :class:`Session` minus ``items``. Used by the
    REPL's ``/switch`` command and similar list views.

    :param id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param agent_id: Durable identifier of the bound agent.
    :param status: Derived session lifecycle status.
    :param created_at: Unix epoch seconds of creation.
    :param updated_at: Unix epoch seconds of last update.
    :param title: Optional human-readable title.
    :param labels: Session-scoped guardrails labels.
    :param runner_id: Runner currently bound to the session.
    :param reasoning_effort: Per-session reasoning-effort hint.
    :param owner: User ID of the session owner.
    :param external_session_id: Runtime-native session id this
        conversation wraps (e.g. Claude Code's session uuid for
        ``omnigent claude`` sessions). ``None`` for regular AP-only
        conversations.
    :param pending_elicitations_count: Number of approval prompts
        currently waiting on this session. Powers the web sidebar's
        "needs attention" badge so a user with several sessions
        running can tell which ones are blocked on them. ``0`` when
        the session has no outstanding prompts.
    :param archived: Whether the session is archived. Returned by
        ``list`` only when ``include_archived=True``. ``False`` for
        normal sessions.
    """

    id: str
    agent_id: str
    status: str
    created_at: int
    updated_at: int
    title: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    runner_id: str | None = None
    reasoning_effort: str | None = None
    owner: str | None = None
    external_session_id: str | None = None
    pending_elicitations_count: int = 0
    archived: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SessionListItem:
        """
        Parse a :class:`SessionListItem` from a JSON dict.

        :param raw: Raw JSON dict from the server.
        :returns: A typed :class:`SessionListItem`.
        :raises KeyError: If a required field is missing.
        """
        labels_raw = raw.get("labels", {})
        return cls(
            id=str(raw["id"]),
            agent_id=str(raw["agent_id"]),
            status=str(raw["status"]),
            created_at=int(raw["created_at"]),
            updated_at=int(raw["updated_at"]),
            title=raw.get("title"),
            labels=labels_raw if isinstance(labels_raw, dict) else {},
            runner_id=raw.get("runner_id"),
            reasoning_effort=raw.get("reasoning_effort"),
            owner=raw.get("owner"),
            external_session_id=raw.get("external_session_id"),
            pending_elicitations_count=raw.get("pending_elicitations_count", 0),
            archived=bool(raw.get("archived", False)),
        )


class SessionsNamespace:
    """
    Client namespace for ``/v1/sessions`` endpoints.

    Provides the four operations the route module exposes: create a
    session from an uploaded agent bundle, bind it to a runner, fetch
    a snapshot, post an event into the session's input queue (or an
    interrupt that bypasses it), and live-tail the SSE stream. There
    is no replay; see the module docstring for the snapshot +
    live-tail reconnect contract.

    :param http: Pre-built ``httpx.AsyncClient`` shared with the
        parent :class:`OmnigentClient`. Owned by the parent;
        this namespace must NOT close it.
    :param base_url: Server base URL, e.g.
        ``"http://localhost:8000"``. Trailing slash already stripped
        by the parent client.
    """

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        """
        Initialize the namespace.

        :param http: Shared ``httpx.AsyncClient`` from the parent
            client.
        :param base_url: Server base URL, e.g.
            ``"http://localhost:8000"``.
        """
        self._http = http
        self._base = base_url

    async def create(
        self,
        bundle: bytes,
        *,
        filename: str = "agent.tar.gz",
        title: str | None = None,
        labels: dict[str, str] | None = None,
        reasoning_effort: str | None = None,
        workspace: str | None = None,
    ) -> Session:
        """
        Create a new session from an uploaded agent bundle.

        Calls multipart ``POST /v1/sessions`` with a JSON
        ``metadata`` form part and a ``bundle`` file part. The
        endpoint returns only ``{"session_id": "..."}``, so this
        method immediately fetches ``GET /v1/sessions/{id}`` and
        returns the full typed snapshot.

        :param bundle: Gzipped agent tarball bytes.
        :param filename: Filename sent for the multipart file part,
            e.g. ``"agent.tar.gz"``.
        :param title: Optional human-readable title for the session,
            e.g. ``"debugging auth flow"``.
        :param labels: Initial guardrails labels to set. ``None``
            starts with no labels.
        :param reasoning_effort: Optional per-session reasoning
            effort, e.g. ``"high"``. ``None`` uses the agent default.
        :param workspace: Optional absolute starting cwd to record on
            the session, e.g. ``"/Users/corey/projects/myapp"``.
            CLI-launched sessions populate this with ``os.getcwd()``
            so the Web UI can show "running locally in <workspace>";
            sessions with no recorded workspace pass ``None``.
        :returns: The newly created :class:`Session` snapshot.
        :raises OmnigentError: If the server returns a non-2xx
            status.
        """
        metadata: dict[str, Any] = {}
        if title is not None:
            metadata["title"] = title
        if labels is not None:
            metadata["labels"] = labels
        if reasoning_effort is not None:
            metadata["reasoning_effort"] = reasoning_effort
        if workspace is not None:
            metadata["workspace"] = workspace
        resp = await self._http.post(
            f"{self._base}/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={"bundle": (filename, bundle, "application/gzip")},
        )
        raise_for_status(resp.status_code, response_body(resp))
        created = require_json_object(resp, "POST /v1/sessions")
        session_id = str(created["session_id"])
        return await self.get(session_id)

    async def list(
        self,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        agent_id: str | None = None,
        agent_name: str | None = None,
        order: str = "desc",
        sort_by: str = "created_at",
        include_archived: bool = False,
    ) -> list[SessionListItem]:
        """
        List sessions with cursor-based pagination.

        Calls ``GET /v1/sessions``. Returns only sessions (conversations
        with an agent binding), not legacy conversations.

        :param limit: Maximum number of sessions to return
            (1-1000, default 20).
        :param after: Cursor — return sessions after this session ID.
        :param before: Cursor — return sessions before this session ID.
        :param agent_id: Filter to sessions bound to this agent,
            e.g. ``"ag_abc123"``. ``None`` returns all agents.
        :param agent_name: Filter to sessions whose bound agent row
            has this name, including distinct session-scoped agents
            that share the name. ``None`` returns all names.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param sort_by: Column to sort on, ``"created_at"`` or
            ``"updated_at"``.
        :param include_archived: When ``False`` (default), archived
            sessions are omitted. When ``True``, archived sessions are
            returned alongside active ones.
        :returns: List of :class:`SessionListItem`.
        :raises OmnigentError: On non-2xx status.
        """
        params: dict[str, str | int] = {"limit": limit, "order": order, "sort_by": sort_by}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if agent_id is not None:
            params["agent_id"] = agent_id
        if agent_name is not None:
            params["agent_name"] = agent_name
        if include_archived:
            params["include_archived"] = "true"
        resp = await self._http.get(
            f"{self._base}/v1/sessions",
            params=params,
        )
        raise_for_status(resp.status_code, response_body(resp))
        body = require_json_object(resp, "GET /v1/sessions")
        data = body.get("data", [])
        return [SessionListItem.from_dict(d) for d in data]  # type: ignore[attr-defined]

    async def bind_runner(
        self,
        session_id: str,
        *,
        runner_id: str,
    ) -> Session:
        """
        Bind or rebind a session to a registered runner.

        Calls ``PATCH /v1/sessions/{session_id}`` with
        ``{"runner_id": "..."}``. This is last-write-wins and
        replaces any prior binding.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param runner_id: Registered runner id, e.g.
            ``"runner_abc123"``.
        :returns: The updated :class:`Session` snapshot.
        :raises OmnigentError: On non-2xx status (404 when the
            session does not exist, 400 when the runner is not
            registered).
        """
        resp = await self._http.patch(
            f"{self._base}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Session.from_dict(
            require_json_object(resp, "PATCH /v1/sessions/{session_id}"),
        )

    async def unbind_runner(self, session_id: str) -> Session:
        """
        Clear a session's runner binding.

        PATCHes ``{"runner_id": ""}`` (the server's clear sentinel;
        ``None`` means "leave unchanged"). Counterpart to
        :meth:`bind_runner` for the 1:1 session↔runner invariant.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The updated :class:`Session` snapshot.
        :raises OmnigentError: On non-2xx status (404 when the
            session does not exist).
        """
        resp = await self._http.patch(
            f"{self._base}/v1/sessions/{session_id}",
            json={"runner_id": ""},
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Session.from_dict(
            require_json_object(resp, "PATCH /v1/sessions/{session_id}"),
        )

    async def set_reasoning_effort(
        self,
        session_id: str,
        *,
        reasoning_effort: str | None,
    ) -> Session:
        """
        Set or clear a session's reasoning-effort metadata.

        Calls ``PATCH /v1/sessions/{session_id}`` with
        ``{"reasoning_effort": "..."}``. ``None`` is sent as the
        server's explicit clear alias because omitted or JSON-null
        fields leave the current value unchanged.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param reasoning_effort: New effort, e.g. ``"high"``, or
            ``None`` to clear to the agent default.
        :returns: The updated :class:`Session` snapshot.
        :raises OmnigentError: On non-2xx status.
        """
        wire_effort = reasoning_effort if reasoning_effort is not None else "default"
        resp = await self._http.patch(
            f"{self._base}/v1/sessions/{session_id}",
            json={"reasoning_effort": wire_effort},
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Session.from_dict(
            require_json_object(resp, "PATCH /v1/sessions/{session_id}"),
        )

    async def set_model_override(
        self,
        session_id: str,
        *,
        model_override: str | None,
        silent: bool = False,
    ) -> Session:
        """
        Set or clear a session's LLM model override.

        Calls ``PATCH /v1/sessions/{session_id}`` with
        ``{"model_override": "..."}``. ``None`` is sent as the
        server's explicit clear alias because omitted or JSON-null
        fields leave the current value unchanged.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param model_override: New model identifier, e.g.
            ``"claude-opus-4-7"``, or ``None`` to clear to the
            agent default.
        :param silent: When ``True``, persist without triggering the
            claude-native ``/model`` slash-command forward into the
            tmux pane. Use for bind-time auto-apply (e.g. the REPL's
            pre-create ``/model`` snapshot) where the visible
            slash-command item would look like an unexpected first
            message in the chat. Default ``False`` matches the
            user-driven ``/model`` flow where the live forward is the
            desired feedback.
        :returns: The updated :class:`Session` snapshot.
        :raises OmnigentError: On non-2xx status (400 on invalid
            input, 404 when the session does not exist).
        """
        wire_model = model_override if model_override is not None else "default"
        body: dict[str, object] = {"model_override": wire_model}
        if silent:
            body["silent"] = True
        resp = await self._http.patch(
            f"{self._base}/v1/sessions/{session_id}",
            json=body,
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Session.from_dict(
            require_json_object(resp, "PATCH /v1/sessions/{session_id}"),
        )

    async def set_archived(
        self,
        session_id: str,
        *,
        archived: bool,
    ) -> Session:
        """
        Archive or unarchive a session.

        Calls ``PATCH /v1/sessions/{session_id}`` with
        ``{"archived": ...}``. Archived sessions are hidden from the
        default :meth:`list` listing and surfaced only with
        ``include_archived=True``. Owner-only (the web UI stops the
        session on archive, an owner-gated lifecycle action, so archive
        is held to the same gate); note this method only flips the
        archived flag — it does not stop the session.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param archived: ``True`` to archive, ``False`` to unarchive.
        :returns: The updated :class:`Session` snapshot.
        :raises OmnigentError: On non-2xx status (403 without owner
            access, 404 when the session does not exist).
        """
        resp = await self._http.patch(
            f"{self._base}/v1/sessions/{session_id}",
            json={"archived": archived},
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Session.from_dict(
            require_json_object(resp, "PATCH /v1/sessions/{session_id}"),
        )

    async def set_external_session_id(
        self,
        session_id: str,
        *,
        external_session_id: str,
    ) -> Session:
        """
        Record the runtime-native session id this session wraps.

        Calls ``PATCH /v1/sessions/{session_id}`` with
        ``{"external_session_id": "..."}``. Captured by a wrapper
        bridge from the underlying runtime (Claude Code, Codex,
        Pi, ...). Idempotent on same-value writes; the server
        returns ``400 invalid_input`` on attempted overwrite of
        a different existing value.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param external_session_id: Runtime-native session id,
            e.g. a Claude Code session uuid
            ``"a1b2c3d4-1234-5678-9abc-def012345678"``.
        :returns: The updated :class:`Session` snapshot.
        :raises OmnigentError: On non-2xx status (400 on
            overwrite conflict, 404 when the session does not
            exist).
        """
        resp = await self._http.patch(
            f"{self._base}/v1/sessions/{session_id}",
            json={"external_session_id": external_session_id},
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Session.from_dict(
            require_json_object(resp, "PATCH /v1/sessions/{session_id}"),
        )

    async def list_items(
        self,
        session_id: str,
        *,
        limit: int = 100,
        after: str | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        """
        List items in a session with cursor-based pagination.

        Calls ``GET /v1/sessions/{session_id}/items``. Same
        pagination contract as ``GET /v1/conversations/{id}/items``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return
            (1-1000, default 100).
        :param after: Cursor — return items after this item ID.
        :param order: Sort order, ``"asc"`` (chronological) or
            ``"desc"``.
        :returns: List of conversation item dicts.
        :raises OmnigentError: On non-2xx status (404 when the
            session does not exist).
        """
        params: dict[str, str | int] = {"limit": limit, "order": order}
        if after is not None:
            params["after"] = after
        resp = await self._http.get(
            f"{self._base}/v1/sessions/{session_id}/items",
            params=params,
        )
        raise_for_status(resp.status_code, response_body(resp))
        body = require_json_object(resp, "GET /v1/sessions/{session_id}/items")
        return body.get("data", [])

    async def child_sessions(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        List sub-agent (child) sessions under a parent session.

        Calls ``GET /v1/sessions/{session_id}/child_sessions`` and
        returns a page of ``ChildSessionSummary`` dicts (``id``,
        ``title``, ``tool``, ``agent_name``, ``busy``,
        ``current_task_status``, ``last_message_preview``,
        ``pending_elicitations_count``, …). The REPL recurses this per
        node to assemble the sub-agent tree shown on the main interface.

        :param session_id: Parent session/conversation identifier,
            e.g. ``"conv_parent123"``.
        :param limit: Maximum number of children to return
            (1-1000, default 100).
        :returns: List of child-session summary dicts (empty when the
            session has no sub-agents).
        :raises OmnigentError: On non-2xx status (404 when the
            session does not exist).
        """
        resp = await self._http.get(
            f"{self._base}/v1/sessions/{session_id}/child_sessions",
            params={"limit": limit},
        )
        raise_for_status(resp.status_code, response_body(resp))
        body = require_json_object(resp, "GET /v1/sessions/{session_id}/child_sessions")
        return body.get("data", [])

    async def child_sessions_tree(
        self,
        session_id: str,
        *,
        max_depth: int = _DEFAULT_SUBTREE_DEPTH,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List the whole sub-agent subtree under *session_id*, flattened.

        :meth:`child_sessions` is one level deep; this recurses it breadth-first
        to *max_depth*, mirroring web's ``useChildSessions`` per-node fetch.
        Each returned row is the raw ``ChildSessionSummary`` dict with an added
        ``parent_id`` recording the session it was queried under, so callers can
        reconstruct the hierarchy. *session_id* itself is not included.

        This is the shared recursion behind both the CLI ``↓`` sub-agent tree
        (the REPL seeds its registry from this) and :meth:`subtree_busy`.

        :param session_id: Root parent session identifier.
        :param max_depth: Levels to descend (1 = direct children only). Capped
            to match the CLI tree and the web Agents rail.
        :param limit: Per-level page size passed to :meth:`child_sessions`.
        :returns: Flattened list of child-session summary dicts, each carrying a
            ``parent_id`` key (empty when the session has no sub-agents).
        :raises OmnigentError: On non-2xx status (404 when the session does not
            exist).
        """
        nodes: list[dict[str, Any]] = []
        seen: set[str] = {session_id}
        frontier: list[str] = [session_id]
        depth = 0
        while frontier and depth < max_depth:
            next_frontier: list[str] = []
            for parent_id in frontier:
                rows = await self.child_sessions(parent_id, limit=limit)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sid = row.get("id")
                    if not isinstance(sid, str) or sid in seen:  # cycle / dupe guard
                        continue
                    seen.add(sid)
                    nodes.append({**row, "parent_id": parent_id})
                    next_frontier.append(sid)
            frontier = next_frontier
            depth += 1
        return nodes

    async def subtree_busy(
        self,
        session_id: str,
        *,
        max_depth: int = _DEFAULT_SUBTREE_DEPTH,
        limit: int = 100,
    ) -> bool:
        """Whether any sub-agent anywhere under *session_id* is still working.

        The queryable rollup an SDK driver needs: a parent's own ``status`` is
        per-session and reads ``idle`` once it delegates and returns to its own
        prompt, even while its sub-agents run. This recurses the subtree
        (:meth:`child_sessions_tree`) and applies the canonical
        :func:`omnigent_client.child_summary_busy` predicate — the same "busy"
        definition the CLI badge and the web ``SubagentsPanel`` use — so an
        eval loop can gate "your turn" on real subtree activity.

        Point-in-time (no subscription); re-call for a fresh value.

        :param session_id: Root parent session identifier.
        :param max_depth: Levels to descend (see :meth:`child_sessions_tree`).
        :param limit: Per-level page size.
        :returns: ``True`` while any descendant is busy, else ``False``.
        :raises OmnigentError: On non-2xx status.
        """
        nodes = await self.child_sessions_tree(session_id, max_depth=max_depth, limit=limit)
        return any(child_summary_busy(node) for node in nodes)

    async def get(self, session_id: str) -> Session:
        """
        Fetch the current snapshot of a session.

        Calls ``GET /v1/sessions/{session_id}``. The returned
        :class:`Session` includes committed items and any pending
        queued inputs — clients use this on reconnect to reconcile
        state observed via :meth:`stream`.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The current :class:`Session` snapshot.
        :raises OmnigentError: If the server returns a non-2xx
            status (404 when the session does not exist).
        """
        resp = await self._http.get(
            f"{self._base}/v1/sessions/{session_id}",
        )
        raise_for_status(resp.status_code, response_body(resp))
        return Session.from_dict(require_json_object(resp, "GET /v1/sessions/{session_id}"))

    async def post_event(
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Post an event/input item to a running session.

        Calls ``POST /v1/sessions/{session_id}/events``. The body
        is a single event dict matching :class:`SessionEventInput`
        on the wire. The server returns 202 with a small ack body
        (``{"queued": true, "item_id": "..."}`` for persisted
        item events; ``{"queued": false}`` for interrupt / approval).

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param event: The event payload, e.g.
            ``{"type": "message", "data": {"role": "user",
            "content": [{"type": "input_text",
            "text": "Hello"}]}}``. Must contain a ``type`` key;
            ``data`` shape is validated server-side per ``type``.
        :raises OmnigentError: If the server returns a non-2xx
            status (404 when the session does not exist).
        """
        resp = await self._http.post(
            f"{self._base}/v1/sessions/{session_id}/events",
            json=event,
        )
        raise_for_status(resp.status_code, response_body(resp))
        return require_json_object(resp, "POST /v1/sessions/{session_id}/events")

    async def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Resolve an outstanding elicitation via its dedicated URL.

        Calls ``POST /v1/sessions/{session_id}/elicitations/
        {elicitation_id}/resolve`` with the MCP-shape
        ``ElicitationResult`` body — the URL-based counterpart to
        delivering the verdict as a ``{"type": "approval", ...}``
        event through :meth:`post_event`. The elicitation id travels
        in the URL path; the body carries only ``action`` (and
        optional form ``content``). Both paths converge on the same
        server-side resolver, so the effect is identical; routing
        through the URL keeps human approval on a dedicated,
        owner-gated path rather than an in-band session event.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param elicitation_id: Correlation id of the elicitation to
            resolve, e.g. ``"elicit_abc123"``.
        :param result: MCP ``ElicitationResult`` body, e.g.
            ``{"action": "accept"}`` or ``{"action": "accept",
            "content": {"choice": "a"}}``. ``action`` is one of
            ``"accept"`` / ``"decline"`` / ``"cancel"``.
        :returns: The server ack dict (``{"queued": false}``).
        :raises OmnigentError: If the server returns a non-2xx
            status (404 when the session does not exist).
        """
        resp = await self._http.post(
            f"{self._base}/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json=result,
        )
        raise_for_status(resp.status_code, response_body(resp))
        return require_json_object(
            resp,
            f"POST /v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        )

    async def fork(
        self,
        source_session_id: str,
        *,
        title: str | None = None,
        up_to_response_id: str | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """
        Fork an existing session into a new session.

        Calls ``POST /v1/sessions/{source_session_id}/fork``.
        Deep-copies the source session's items into a new session
        with the same agent binding.

        :param source_session_id: ID of the session to fork, e.g.
            ``"conv_abc123"``.
        :param title: Optional title for the forked session. When
            ``None``, the server derives one from the source.
        :param up_to_response_id: Optional truncation point, e.g.
            ``"resp_abc123"``. When set, the fork copies history only
            up to and including that response; ``None`` copies the
            full history.
        :param model_override: Optional model id to launch the fork on
            ("restart with model"), e.g. ``"databricks-gpt-5-4-mini"``.
            Overrides the model the fork inherits from the source; the
            server validates and family-checks it. ``None`` keeps the
            source's model.
        :returns: Raw response dict matching the ``SessionResponse``
            shape: ``id``, ``agent_id``, ``status``, ``created_at``,
            ``title``, ``labels``, ``reasoning_effort``, and
            ``items``.
        :raises OmnigentError: 404 if *source_session_id* does
            not exist; 400 if the source has no agent binding,
            *up_to_response_id* names no response in the source, or
            *model_override* is invalid / cross-family for the fork.
        """
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if up_to_response_id is not None:
            body["up_to_response_id"] = up_to_response_id
        if model_override is not None:
            body["model_override"] = model_override
        resp = await self._http.post(
            f"{self._base}/v1/sessions/{source_session_id}/fork",
            json=body,
        )
        raise_for_status(resp.status_code, response_body(resp))
        return require_json_object(
            resp,
            f"POST /v1/sessions/{source_session_id}/fork",
        )

    async def compact(self, session_id: str) -> None:
        """
        Request explicit context compaction for a session.

        Convenience wrapper over :meth:`post_event` that posts a
        ``{"type": "compact", "data": {}}`` control event. The server
        runs compaction without appending a user message or starting a
        normal agent turn.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :raises OmnigentError: If the server returns a non-2xx status.
        """
        await self.post_event(
            session_id,
            {"type": "compact", "data": {}},
        )

    async def interrupt(self, session_id: str) -> None:
        """
        Interrupt a running session.

        Convenience wrapper over :meth:`post_event` that posts an
        ``{"type": "interrupt", "data": {}}`` event. The server
        bypasses the input queue and cancels the loop directly,
        co-emitting ``response.incomplete`` (``reason=
        "user_interrupt"``) and ``session.interrupted`` on the
        stream.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :raises OmnigentError: If the server returns a non-2xx
            status (404 when the session does not exist).
        """
        await self.post_event(
            session_id,
            {"type": _INTERRUPT_TYPE, "data": {}},
        )

    async def stream(
        self,
        session_id: str,
    ) -> AsyncIterator[ServerStreamEvent]:
        """
        Live-tail the session's SSE event stream.

        Calls ``GET /v1/sessions/{session_id}/stream``. Yields one
        :class:`ServerStreamEvent` per server event in arrival order. The
        server does NOT replay history; on reconnect, callers should
        open a new stream and reconcile via :meth:`get`.

        Iteration ends cleanly when the server closes the stream
        (the ``[DONE]`` sentinel). Network errors propagate to the
        caller — auto-reconnect lives at the application layer
        because the snapshot/dedupe step is application-specific.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :yields: :class:`ServerStreamEvent` envelopes whose ``type`` is a
            :class:`omnigent.server.schemas.ServerStreamEvent`
            member and whose ``data`` is the event-specific payload
            dict.
        :raises OmnigentError: If the server returns a non-2xx
            status when opening the stream (404 when the session
            does not exist).
        """
        async for event in _stream_session_events(
            self._http,
            self._base,
            session_id,
        ):
            yield event


# ── Helpers ─────────────────────────────────────────────────────────


async def _stream_session_events(
    http: httpx.AsyncClient,
    base_url: str,
    session_id: str,
) -> AsyncIterator[ServerStreamEvent]:
    """
    Open a single SSE connection and yield parsed
    :class:`ServerStreamEvent` instances.

    Does NOT handle reconnection — that is the caller's
    responsibility. Network errors (``httpx.RemoteProtocolError``,
    ``httpx.ReadTimeout``, etc.) propagate.

    :param http: Shared ``httpx.AsyncClient``.
    :param base_url: Server base URL, e.g.
        ``"http://localhost:8000"``.
    :param session_id: Session/conversation identifier whose stream
        to subscribe to, e.g. ``"conv_abc123"``.
    :yields: :class:`ServerStreamEvent` envelopes parsed from the SSE
        ``data:`` payload.
    """
    async with http.stream(
        "GET",
        f"{base_url}/v1/sessions/{session_id}/stream",
    ) as resp:
        if resp.status_code >= 400:
            await resp.aread()
            raise_for_status(resp.status_code, response_body(resp))

        async for event in _parse_sse_lines(resp.aiter_lines()):
            yield event


async def _parse_sse_lines(
    line_stream: AsyncIterator[str],
) -> AsyncIterator[ServerStreamEvent]:
    """
    Parse raw SSE text lines into :class:`ServerStreamEvent` instances.

    Expects the standard SSE framing emitted by the server's
    ``_format_sse`` helper: ``event: <type>`` followed by
    ``data: <json>``, separated by blank lines. The ``[DONE]``
    sentinel terminates the stream cleanly. Each ``data:`` JSON
    payload is the full envelope dict ``{"type": ..., "data": ...}``
    that the server publishes; we feed it into Pydantic to enforce
    the :class:`ServerStreamEvent` discriminator.

    Malformed payloads (non-JSON, non-dict, unknown event type) are
    logged and skipped so a single bad event does not poison the
    stream — same forward-compatibility posture as ``_sse.py``.

    :param line_stream: Async iterator of text lines from
        ``httpx.Response.aiter_lines()``.
    :yields: Parsed :class:`ServerStreamEvent` instances.
    """
    current_event: str | None = None

    async for line in line_stream:
        line = line.rstrip("\r\n")

        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: ") and current_event is not None:
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                return
            parsed = _try_parse_envelope(data_str)
            if parsed is not None:
                yield parsed
            current_event = None
        elif line == "":
            current_event = None


def _try_parse_envelope(raw: str) -> ServerStreamEvent | None:
    """
    Parse a single SSE ``data:`` payload into a typed
    :data:`ServerStreamEvent`.

    The server emits each event with a flat shape carrying the
    fields documented on the matching subclass in
    :mod:`omnigent.server.schemas` (e.g. ``{"type":
    "response.output_text.delta", "delta": "Hello",
    "sequence_number": 5}``). The
    :data:`_SERVER_STREAM_EVENT_ADAPTER` dispatches on ``type`` to
    the right concrete model; unknown event names raise
    ``ValueError`` from the validator and are logged + skipped here
    for forward compatibility.

    :param raw: Raw JSON string from an SSE ``data:`` field, e.g.
        ``'{"type": "response.output_text.delta", "delta": "Hi"}'``.
    :returns: A typed :data:`ServerStreamEvent` (one of the
        concrete event subclasses), or ``None`` if the payload is
        malformed or names an unknown event type.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("Failed to parse SSE data: %s", raw[:200])
        return None
    if not isinstance(decoded, dict):
        _log.warning("SSE data is not a JSON object: %s", raw[:200])
        return None
    try:
        return _SERVER_STREAM_EVENT_ADAPTER.validate_python(decoded)
    except ValueError as exc:
        # ValueError covers Pydantic ValidationError (a subclass) for
        # unknown discriminator values and missing required fields.
        # Log + skip so a single bad event does not abort the
        # iteration; the caller still observes every well-formed
        # event in arrival order.
        _log.debug("Skipping unparseable session event: %s (%s)", raw[:200], exc)
        return None
