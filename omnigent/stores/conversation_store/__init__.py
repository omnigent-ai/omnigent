"""Conversation store — manages conversations and their items."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from omnigent.entities import (
    Agent,
    Conversation,
    ConversationItem,
    NewConversationItem,
    PagedList,
)

# Label set on a fork of a session that had a working directory. Its
# value is the source session id. Presence marks the (unbound) clone as
# needing a host + working directory before it can run, so the
# online-dot reports it offline until bound and the UI opens the
# directory picker instead of silently dropping the first message.
# Forks of chat-only sources (no workspace) get no label and resume
# in-process like a brand-new chat session. Canonical home is the store
# layer; the server route and the SQLAlchemy store both import it.
FORK_SOURCE_LABEL_KEY = "omnigent.fork.source_id"

# One-shot fork directive: the SOURCE session's runtime-native session id
# (e.g. the source claude-native Claude Code session uuid), stamped on the
# clone at fork time when the source had one. A native harness launching
# the (still-unbound) clone uses it to locate the source's local transcript
# and clone it into the clone's OWN project dir under a freshly assigned
# uuid (rewriting sessionId/cwd), then launch plain ``--resume <our_uuid>``
# (see ``omnigent.claude_native._clone_claude_transcript`` and the
# fork-resume branch in ``omnigent.runner.app``), so the clone opens with
# the prior history instead of a blank session. Once the clone captures its
# OWN native session id (``external_session_id`` set on first launch), this
# directive is inert — the launch path only consults it while
# ``external_session_id`` is still NULL. Cleared/ignored thereafter.
FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY = "omnigent.fork.source_external_session_id"

# Fork directive: set when the fork binds a NATIVE target harness
# (claude-native / codex-native) whose history should carry over. A native
# CLI ignores the Omnigent transcript, so the runner rebuilds the target's
# on-disk transcript before launch. Two rebuild paths share this directive:
# when the source was a SAME-FAMILY native session its captured
# ``external_session_id`` is also stamped (see
# FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY) and the runner clones that
# transcript; otherwise (an SDK or cross-family source) the runner builds
# the native transcript from the fork's copied Omnigent items
# (``_ensure_local_claude_resume_transcript`` /
# ``_ensure_local_codex_resume_rollout`` — the converters consume Omnigent's
# normalized item shape, so the source harness doesn't matter). Set by the
# route whenever the target is native. Inert once the clone captures its
# own native session id (the launch path consults it only while
# ``external_session_id`` is NULL).
FORK_CARRY_HISTORY_LABEL_KEY = "omnigent.fork.carry_history"

# Set by an in-place agent switch (``POST /v1/sessions/{id}/switch-agent``):
# the BUILT-IN agent id the session was switched away from, so the UI can
# offer a one-click "Switch back". A convenience pointer only — switching
# back is a fresh re-clone of that built-in (a new session-scoped agent,
# fresh harness), not a transactional undo. Persisted (not instance-scoped),
# so it survives across turns and is overwritten by each subsequent switch.
SWITCH_PREVIOUS_BUILTIN_LABEL_KEY = "omnigent.switch.previous_builtin_id"

# Opt-in DANGEROUS launch directive for a codex-native session: when set to
# ``"1"`` the runner launches Codex with
# ``--dangerously-bypass-approvals-and-sandbox`` and puts the app-server
# threads into the matching no-approval / no-sandbox stance (see
# ``omnigent.runner.app._codex_native_launch_config`` and
# ``codex_native_app_server.build_codex_remote_args``). Stored as a plain
# conversation label (cheap thread metadata, like the fork directives above)
# so it survives reload without a schema migration. The web UI gates turning
# this on behind a typed confirmation + a persistent red warning banner; any
# value other than ``"1"`` (incl. absent) leaves the session in Codex's
# normal approval/sandbox stance. See issue #657.
CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY = "omnigent.codex_native.bypass_sandbox"

# Labels that must NOT cross into a new session context — deliberately
# dropped both when forking (not copied to the clone) and on an in-place
# agent switch (deleted from the switched session). Two distinct reasons
# put a key here:
#
#   * Runtime state bound to ONE running instance — the native bridge-id
#     labels would route the new context's terminal + web injection to the
#     SOURCE's claude/codex bridge (whose active-session marker isn't the
#     clone → "session no longer active"); the context-size metrics would
#     display the source's last usage. The bridge-id literals mirror the
#     harness modules' ``*_BRIDGE_ID_LABEL_KEY`` constants; a store test
#     cross-checks them so a rename in those modules fails loudly here.
#
#   * Per-context safety opt-in — the DANGEROUS codex full-bypass directive
#     (:data:`CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY`). Letting it ride into a
#     fork (a new session + workspace) or survive an agent switch would
#     silently re-arm ``--dangerously-bypass-approvals-and-sandbox`` with no
#     typed re-confirmation and no banner, violating the "impossible to
#     enable accidentally" contract (#657). Dropping it forces each session
#     that runs bypass to make its own explicit opt-in.
_INSTANCE_SCOPED_LABEL_KEYS = frozenset(
    {
        "omnigent.claude_native.bridge_id",
        "omnigent.codex_native.bridge_id",
        "omnigent.last_context_tokens",
        "omnigent.last_context_window",
        CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY,
    }
)


@dataclass(frozen=True)
class CreatedSession:
    """
    Result of atomic session and agent creation.

    :param conversation: Newly created session/conversation row.
    :param agent: Newly created session-scoped agent row with
        ``session_id`` pointing at ``conversation.id``.
    """

    conversation: Conversation
    agent: Agent


@dataclass(frozen=True)
class SessionConnectivity:
    """
    The minimal session fields the sidebar's online-dot needs.

    Returned by :meth:`ConversationStore.get_session_connectivity` so
    the ``/health`` batch path can decide reachability without an N+1
    fan-out of :meth:`get_conversation` (each of which also fetches
    labels). One row per existing conversation.

    :param runner_id: Runner the session is pinned to, or ``None``
        when no runner has claimed it yet (in-process executor),
        e.g. ``"runner_token_abc123"``.
    :param host_id: Host the session is bound to, or ``None`` for
        CLI-launched / runner-only sessions, e.g. ``"host_abc123"``.
    :param needs_workspace: ``True`` when this is an unbound fork of a
        session that had a working directory (the
        ``omnigent.fork.source_id`` label is set). Forces the online
        dot off while ``runner_id``/``host_id`` are still ``None`` so
        the UI prompts for a host + directory before the clone can run,
        rather than treating it as an in-process session.
    """

    runner_id: str | None
    host_id: str | None
    needs_workspace: bool


class ConversationNotFoundError(Exception):
    """
    Raised when a required conversation row is missing.

    Store methods use this when absence is not a benign
    no-op and the route layer must return a typed 404.
    """


class NameAlreadyExistsError(Exception):
    """
    Raised by ``create_conversation`` when the requested
    ``(parent_conversation_id, title)`` pair already exists.

    Phase 4: the conversations table has a partial unique index
    that enforces sub-agent name uniqueness within a parent.
    SqlAlchemy's ``IntegrityError`` is translated to this exception
    so callers (the ``sys_session_send`` and ``sys_session_send``
    builtins) can surface a clean ``name_already_exists`` tool
    error to the LLM.
    """


class ConversationStore(ABC):
    """
    Abstract base for conversation persistence.

    Manages conversations and their items: creation, lookup,
    paginated listing, appending items, full-text search,
    updates, and deletion.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the conversation store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///conversations.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create_conversation(
        self,
        kind: str = "default",
        title: str | None = None,
        parent_conversation_id: str | None = None,
        agent_id: str | None = None,
        runner_id: str | None = None,
        sub_agent_name: str | None = None,
        host_id: str | None = None,
        workspace: str | None = None,
        git_branch: str | None = None,
        terminal_launch_args: list[str] | None = None,
    ) -> Conversation:
        """
        Create a new conversation. Generates a unique
        conversation_id.

        ``root_conversation_id`` is set automatically: for
        top-level conversations (no ``parent_conversation_id``)
        it equals the new ``id``; for child conversations it is
        inherited from the parent's ``root_conversation_id``
        (which itself ultimately resolves to the top-level
        conversation in the spawn tree).

        :param kind: Conversation type. ``"default"`` for
            user-initiated, ``"sub_agent"`` for sub-agent
            execution conversations.
        :param title: Optional title. Phase 4 named sub-agents
            store ``"<type>:<name>"`` so the partial unique index
            can enforce ``(parent_conversation_id, title)``
            uniqueness within a parent.
        :param parent_conversation_id: Phase 4 — for child
            sub-agent conversations, the owning parent's id.
            ``None`` for top-level conversations.
        :param agent_id: Agent to bind at creation time, e.g.
            ``"ag_abc123"``. ``None`` only for legacy rows or
            callers that cannot bind a conversation.
        :param runner_id: Optional runner binding to persist at
            creation time, e.g. ``"runner_abc123"``. Used when
            creating child sub-agent conversations so they inherit
            the parent session's current runner affinity.
        :param sub_agent_name: For sub-agent sessions, the
            sub-agent type name within the parent's spec tree,
            e.g. ``"summarizer"``. ``None`` for top-level.
        :param host_id: Host that should launch the runner for
            this session, e.g. ``"host_a1b2c3d4..."``. ``None``
            for CLI-initiated sessions.
        :param workspace: Absolute path on disk where the runner
            should start, e.g. ``"/Users/corey/universe/src/foo"``.
            Required when ``host_id`` is set (a DB check constraint
            enforces this); optional otherwise. The caller passes
            the canonicalized realpath returned by ``host.stat``;
            this method does no path expansion. When a git worktree
            was created, this is the worktree directory path.
        :param git_branch: Git branch checked out in the session's
            worktree, e.g. ``"feature/login"``. Set only when the
            session was created with a server-created worktree;
            ``None`` otherwise. See designs/SESSION_GIT_WORKTREE.md.
        :param terminal_launch_args: Optional pass-through CLI args
            for a native terminal wrapper (claude / codex), e.g.
            ``["--dangerously-skip-permissions"]``. ``None`` leaves
            the column NULL; a list (including ``[]``) is persisted
            so the runner applies it when it auto-launches the
            terminal.
        :returns: The newly created :class:`Conversation`.
        :raises NameAlreadyExistsError: If
            ``parent_conversation_id`` is not ``None`` and a
            sibling with the same ``title`` already exists
            (Phase 4 partial unique index violation).
        :raises ConversationNotFoundError: If
            ``parent_conversation_id`` is set but the parent
            row does not exist (root id can't be inherited).
        """
        ...

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return the conversation, or ``None`` if it does not exist.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The :class:`Conversation` if found, otherwise
            ``None``.
        """
        ...

    @abstractmethod
    def get_runner_ids(self, conversation_ids: list[str]) -> dict[str, str | None]:
        """
        Return ``conversation_id -> runner_id`` for a batch of sessions.

        Bulk variant for the sidebar runner-online dot path. Missing
        ids are omitted; ids without a bound runner map to ``None``.
        """
        ...

    @abstractmethod
    def get_session_connectivity(
        self, conversation_ids: list[str]
    ) -> dict[str, SessionConnectivity]:
        """
        Return connectivity fields for a batch of sessions in one query.

        Powers the sidebar's online-dot batch check (``GET /health``)
        without the N+1 fan-out of calling :meth:`get_conversation`
        per id (each of which also issues a second labels query). One
        ``SELECT`` over the conversations table plus one over the
        connectivity-label rows (the fork-source marker). Missing ids
        are omitted from the result.

        :param conversation_ids: Session/conversation IDs to look up,
            e.g. ``["conv_abc123", "conv_def456"]``. Duplicates are
            tolerated.
        :returns: Mapping ``conversation_id -> SessionConnectivity``.
            Ids with no conversation row are absent (callers treat a
            missing id as reachable, mirroring the single-row path).
        """
        ...

    @abstractmethod
    def get_conversations(self, conversation_ids: list[str]) -> dict[str, Conversation]:
        """
        Fetch a batch of conversations by id in a single round-trip.

        Bulk variant of :meth:`get_conversation` for callers that hold
        a known id set and would otherwise fan out one read per id —
        e.g. the ``WS /v1/sessions/updates`` stream rescanning its
        watch-set every interval. Labels are batched too, so the whole
        call is a small constant number of queries regardless of the id
        count.

        :param conversation_ids: Conversation ids to fetch,
            e.g. ``["conv_abc123", "conv_def456"]``. Duplicates are
            tolerated. Empty input returns an empty map without
            touching the database.
        :returns: Mapping ``{conversation_id: Conversation}``. Ids that
            don't resolve to a row are omitted (the caller decides
            whether a missing id is an error), so the result may be
            smaller than the input.
        """
        ...

    @abstractmethod
    def list_child_conversation_ids_by_parent(
        self,
        parent_conversation_ids: list[str],
    ) -> dict[str, list[str]]:
        """
        Return direct sub-agent child ids grouped by parent conversation.

        Batched counterpart to calling :meth:`list_conversations` with
        ``kind="sub_agent"`` and one ``parent_conversation_id`` at a
        time. Callers use this when they only need child identity (for
        example, rolling live child status into parent session rows) and
        should not pay for full child entities or one query per parent.

        :param parent_conversation_ids: Parent conversation ids to
            inspect, e.g. ``["conv_parent1", "conv_parent2"]``.
            Duplicates are tolerated.
        :returns: Mapping from every unique input parent id to the
            matching direct child ids. Parents with no direct sub-agent
            children, or ids that do not exist, map to an empty list.
        """
        ...

    @abstractmethod
    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        """
        Return items in a conversation with cursor-based pagination.

        ``order`` controls the sort direction on ``position``
        (``"asc"`` = chronological, ``"desc"`` = reverse).

        Both ``after`` and ``before`` can be used together to
        select a window. Used by the agent loop
        (``after=last_seen``) to poll for steering items.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return.
        :param after: Cursor item ID; only return items after
            this item in sort order, e.g. ``"msg_xyz789"``.
        :param before: Cursor item ID; only return items before
            this item in sort order.
        :param order: Sort direction, ``"asc"`` or ``"desc"``.
        :param type: Optional item type filter. When provided, only items
            with this type are returned, e.g. ``"compaction"``. ``None``
            means return all types.
        :returns: A :class:`PagedList` of
            :class:`ConversationItem` objects.
        """
        ...

    @abstractmethod
    def list_latest_message_items_for_conversations(
        self,
        conversation_ids: list[str],
        per_conversation_limit: int = 10,
    ) -> dict[str, list[ConversationItem]]:
        """
        Return newest message items for multiple conversations.

        This is the batched counterpart to calling
        ``list_items(conversation_id, type="message", order="desc")`` once
        per conversation. It preserves newest-first order within each
        conversation and returns at most ``per_conversation_limit`` items per
        conversation.

        :param conversation_ids: Conversation ids to fetch messages for,
            e.g. ``["conv_child1", "conv_child2"]``. Duplicates are tolerated.
        :param per_conversation_limit: Maximum number of message items to
            return per conversation, e.g. ``10``.
        :returns: Mapping ``{conversation_id: [ConversationItem, ...]}``.
            Input ids with no matching messages map to an empty list.
        """
        ...

    @abstractmethod
    def append(
        self,
        conversation_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        """
        Append items to a conversation. Assigns a globally unique
        ID and timestamp to each item.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param items: List of :class:`NewConversationItem` objects
            to persist.
        :returns: The persisted :class:`ConversationItem` list
            with store-assigned IDs and timestamps.
        """
        ...

    @abstractmethod
    def list_conversations(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        kind: str | None = "default",
        parent_conversation_id: str | None = None,
        root_conversation_id: str | None = None,
        agent_id: str | None = None,
        agent_name: str | None = None,
        has_agent_id: bool | None = None,
        order: str = "desc",
        sort_by: str = "created_at",
        search_query: str | None = None,
        accessible_by: str | None = None,
        include_archived: bool = False,
    ) -> PagedList[Conversation]:
        """
        List conversations with cursor-based pagination.

        ``order`` controls the sort direction on the column
        selected by ``sort_by`` (``"desc"`` = newest-first,
        ``"asc"`` = oldest-first).

        :param limit: Maximum number of conversations to return.
        :param after: Cursor conversation ID; return conversations
            appearing after this one in sort order,
            e.g. ``"conv_abc123"``.
        :param before: Cursor conversation ID; return conversations
            appearing before this one in sort order.
        :param kind: Filter to conversations of this kind. Exact
            match. ``"default"`` returns only user-initiated.
            ``"sub_agent"`` returns only sub-agent conversations.
            ``None`` disables the filter and returns all.
        :param parent_conversation_id: Phase 4 — when set, only
            return conversations whose
            ``parent_conversation_id == parent_conversation_id``
            (named sub-agents under the given parent). When
            ``None`` (default), the filter is disabled and all
            parent pointers are accepted. Powers the
            ``sys_session_list`` builtin and the ambient-hint
            injection.
        :param root_conversation_id: When set, only return
            conversations whose
            ``root_conversation_id == root_conversation_id``
            (every conversation in the same spawn tree). Powers
            the tree-scoped guard for ``sys_session_get_history`` /
            ``sys_session_close`` so any agent in a tree can
            address any other by ``conversation_id``. ``None``
            disables the filter.
        :param agent_id: When set, only return conversations
            that have at least one task whose ``agent_id``
            matches. Implementation joins through the ``tasks``
            table on the existing indexed FK. Ordering still
            follows ``sort_by`` + ``order`` on the
            *conversation* columns (``created_at`` /
            ``updated_at``); the agent_id filter does NOT
            re-sort by task timestamps. This matters for
            ``--continue``: "most recent" means the
            conversation whose own ``updated_at`` is newest
            (bumped on every item append), which lines up
            with what users expect from "the conversation I
            most recently *did anything in*". Powers the
            Omnigent mode ``--continue`` flag (resume the
            most-recent conversation for the agent that
            *this YAML* registers as) — see
            ``designs/RUN_OMNIGENT_SESSION_RESUMPTION.md``. ``None``
            disables the filter.
        :param agent_name: When set, only return conversations
            whose bound ``conversations.agent_id`` points at an
            agent row with this name. This is intentionally name-
            based so session-scoped agents created by multipart
            ``POST /v1/sessions`` remain resumable without sharing
            a template ``agent_id``. ``None`` disables the filter.
        :param has_agent_id: When ``True``, only return
            conversations whose ``agent_id`` column is not
            ``None`` — i.e. sessions created via
            ``POST /v1/sessions``. When ``None`` (default), the
            filter is disabled. Powers the ``GET /v1/sessions``
            list endpoint.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param sort_by: Column to sort on, ``"created_at"`` or
            ``"updated_at"``.
        :param search_query: Case-insensitive substring filter on
            the conversation title OR conversation item content
            (``search_text``). ``None`` or empty string disables
            the filter. A conversation matches if its title
            contains the query OR any of its items' search text
            does. Powers the sidebar's session search on
            ``GET /v1/sessions?search_query=...``.
        :param accessible_by: When set, filter to sessions the
            user has access to via ``session_permissions``. Uses
            a UNION subquery: sessions the user has a direct
            grant on, plus sessions with a ``"__public__"`` grant.
            ``None`` disables the filter (returns all sessions).
        :param include_archived: When ``False`` (default), archived
            conversations are excluded. When ``True``, archived and
            non-archived conversations are both returned (the caller
            groups them). Powers the sidebar's "Show archived" toggle.
        :returns: A :class:`PagedList` of :class:`Conversation`
            objects.
        """
        ...

    @abstractmethod
    def search(
        self,
        query: str,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[ConversationItem]:
        """
        Full-text search over conversation items.

        Returns items whose search_text matches the query,
        optionally scoped to a single conversation. Results are
        ranked by relevance.

        :param query: The search query string,
            e.g. ``"deployment error"``.
        :param conversation_id: Optional conversation to scope
            the search to, e.g. ``"conv_abc123"``.
        :param limit: Maximum number of results to return.
        :returns: A list of matching :class:`ConversationItem`
            objects ranked by relevance.
        """
        ...

    @abstractmethod
    def update_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        reasoning_effort: str | None = None,
        _unset_reasoning_effort: bool = False,
        model_override: str | None = None,
        _unset_model_override: bool = False,
        cost_control_mode_override: str | None = None,
        _unset_cost_control_mode_override: bool = False,
        harness_override: str | None = None,
        terminal_launch_args: list[str] | None = None,
        archived: bool | None = None,
    ) -> Conversation | None:
        """
        Update mutable fields on a conversation.

        For ``reasoning_effort``, ``model_override``, and
        ``cost_control_mode_override``, ``None`` means "leave
        unchanged". To explicitly clear them back to ``None``, pass
        the matching ``_unset_*`` flag.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param title: New title for the conversation, or ``None``
            to leave unchanged.
        :param reasoning_effort: Per-session reasoning effort hint,
            e.g. ``"high"``. ``None`` leaves unchanged.
        :param _unset_reasoning_effort: When ``True``, set
            ``reasoning_effort`` to ``None`` regardless of the
            ``reasoning_effort`` param value.
        :param model_override: Per-session LLM model override,
            e.g. ``"claude-opus-4-7"``. ``None`` leaves unchanged.
        :param _unset_model_override: When ``True``, set
            ``model_override`` to ``None`` regardless of the
            ``model_override`` param value.
        :param cost_control_mode_override: Per-session cost-control
            switch, ``"on"`` or ``"off"``. ``None`` leaves unchanged.
        :param _unset_cost_control_mode_override: When ``True``, set
            ``cost_control_mode_override`` to ``None`` regardless of
            the ``cost_control_mode_override`` param value.
        :param harness_override: Per-session brain-harness override,
            e.g. ``"pi"``. ``None`` leaves unchanged. No ``_unset``
            variant — the override is set once at session create and
            immutable thereafter (the harness process is spawned on
            the first turn).
        :param terminal_launch_args: Per-session native-terminal
            pass-through args, e.g.
            ``["--dangerously-skip-permissions"]``. ``None`` leaves
            unchanged; a list (including ``[]``) replaces the stored
            value wholesale (resume is last-write-wins, never an
            append).
        :param archived: New archived state. ``True`` archives
            (hides from the default listing), ``False`` unarchives,
            ``None`` leaves unchanged.
        :returns: The updated :class:`Conversation`, or ``None``
            if the conversation does not exist.
        """
        ...

    @abstractmethod
    def set_labels(
        self,
        conversation_id: str,
        updates: dict[str, str],
        updated_at: int | None = None,
    ) -> None:
        """
        Upsert guardrails labels on a conversation.

        Atomic batched UPSERT: either every key in *updates*
        lands, or none of them do (matches POLICIES.md §6.3).
        Overwrites existing rows for the same keys;
        non-mentioned keys are left untouched. The caller is
        responsible for schema validation (``values`` /
        ``monotonic``) — the store persists whatever it's
        given (see POLICIES.md §9.2 + §13 where that
        validation lives in ``PolicyEngine.apply_label_writes``).

        Callers that need "insert only if missing" semantics
        (initial-value seeding — POLICIES.md §10) should check
        ``conversation.labels`` first and filter the updates
        to keys not already present; this method always
        overwrites.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``. If the conversation does
            not exist, behavior is implementation-defined
            (typically raises via the FK constraint).
        :param updates: Mapping from label key to new value.
            Both keys and values must already be strings
            (string coercion happens upstream at spec load).
            Example: ``{"integrity": "0", "sensitivity": "confidential"}``.
            Empty dict is a no-op.
        :param updated_at: Unix epoch seconds to stamp on the
            affected rows. ``None`` (default) → the store
            records the current time. The caller-supplied form
            is there for the policy engine to pass its
            evaluation timestamp (POLICIES.md §6.3), keeping
            audit trails aligned with the enforcement site
            rather than wall-clock drift between evaluate()
            and the actual DB write.
        """
        ...

    @abstractmethod
    def set_session_state(
        self,
        conversation_id: str,
        state: dict[str, Any],
    ) -> None:
        """
        Persist the full session-state snapshot for a conversation.

        Overwrites the existing ``session_state`` JSON column with
        the serialized *state* dict. Called by
        :meth:`PolicyEngine.apply_state_updates` after applying
        structured :class:`StateUpdate` operations to the hot
        cache.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param state: The complete session-state dict to persist.
            Serialized as JSON. Empty dict is stored as ``"{}"``.
        """
        ...

    @abstractmethod
    def set_session_usage(
        self,
        conversation_id: str,
        usage: dict[str, Any],
    ) -> None:
        """
        Persist the cumulative LLM token usage for a conversation.

        Overwrites the existing ``session_usage`` JSON column with
        the serialized *usage* dict. Called by
        :meth:`PolicyEngine.record_usage` after incrementing the
        in-memory counters.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param usage: The complete usage dict to persist, e.g.
            ``{"input_tokens": 1500, "output_tokens": 350,
            "total_tokens": 1850}``. May carry a nested ``"by_model"``
            sub-dict (per-model token/cost buckets), hence ``Any``.
        """
        ...

    @abstractmethod
    def add_daily_cost(self, user_id: str, day_utc: str, delta_usd: float) -> None:
        """
        Atomically add *delta_usd* to a user's spend for one UTC day.

        UPSERTs the ``user_daily_cost`` row keyed by
        ``(user_id, day_utc)``: inserts ``delta_usd`` when no row
        exists, otherwise increments the existing ``cost_usd`` by
        ``delta_usd`` in a single atomic statement (no
        read-modify-write, so concurrent turns and replicas don't lose
        updates). Powers cost-aware policies' per-user daily budget
        reads. Callers gate this on the session running under at least
        one policy, so the table is untouched when no policy exists.

        :param user_id: The user the cost is attributed to (the session
            creator), e.g. ``"alice@example.com"``.
        :param day_utc: UTC calendar day as an ISO date string
            ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        :param delta_usd: USD amount to add. A no-op when ``<= 0`` so a
            zero-cost turn (e.g. pricing unavailable) never creates a
            row.
        """
        ...

    @abstractmethod
    def get_daily_cost(self, user_id: str, day_utc: str) -> float:
        """
        Return a user's accumulated LLM spend for one UTC day.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param day_utc: UTC calendar day as an ISO date string
            ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        :returns: The accumulated ``cost_usd`` for that
            ``(user_id, day_utc)``, or ``0.0`` when no row exists.
        """
        ...

    @abstractmethod
    def get_daily_cost_state(self, user_id: str, day_utc: str) -> dict[str, float]:
        """
        Return a user's daily cost rollup state for one UTC day.

        Reads both the accumulated spend and the highest soft
        checkpoint already approved that day, in one lookup — what the
        per-user daily cost-budget policy needs.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param day_utc: UTC calendar day as an ISO date string
            ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        :returns: ``{"cost_usd": <float>, "ask_approved_usd": <float>}``;
            both ``0.0`` when no row exists for ``(user_id, day_utc)``.
        """
        ...

    @abstractmethod
    def set_daily_ask_approved(self, user_id: str, day_utc: str, ask_approved_usd: float) -> None:
        """
        Record the highest approved soft checkpoint for a user+day.

        Sets ``ask_approved_usd`` without altering ``cost_usd`` (insert
        with ``cost_usd = 0`` when no row exists, else update only the
        approval field). Called when a per-user daily cost-budget ASK is
        approved, so an approved checkpoint does not re-prompt that user
        again the same day — including from other sessions.

        :param user_id: The user the approval is for, e.g.
            ``"alice@example.com"``.
        :param day_utc: UTC calendar day as ``"YYYY-MM-DD"``, e.g.
            ``"2026-06-05"``.
        :param ask_approved_usd: The crossed checkpoint value (USD) the
            user approved continuing past, e.g. ``0.05``.
        """
        ...

    @abstractmethod
    def get_session_owner(self, conversation_id: str) -> str | None:
        """
        Return the user id that owns a session (its creator).

        The owner is the highest-privilege grantee in
        ``session_permissions`` for this conversation — the
        ``LEVEL_OWNER`` grant the creator receives at session
        creation (the ``"__public__"`` read sentinel and any
        read/edit grants are lower-level, so they are never
        returned ahead of it). Used to attribute a session's LLM
        spend to a single user for per-user daily cost rollups.

        :param conversation_id: The session to look up, e.g.
            ``"conv_abc123"``.
        :returns: The owner's user id, e.g. ``"alice@example.com"``,
            or ``None`` when the session has no permission grants
            (e.g. single-user mode, where access is not tracked).
        """
        ...

    @abstractmethod
    def set_runner_id(self, conversation_id: str, runner_id: str) -> bool:
        """
        Atomically pin ``conversations.runner_id`` only if currently NULL.

        Implemented as ``UPDATE ... WHERE id = :id AND runner_id IS NULL``
        so concurrent binders race safely: exactly one transitions the
        row from NULL → ``runner_id`` and gets ``True``; others (or an
        already-bound / missing row) get ``False``. Closes the TOCTOU on
        host-launch binding (see ``resolve_host_launch``).

        :param conversation_id: Conversation to pin, e.g.
            ``"conv_abc123"``.
        :param runner_id: Runner id to bind to, e.g.
            ``"runner_abc123"``.
        :returns: ``True`` if this call won the bind (NULL → runner_id);
            ``False`` if already bound or the row doesn't exist.
        """
        ...

    @abstractmethod
    def replace_runner_id(self, conversation_id: str, runner_id: str) -> Conversation:
        """
        Replace ``conversations.runner_id`` for a conversation.

        Atomic last-write-wins write. Public session binding routes
        validate session-scoped agent ownership before calling this
        method; internal sub-agent code also uses it to keep child
        conversations on the parent's current runner.

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param runner_id: Runner identifier to bind to,
            e.g. ``"runner_abc123"``. Online-ness is validated
            by the route before calling the store.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def clear_runner_id(self, conversation_id: str) -> Conversation:
        """
        Null out ``conversations.runner_id``.

        Counterpart to :meth:`replace_runner_id` for the 1:1
        session↔runner invariant — /clear and /switch unbind the old
        session before binding the runner to the new one.

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def clear_host_binding(self, conversation_id: str) -> Conversation:
        """
        Revert a session to fully unbound: NULL ``host_id``,
        ``workspace``, ``git_branch``, and ``runner_id`` together.

        Used to undo a failed per-session bind (``POST
        /v1/hosts/{id}/runners``) after the runner was atomically
        bound and the binding fields persisted, but the launch
        failed and any worktree was rolled back. Clearing all four
        fields in one transaction keeps the row consistent with the
        host's actual state (no runner, no worktree) and, unlike
        :meth:`set_host_id` (which treats ``None`` as "leave
        untouched" and so cannot clear ``git_branch``), lets a later
        rebind that omits a worktree start from a clean slate rather
        than inheriting a stale branch. Nulling ``host_id`` and
        ``workspace`` together never violates
        ``ck_conversations_workspace_required_for_host`` (workspace
        is only required while ``host_id`` is set).

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def list_conversations_by_host_id(
        self,
        host_id: str,
    ) -> list[Conversation]:
        """
        Return all conversations with the given ``host_id``.

        Used by reconnect reconciliation to find sessions that
        need their runner relaunched on a specific host.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :returns: List of :class:`Conversation` entities with
            ``host_id`` matching the given value.
        """
        ...

    @abstractmethod
    def list_conversations_by_runner_id(
        self,
        runner_id: str,
    ) -> list[Conversation]:
        """
        Return all conversations bound to the given ``runner_id``.

        Used by the runner tunnel's connect/disconnect callbacks to
        find the sessions pinned to a specific runner. Implementations
        must be read-after-write consistent with ``set_runner_id`` /
        ``replace_runner_id``: the connect callback fires seconds after
        a session is bound, so an eventually-consistent source (e.g. a
        search index) misses just-created sessions and the runner's
        claude-native terminal bootstrap silently never fires.

        :param runner_id: Runner identifier, e.g.
            ``"runner_token_a1b2c3d4..."``.
        :returns: List of :class:`Conversation` entities with
            ``runner_id`` matching the given value.
        """
        ...

    @abstractmethod
    def set_host_id(
        self,
        conversation_id: str,
        host_id: str,
        workspace: str | None = None,
        git_branch: str | None = None,
    ) -> Conversation:
        """
        Set the host that launched (or should launch) the runner.

        Used when the server asks a host to spawn a runner for
        an existing session. Last-write-wins semantics (like
        :meth:`replace_runner_id`).

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param workspace: Optional canonical absolute workspace
            path to set together with ``host_id``, e.g.
            ``"/Users/corey/projects/myapp"``. Required when the
            existing row has ``workspace=NULL`` (DB constraint
            ``ck_conversations_workspace_required_for_host``).
            ``None`` leaves the workspace untouched.
        :param git_branch: Optional git branch checked out in a
            server-created worktree, e.g. ``"feature/login"``. Set
            when binding an existing session to a freshly created
            worktree (the fork resume path). ``None`` leaves it
            untouched.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        """
        ...

    @abstractmethod
    def set_external_session_id(
        self,
        conversation_id: str,
        value: str,
    ) -> Conversation:
        """
        Set the runtime-native session id this conversation wraps.

        Captured by the wrapper bridge from the underlying runtime
        (Claude Code's session uuid today; Codex / Pi tomorrow) and
        recorded once per conversation so ``--resume`` can recover
        the external session's prior transcript on a fresh runner.

        Idempotent: setting the same value as the existing one is a
        no-op (the wrapper bridge may observe the value across
        multiple hook events). Setting a different value when the
        field is already populated raises ``ValueError`` —
        wrappers should observe exactly one runtime-native session
        id per conversation, and a divergent write signals a bug
        worth surfacing loudly rather than silently overwriting.

        :param conversation_id: Conversation to update, e.g.
            ``"conv_abc123"``.
        :param value: Runtime-native session id captured by the
            wrapper bridge, e.g. a Claude Code session uuid
            ``"a1b2c3d4-..."``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            with ``conversation_id`` exists.
        :raises ValueError: If
            ``conversation.external_session_id`` is already set
            to a different value.
        """
        ...

    @abstractmethod
    def create_session_with_agent(
        self,
        *,
        agent_id: str,
        agent_name: str,
        agent_bundle_location: str,
        agent_description: str | None,
        title: str | None = None,
        labels: dict[str, str] | None = None,
        reasoning_effort: str | None = None,
        workspace: str | None = None,
        terminal_launch_args: list[str] | None = None,
        parent_conversation_id: str | None = None,
        runner_id: str | None = None,
    ) -> CreatedSession:
        """
        Atomically create a session and its session-scoped agent.

        The conversation row and agent row are written in one
        database transaction. If either insert or any label write
        fails, none of the database rows are committed.

        :param agent_id: Pre-generated agent id, e.g.
            ``"ag_abc123"``.
        :param agent_name: Human-readable agent name from the
            uploaded spec, e.g. ``"code-assistant"``.
        :param agent_bundle_location: Artifact-store key for the
            uploaded bundle, e.g. ``"ag_abc123/a1b2c3d4"``.
        :param agent_description: Optional spec description.
            ``None`` when the spec omits it.
        :param title: Optional session title, e.g.
            ``"debugging auth flow"``.
        :param labels: Optional initial guardrails labels,
            e.g. ``{"env": "test"}``. ``None`` writes no labels.
        :param reasoning_effort: Optional per-session
            reasoning-effort hint, e.g. ``"high"``. ``None``
            means use the agent default.
        :param workspace: Optional starting cwd to record on the
            session, e.g. ``"/Users/corey/projects/myapp"``.
            ``None`` leaves the column NULL.
        :param terminal_launch_args: Optional pass-through CLI args
            for a native terminal wrapper (claude / codex), e.g.
            ``["--dangerously-skip-permissions"]``. ``None`` leaves
            the column NULL.
        :param parent_conversation_id: Optional parent conversation
            id, e.g. ``"conv_parent1"``. When set, the new session
            is a sub-agent child of that conversation
            (``kind="sub_agent"``) and inherits its spawn-tree root.
            ``None`` creates a top-level session.
        :param runner_id: Optional runner binding to persist at
            creation time, e.g. ``"runner_abc123"``. Child sessions
            inherit the parent's binding through this field.
        :returns: The committed conversation and agent entities.
        :raises ConversationNotFoundError: If
            ``parent_conversation_id`` is set but no such
            conversation exists.
        :raises Exception: Backend errors propagate after rollback.
        """
        ...

    @abstractmethod
    def fork_conversation(
        self,
        source_conversation_id: str,
        *,
        title: str | None = None,
        agent_id: str | None = None,
        cloned_agent_name: str | None = None,
        cloned_agent_bundle_location: str | None = None,
        cloned_agent_description: str | None = None,
        copy_model_settings: bool = True,
        carry_history_into_native: bool = False,
        resume_source_native_session: bool = True,
        presentation_labels: dict[str, str] | None = None,
        up_to_response_id: str | None = None,
    ) -> Conversation:
        """
        Deep-copy a conversation and its items into a new conversation.

        Creates a new top-level conversation
        (``kind="default"``, ``parent_conversation_id=None``)
        with the source's ``reasoning_effort``, then copies every
        item (with fresh IDs) preserving position order,
        ``response_id``, ``type``, ``status``, and ``data``. FTS
        records are inserted for each copied item. The entire
        operation runs in a single transaction for atomicity.

        :param source_conversation_id: ID of the conversation to
            fork, e.g. ``"conv_abc123"``.
        :param title: Title for the new conversation. When
            ``None``, defaults to ``"Fork of <source_title>"``
            (or ``"Fork of <source_id>"`` when the source has no
            title).
        :param agent_id: Agent ID to bind the fork to. When ``None``,
            the fork inherits the source's ``agent_id``. With
            ``cloned_agent_bundle_location`` set, a fresh agent row is
            created with this id; otherwise it must name an existing
            agent, whose ``session_id`` is repointed at the fork.
        :param cloned_agent_name: Name for the cloned agent row.
            Required when ``cloned_agent_bundle_location`` is set.
        :param cloned_agent_bundle_location: When set, clone this
            bundle into a new session-scoped agent row created
            atomically in the fork transaction, so a fork failure rolls
            it back instead of orphaning a ``session_id IS NULL``
            built-in. ``None`` keeps the legacy bind-existing behavior.
        :param cloned_agent_description: Optional description for the
            cloned agent row. Ignored unless
            ``cloned_agent_bundle_location`` is set.
        :param copy_model_settings: When ``True`` (default), copy the
            source's ``model_override`` / ``reasoning_effort``. When
            ``False``, both are left ``None`` so the fork falls back to
            the bound agent's defaults — used when the fork switches to
            an agent in a different provider family, where the source's
            model id is meaningless (a model is provider-bound).
        :param carry_history_into_native: When ``True``, stamp
            :data:`FORK_CARRY_HISTORY_LABEL_KEY` on the fork so a native
            target harness rebuilds its transcript (clone the source's
            native transcript, or build from the copied Omnigent items) instead
            of starting fresh. Set by the route only for native targets whose
            harness can replay fork history.
        :param resume_source_native_session: When ``True`` (default), a
            full fork of a source with a native session stamps
            :data:`FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY` so the runner
            clones the source's local native transcript. ``False`` when the
            fork switches to an agent in a DIFFERENT provider family: the
            source's native transcript is the wrong format for the target
            harness, so the directive is skipped and the runner builds the
            native transcript from the copied Omnigent items instead.
        :param presentation_labels: When not ``None``, replace the source's
            harness-presentation labels (``omnigent.ui`` /
            ``omnigent.wrapper``) on the clone with these. Used when the
            fork switches agents so the clone's UI mode matches the TARGET
            harness: a native target supplies ``{ui: terminal, wrapper:
            ...}``; an SDK target supplies ``{}`` (drop them → chat mode).
            ``None`` (default, same-agent fork) keeps the copied labels.
        :param up_to_response_id: When set, copy only the items up to and
            including the last item of this response (by position), e.g.
            ``"resp_abc123"`` — a "fork from this response" truncation.
            A truncated fork drops the source's external-session fork
            directive so a native target rebuilds its transcript from the
            truncated items instead of resuming the full source
            transcript; when the response is the source's last one, the
            copy is equivalent to a full fork and the directive is kept.
            ``None`` (default) copies the full history.
        :returns: The newly created :class:`Conversation`.
        :raises LookupError: If no conversation with
            *source_conversation_id* exists.
        :raises ValueError: If *up_to_response_id* is set but no item in
            the source conversation has that ``response_id``.
        """
        ...

    @abstractmethod
    def switch_conversation_agent(
        self,
        conversation_id: str,
        *,
        new_agent_id: str,
        new_agent_name: str,
        new_agent_bundle_location: str,
        new_agent_description: str | None,
        copy_model_settings: bool,
        carry_history_into_native: bool,
        presentation_labels: dict[str, str],
        previous_builtin_id: str | None,
    ) -> Conversation:
        """
        Rebind a session in place to a different (cloned) agent.

        Unlike :meth:`fork_conversation`, this mutates the SAME
        conversation row — the transcript, comments, files, host,
        and workspace are untouched; only the agent/harness changes.
        In one transaction it: deletes the session's current
        session-scoped agent (the unique ``session_id`` index forbids
        two agents on one session, so the old must go before the new
        binds), creates a new session-scoped agent from the supplied
        bundle, points ``agent_id`` at it, applies the model-settings
        and label deltas below, and clears ``external_session_id``
        (the old harness's native runtime state). The whole operation
        is atomic: any failure rolls back and the session stays on its
        current agent.

        :param conversation_id: Session to switch, e.g.
            ``"conv_abc123"``.
        :param new_agent_id: Pre-generated id for the new
            session-scoped agent, e.g. ``"ag_def456"``.
        :param new_agent_name: Name for the new agent row, e.g.
            ``"Codex (switch ag_def456)"``.
        :param new_agent_bundle_location: Artifact-store key of the
            target built-in's bundle to clone, e.g.
            ``"ag_builtin/abcd1234"``.
        :param new_agent_description: Optional description from the
            target's spec. ``None`` leaves the column NULL.
        :param copy_model_settings: When ``True``, keep the session's
            existing ``model_override`` / ``reasoning_effort`` (the
            switch stays in the same provider family). When ``False``,
            both are reset to ``None`` so the new agent's defaults
            apply (a cross-family switch — a model id is provider-bound).
        :param carry_history_into_native: When ``True``, stamp
            :data:`FORK_CARRY_HISTORY_LABEL_KEY` so a native target
            rebuilds its transcript from this session's own AP items on
            the next turn; when ``False``, that label is removed. Set by
            the route only when the target is native AND same-family.
        :param presentation_labels: Replace the session's
            ``omnigent.ui`` / ``omnigent.wrapper`` labels with these so
            the UI mode matches the TARGET harness (native →
            ``{ui: terminal, wrapper: ...}``; SDK → ``{}`` → chat mode).
        :param previous_builtin_id: Built-in agent id the session is
            switching away from, stamped as
            :data:`SWITCH_PREVIOUS_BUILTIN_LABEL_KEY` for a one-click
            "Switch back". ``None`` leaves it unset.
        :returns: The updated :class:`Conversation`.
        :raises LookupError: If no conversation with *conversation_id*
            exists.
        """
        ...

    @abstractmethod
    async def delete_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation and all its items.

        Async because it may need to cancel in-flight responses
        in the conversation first.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` if the conversation existed,
            ``False`` otherwise.
        """
        ...
