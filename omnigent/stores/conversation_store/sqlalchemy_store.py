"""SQLAlchemy-backed conversation store."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    asc,
    delete,
    desc,
    func,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.orm import QueryableAttribute, Session
from sqlalchemy.sql.selectable import Subquery

from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, WRAPPER_LABEL_KEY
from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import (
    SqlAgent,
    SqlConversation,
    SqlConversationItem,
    SqlConversationLabel,
    SqlUserDailyCost,
)
from omnigent.db.utils import (
    _supports_fts5,
    delete_fts_by_conversation,
    ensure_fts_table,
    extract_search_text,
    generate_conversation_id,
    generate_item_id,
    get_or_create_engine,
    insert_fts,
    make_managed_session_maker,
    now_epoch,
    strip_nul_bytes,
)
from omnigent.entities import (
    Conversation,
    ConversationItem,
    NewConversationItem,
    PagedList,
    UsageTotals,
    parse_item_data,
)
from omnigent.stores.conversation_store import (
    _INSTANCE_SCOPED_LABEL_KEYS,
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    FORK_SOURCE_LABEL_KEY,
    PROJECT_LABEL_KEY,
    SWITCH_PREVIOUS_BUILTIN_LABEL_KEY,
    ConversationNotFoundError,
    ConversationStore,
    CreatedSession,
    SessionConnectivity,
)


def _to_conversation(
    row: SqlConversation,
    labels: dict[str, str] | None = None,
) -> Conversation:
    """
    Convert a :class:`SqlConversation` ORM row to a
    :class:`Conversation` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :param labels: Pre-fetched guardrails labels for this
        conversation. ``None`` means "no label fetch was
        performed" (callers that don't need labels pass
        ``None`` rather than forcing a second query); this
        maps to an empty dict on the entity. Populated
        callers pass the JOINed ``{key: value}`` map.
    :returns: A :class:`Conversation` dataclass instance.
    """
    import json

    session_state: dict[str, Any] = {}
    if row.session_state:
        session_state = json.loads(row.session_state)
    session_usage: dict[str, Any] = {}
    if row.session_usage:
        session_usage = json.loads(row.session_usage)
    return Conversation(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        title=row.title,
        kind=row.kind,
        parent_conversation_id=row.parent_conversation_id,
        root_conversation_id=row.root_conversation_id,
        agent_id=row.agent_id,
        runner_id=row.runner_id,
        host_id=row.host_id,
        labels=labels if labels is not None else {},
        session_state=session_state,
        session_usage=session_usage,
        reasoning_effort=row.reasoning_effort,
        model_override=row.model_override,
        cost_control_mode_override=row.cost_control_mode_override,
        harness_override=row.harness_override,
        sub_agent_name=row.sub_agent_name,
        external_session_id=row.external_session_id,
        # NULL → None; a stored JSON array (e.g. ``"[]"`` or
        # ``'["--foo"]'``) decodes back to a list. ``"[]"`` is a
        # non-empty, truthy string, so an explicitly-empty arg list
        # round-trips as ``[]`` and stays distinct from NULL/None.
        terminal_launch_args=(
            json.loads(row.terminal_launch_args) if row.terminal_launch_args is not None else None
        ),
        workspace=row.workspace,
        git_branch=row.git_branch,
        archived=row.archived,
    )


def _new_session_conversation_row(
    conversation_id: str,
    now: int,
    title: str | None,
    reasoning_effort: str | None,
    workspace: str | None = None,
    terminal_launch_args: list[str] | None = None,
    parent_conversation_id: str | None = None,
    root_conversation_id: str | None = None,
    runner_id: str | None = None,
) -> SqlConversation:
    """
    Build the conversation row for atomic session creation.

    :param conversation_id: New conversation id, e.g.
        ``"conv_abc123"``.
    :param now: Unix epoch seconds used for created/updated fields.
    :param title: Optional session title.
    :param reasoning_effort: Optional per-session reasoning-effort
        hint, e.g. ``"high"``. ``None`` means use the agent
        default.
    :param workspace: Optional starting cwd, e.g.
        ``"/Users/corey/projects/myapp"`` (recorded for CLI
        sessions whose runner is launched locally). ``None``
        leaves the column NULL.
    :param terminal_launch_args: Optional pass-through CLI args for a
        native terminal wrapper, e.g.
        ``["--dangerously-skip-permissions"]``. ``None`` leaves the
        column NULL; a list (including ``[]``) is JSON-encoded.
    :param parent_conversation_id: Optional parent conversation id,
        e.g. ``"conv_parent1"``. When set, the row is created as a
        sub-agent child (``kind="sub_agent"``); ``None`` creates a
        top-level row.
    :param root_conversation_id: Root of the spawn tree, e.g.
        ``"conv_root1"``. Required (resolved from the parent row)
        when ``parent_conversation_id`` is set; ``None`` for
        top-level rows, where the root mirrors the primary key.
    :param runner_id: Optional runner binding inherited from the
        parent session, e.g. ``"runner_abc123"``. ``None`` leaves
        the column NULL.
    :returns: Unsaved :class:`SqlConversation` row.
    """
    return SqlConversation(
        id=conversation_id,
        created_at=now,
        updated_at=now,
        title=title,
        kind="sub_agent" if parent_conversation_id else "default",
        parent_conversation_id=parent_conversation_id,
        # Top-level row: ``root_conversation_id`` mirrors the
        # primary key so tree-scoped lookups treat it as its own
        # root. Child rows inherit their parent's root.
        root_conversation_id=root_conversation_id or conversation_id,
        agent_id=None,
        runner_id=runner_id,
        reasoning_effort=reasoning_effort,
        terminal_launch_args=(
            json.dumps(terminal_launch_args) if terminal_launch_args is not None else None
        ),
        workspace=workspace,
    )


def _new_session_agent_row(
    *,
    agent_id: str,
    agent_name: str,
    agent_bundle_location: str,
    agent_description: str | None,
    conversation_id: str,
    now: int,
) -> SqlAgent:
    """
    Build the session-scoped agent row for atomic creation.

    :param agent_id: New agent id, e.g. ``"ag_abc123"``.
    :param agent_name: Agent name loaded from the uploaded spec.
    :param agent_bundle_location: Artifact-store key for the bundle.
    :param agent_description: Optional description from the spec.
    :param conversation_id: Owning conversation id.
    :param now: Unix epoch seconds used for the created field.
    :returns: Unsaved :class:`SqlAgent` row.
    """
    return SqlAgent(
        id=agent_id,
        created_at=now,
        name=agent_name,
        bundle_location=agent_bundle_location,
        version=1,
        description=agent_description,
        session_id=conversation_id,
    )


def _created_session_from_rows(
    conversation_row: SqlConversation,
    agent_row: SqlAgent,
    labels: dict[str, str] | None,
) -> CreatedSession:
    """
    Convert committed session creation rows to store entities.

    :param conversation_row: Inserted conversation row.
    :param agent_row: Inserted session-scoped agent row.
    :param labels: Labels written during creation, or ``None``.
    :returns: :class:`CreatedSession` with entity objects.
    """
    return CreatedSession(
        conversation=_to_conversation(
            conversation_row,
            labels if labels is not None else {},
        ),
        agent=sql_agent_to_entity(agent_row),
    )


def _upsert_labels(
    session: Session,
    conversation_id: str,
    updates: dict[str, str],
    updated_at: int,
) -> None:
    """
    Atomically UPSERT multiple labels on one conversation.

    Dialect-aware: SQLite and PostgreSQL both support
    ``INSERT ... ON CONFLICT ... DO UPDATE``, so we use
    their dedicated INSERT builders. Other dialects fall
    back to a SELECT-then-INSERT/UPDATE path, which is
    race-safe inside one transaction under SERIALIZABLE or
    (for SQLite) its default single-writer semantics.

    :param session: Active SQLAlchemy session (the atomic
        unit of work).
    :param conversation_id: Owning conversation ID.
    :param updates: Non-empty dict of label key → value.
    :param updated_at: Timestamp to write on every row
        touched by this call.
    """
    dialect = session.bind.dialect.name if session.bind is not None else ""
    rows = [
        {
            "conversation_id": conversation_id,
            "key": key,
            "value": value,
            "updated_at": updated_at,
        }
        for key, value in updates.items()
    ]
    if dialect in ("sqlite", "postgresql"):
        _dialect_upsert_labels(session, dialect, rows)
        return
    # Generic dialect fallback — SELECT-then-INSERT/UPDATE in
    # one transaction. Safe for the v1 "one active workflow
    # per conversation" invariant (POLICIES.md §10); the
    # SQLite / Postgres dialect-specific paths above give
    # true atomic UPSERT for the supported production dbs.
    for row in rows:
        existing = session.get(
            SqlConversationLabel,
            (row["conversation_id"], row["key"]),
        )
        if existing is None:
            session.add(SqlConversationLabel(**row))
        else:
            # mypy sees existing.{value,updated_at} as the
            # Mapped[...] descriptor types; at runtime these
            # are plain attributes that accept the target
            # Python type directly. SQLAlchemy's ORM handles
            # the coercion.
            existing.value = row["value"]  # type: ignore[assignment]
            existing.updated_at = row["updated_at"]  # type: ignore[assignment]


def _dialect_upsert_labels(
    session: Session,
    dialect: str,
    rows: list[dict[str, Any]],
) -> None:
    """
    Dialect-specific UPSERT path for SQLite / PostgreSQL.

    Extracted from ``_upsert_labels`` so the two branches
    (which use different ``insert`` builders producing
    incompatible type variances at the mypy level) each live
    in their own narrow scope. The outer function selects the
    branch; this one executes it.

    :param session: Active SQLAlchemy session.
    :param dialect: ``"sqlite"`` or ``"postgresql"`` (the
        outer function gates all other dialects onto the
        generic fallback path).
    :param rows: Pre-built row dicts to upsert.
    """
    # Typed as Any to sidestep the mypy variance issue between
    # the two dialect-specific ``Insert`` classes; the runtime
    # shape of both classes is identical for our use.
    stmt: Any
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(SqlConversationLabel).values(rows)
    else:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(SqlConversationLabel).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["conversation_id", "key"],
        set_={
            "value": stmt.excluded.value,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    session.execute(stmt)


def _fetch_labels(
    session: Session,
    conversation_id: str,
) -> dict[str, str]:
    """
    Load all guardrails labels for a conversation.

    Returns an empty dict when no labels have been written
    yet — a conversation that was created before its spec
    declared guardrails, or before any policy wrote a label.

    :param session: The active SQLAlchemy session.
    :param conversation_id: Unique conversation identifier,
        e.g. ``"conv_abc123"``.
    :returns: Mapping from label key to value (string-typed).
        Empty dict when no rows match.
    """
    rows = session.execute(
        select(SqlConversationLabel.key, SqlConversationLabel.value).where(
            SqlConversationLabel.conversation_id == conversation_id,
        )
    ).all()
    return dict(rows)


def _fetch_labels_bulk(
    session: Session,
    conversation_ids: list[str],
) -> dict[str, dict[str, str]]:
    """
    Load labels for many conversations in a single query.

    Used by ``list_conversations`` to avoid an N+1 fan-out.
    Empty input returns an empty map without touching the
    database.

    :param session: The active SQLAlchemy session.
    :param conversation_ids: Conversation IDs to fetch labels
        for, e.g. ``["conv_a", "conv_b"]``. Duplicates are
        tolerated but yield the same map entries.
    :returns: Mapping ``{conversation_id: {key: value}}``.
        Conversations with no label rows are absent from the
        outer map (callers should default to ``{}``).
    """
    if not conversation_ids:
        return {}
    rows = session.execute(
        select(
            SqlConversationLabel.conversation_id,
            SqlConversationLabel.key,
            SqlConversationLabel.value,
        ).where(SqlConversationLabel.conversation_id.in_(conversation_ids))
    ).all()
    out: dict[str, dict[str, str]] = {}
    for conv_id, key, value in rows:
        out.setdefault(conv_id, {})[key] = value
    return out


def _to_item(row: SqlConversationItem) -> ConversationItem:
    """
    Convert a :class:`SqlConversationItem` ORM row to a
    :class:`ConversationItem` entity.

    Deserializes the JSON ``data`` column and parses it into
    the appropriate typed data model.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`ConversationItem` Pydantic model.
    """
    return ConversationItem(
        id=row.id,
        type=row.type,
        status=row.status,
        response_id=row.response_id,
        created_at=row.created_at,
        data=parse_item_data(row.type, json.loads(row.data)),
        created_by=row.created_by,
    )


def _ranked_latest_message_item_ids(conversation_ids: list[str]) -> Subquery:
    """
    Build a ranked latest-message-id subquery for multiple conversations.

    :param conversation_ids: Conversation ids to fetch messages for,
        e.g. ``["conv_child1", "conv_child2"]``.
    :returns: SQLAlchemy subquery with ``item_id`` and per-conversation
        ``row_num`` columns, newest message first.
    """
    return (
        select(
            SqlConversationItem.id.label("item_id"),
            func.row_number()
            .over(
                partition_by=SqlConversationItem.conversation_id,
                order_by=desc(SqlConversationItem.position),
            )
            .label("row_num"),
        )
        .where(
            SqlConversationItem.conversation_id.in_(conversation_ids),
            SqlConversationItem.type == "message",
        )
        .subquery()
    )


class SqlAlchemyConversationStore(ConversationStore):
    """
    SQLAlchemy-backed implementation of :class:`ConversationStore`.

    Persists conversations and their items in a relational database
    via SQLAlchemy ORM. Also manages a full-text search (FTS) table
    for item content.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy conversation store.

        Creates or reuses a SQLAlchemy engine and session factory,
        and ensures the FTS virtual table exists.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///conversations.db"`` or
            ``"postgresql://user:pass@host/db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._supports_for_update = self._engine.dialect.name != "sqlite"
        # SQLite rowid is monotonically increasing absent deletions; it serves
        # as an insertion-ordered tiebreaker for timestamp ties. Note: without
        # the AUTOINCREMENT keyword, SQLite may reuse a rowid if the max-rowid
        # row is deleted — acceptable here since deletions won't cause
        # same-timestamp collisions in practice. Other dialects fall back to
        # the string id column (non-deterministic for ties; proper fix: add a
        # BIGSERIAL seq col).
        self._tiebreaker_col = (
            literal_column("conversations.rowid")
            if self._engine.dialect.name == "sqlite"
            else SqlConversation.id
        )
        ensure_fts_table(self._engine)

    def _lock_conversation(self, session: Session, conversation_id: str) -> None:
        """
        Acquire a row-level lock on the conversation to serialize
        position writes.

        On PostgreSQL, issues ``SELECT ... FOR UPDATE`` on the
        conversation row.

        On SQLite, issues a no-op ``UPDATE`` on the conversation
        row to escalate the transaction to ``RESERVED``. SQLite
        starts transactions as ``DEFERRED`` (read-only) by
        default — concurrent ``append()`` calls would otherwise
        both read the same ``next_position`` counter (or, for a
        pre-counter conversation, the same ``max(position)``) without
        holding any write lock, both allocate the same position, and
        both try to INSERT it → UNIQUE
        constraint failure on
        ``ix_conversation_items_conversation_id_position``.
        Reproduced 2026-04-30 in the user's 20-shell scenario:
        the agent loop's incremental tool-call persist raced the
        steering inbox's auto-injection of idle-notification user
        messages, both grabbed positions 34 + 35, the loser
        crashed with ``IntegrityError``. Issuing an UPDATE here
        escalates this transaction to ``RESERVED`` immediately,
        so a second concurrent transaction blocks on
        ``busy_timeout`` (20s, set in :func:`make_managed_session_maker`)
        rather than racing the read, and re-reads the up-to-date
        ``next_position`` counter (or, for a pre-counter conversation,
        ``max(position)``) once the holder commits.

        :param session: The active SQLAlchemy session.
        :param conversation_id: The conversation to lock,
            e.g. ``"conv_abc123"``.
        """
        if self._supports_for_update:
            stmt = (
                select(SqlConversation.id)
                .where(SqlConversation.id == conversation_id)
                .with_for_update()
            )
            session.execute(stmt)
        else:
            # SQLite: any UPDATE escalates the transaction to
            # RESERVED. Setting ``updated_at`` to itself is the
            # cheapest no-op write that achieves this — SQLite
            # actually executes it (no statement-level
            # short-circuit on equal values), which is what we
            # want here.
            session.execute(
                text("UPDATE conversations SET updated_at = updated_at WHERE id = :id"),
                {"id": conversation_id},
            )

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
        Create a new conversation in the database.

        :param kind: Conversation type. ``"default"`` for
            user-initiated, ``"sub_agent"`` for sub-agent
            execution conversations.
        :param title: Optional title. Phase 4 named sub-agents
            store ``"<type>:<name>"`` so the partial unique
            index enforces ``(parent_conversation_id, title)``
            uniqueness within a parent.
        :param parent_conversation_id: Phase 4 — id of the
            owning parent conversation. ``None`` for top-level.
        :param agent_id: Agent to bind at creation time, e.g.
            ``"ag_abc123"``. ``None`` only for legacy rows or
            callers that cannot bind a conversation.
        :param runner_id: Optional runner binding to persist at
            creation time, e.g. ``"runner_abc123"``. Child
            sub-agent conversations inherit the parent's binding
            through this field so runner dispatch remains explicit
            in store state.
        :param sub_agent_name: For sub-agent sessions, the
            sub-agent type name within the parent's spec tree,
            e.g. ``"summarizer"``. ``None`` for top-level.
        :param host_id: Host that should launch the runner for
            this session, e.g. ``"host_a1b2c3d4..."``. ``None``
            for CLI-initiated sessions.
        :param workspace: Absolute path on disk where the runner
            should start, e.g. ``"/Users/corey/universe/src/foo"``.
            Required when ``host_id`` is set (DB check constraint
            ``ck_conversations_workspace_required_for_host``);
            optional for CLI-launched sessions that record their
            starting cwd for display. The caller passes the
            already-canonicalized realpath from
            ``host.stat`` — this method does no expansion. When a git
            worktree was created, this is the worktree directory path.
        :param git_branch: Git branch checked out in the session's
            worktree, e.g. ``"feature/login"``. Set only when the
            session was created with a server-created worktree;
            ``None`` otherwise. See designs/SESSION_GIT_WORKTREE.md.
        :param terminal_launch_args: Optional pass-through CLI args
            for a native terminal wrapper (claude / codex), e.g.
            ``["--dangerously-skip-permissions"]``. ``None`` leaves
            the column NULL; a list (including ``[]``) is JSON-encoded
            so the runner applies it when it auto-launches the
            terminal.
        :returns: The newly created :class:`Conversation`.
        :raises NameAlreadyExistsError: If
            ``parent_conversation_id`` is set and a sibling row
            with the same ``title`` already exists.
        :raises IntegrityError: If ``host_id`` is set without
            ``workspace`` (the check constraint catches it).
        """
        from sqlalchemy.exc import IntegrityError

        from omnigent.stores.conversation_store import (
            ConversationNotFoundError,
            NameAlreadyExistsError,
        )

        now = now_epoch()
        new_id = generate_conversation_id()
        try:
            with self._session() as session:
                if parent_conversation_id is None:
                    # Top-level conversation: root_id == own id, so the
                    # tree-scoped index covers the row from the moment
                    # it lands.
                    root_id: str = new_id
                else:
                    parent_row = session.get(SqlConversation, parent_conversation_id)
                    if parent_row is None:
                        raise ConversationNotFoundError(
                            f"parent conversation {parent_conversation_id!r} does not exist"
                        )
                    # Inherit the parent's root: nested sub-agents all
                    # share the same root with their top-level ancestor.
                    # ``root_conversation_id`` is NOT NULL (see migration
                    # d8e2f3b4c910), so the parent always has one.
                    root_id = parent_row.root_conversation_id
                row = SqlConversation(
                    id=new_id,
                    created_at=now,
                    updated_at=now,
                    title=title,
                    kind=kind,
                    parent_conversation_id=parent_conversation_id,
                    root_conversation_id=root_id,
                    agent_id=agent_id,
                    runner_id=runner_id,
                    host_id=host_id,
                    sub_agent_name=sub_agent_name,
                    workspace=workspace,
                    git_branch=git_branch,
                    terminal_launch_args=(
                        json.dumps(terminal_launch_args)
                        if terminal_launch_args is not None
                        else None
                    ),
                )
                session.add(row)
                # Convert inside the session so the entity is
                # populated before SQLAlchemy detaches it on
                # session close.
                return _to_conversation(row)
        except IntegrityError as exc:
            # Translate the partial-unique-index violation into a
            # clean exception type the spawn/send tools can map
            # to a name_already_exists tool error. Other integrity
            # violations (FK, check constraints) re-raise.
            #
            # Detection prefers the specific index name (Postgres
            # surfaces it directly), and falls back to the
            # ``parent_conversation_id`` + ``title`` column
            # signature (SQLite tends to format the message that
            # way). This is narrower than a generic "unique"
            # check, which would misclassify any future unique
            # constraint added to the conversations table.
            msg = str(exc).lower()
            is_partial_index_violation = "ix_conversations_parent_title_unique" in msg or (
                "unique" in msg and "parent_conversation_id" in msg and "title" in msg
            )
            if is_partial_index_violation:
                raise NameAlreadyExistsError(
                    f"sub-agent name already exists under parent "
                    f"{parent_conversation_id!r}: title={title!r}"
                ) from exc
            raise

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Fetch a conversation by its unique ID.

        Populates ``Conversation.labels`` via a second query
        against ``conversation_labels`` — separate from the
        conversation row fetch because the label JOIN would
        otherwise multiply the row count by the label count
        and require post-processing. The two queries run in
        the same session so they see a consistent snapshot
        under serializable isolation.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The :class:`Conversation` if found, otherwise
            ``None``.
        """
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if row is None:
                return None
            return _to_conversation(row, _fetch_labels(session, conversation_id))

    def get_runner_ids(self, conversation_ids: list[str]) -> dict[str, str | None]:
        """
        Single ``SELECT id, runner_id WHERE id IN (...)`` — bulk
        variant of :meth:`get_conversation` for the runner-dot path.
        Missing ids are omitted; ids without a bound runner map to
        ``None``.
        """
        if not conversation_ids:
            return {}
        unique_ids = list(set(conversation_ids))
        with self._session() as session:
            rows = session.execute(
                select(SqlConversation.id, SqlConversation.runner_id).where(
                    SqlConversation.id.in_(unique_ids)
                )
            ).all()
        return {row.id: row.runner_id for row in rows}

    def get_session_connectivity(
        self, conversation_ids: list[str]
    ) -> dict[str, SessionConnectivity]:
        """
        Return connectivity fields for a batch of sessions in one query.

        Two bulk ``SELECT`` s — one over ``conversations`` for the
        runner/host binding, one over ``conversation_labels`` for the
        fork-source connectivity marker — instead of the per-id
        ``get_conversation`` + labels fan-out the sidebar online-dot used
        to drive. See the abstract method for the contract.

        :param conversation_ids: Session/conversation IDs to look up,
            e.g. ``["conv_abc123", "conv_def456"]``.
        :returns: Mapping ``conversation_id -> SessionConnectivity``;
            ids without a conversation row are omitted.
        """
        if not conversation_ids:
            return {}
        unique_ids = list(set(conversation_ids))
        with self._session() as session:
            rows = session.execute(
                select(
                    SqlConversation.id,
                    SqlConversation.runner_id,
                    SqlConversation.host_id,
                ).where(SqlConversation.id.in_(unique_ids))
            ).all()
            # One pass over the fork-source connectivity marker, which
            # signals on presence (its value is the source id).
            label_rows = session.execute(
                select(
                    SqlConversationLabel.conversation_id,
                    SqlConversationLabel.key,
                    SqlConversationLabel.value,
                ).where(
                    SqlConversationLabel.conversation_id.in_(unique_ids),
                    SqlConversationLabel.key.in_([FORK_SOURCE_LABEL_KEY]),
                )
            ).all()
        needs_workspace_ids = {
            row.conversation_id for row in label_rows if row.key == FORK_SOURCE_LABEL_KEY
        }
        return {
            row.id: SessionConnectivity(
                runner_id=row.runner_id,
                host_id=row.host_id,
                needs_workspace=row.id in needs_workspace_ids,
            )
            for row in rows
        }

    def get_conversations(self, conversation_ids: list[str]) -> dict[str, Conversation]:
        """
        Bulk variant of :meth:`get_conversation` — one ``SELECT ... WHERE
        id IN (...)`` for the rows plus one batched label query, so the
        watch-set rescan costs a constant number of round-trips instead
        of one per id. Missing ids are omitted from the result.

        :param conversation_ids: Conversation ids to fetch,
            e.g. ``["conv_abc123", "conv_def456"]``. Duplicates are
            tolerated; empty input returns ``{}`` without a query.
        :returns: Mapping ``{conversation_id: Conversation}`` for the
            ids that resolved to a row.
        """
        if not conversation_ids:
            return {}
        unique_ids = list(set(conversation_ids))
        with self._session() as session:
            rows = list(
                session.execute(select(SqlConversation).where(SqlConversation.id.in_(unique_ids)))
                .scalars()
                .all()
            )
            # Batch the labels in the same session so the bulk fetch sees a
            # consistent snapshot and avoids the per-row label fan-out that
            # get_conversation incurs. Build the entities inside the session
            # too — _to_conversation reads ORM columns, which would raise
            # DetachedInstanceError once the session closes.
            labels_by_conv = _fetch_labels_bulk(session, [row.id for row in rows])
            return {row.id: _to_conversation(row, labels_by_conv.get(row.id, {})) for row in rows}

    def list_child_conversation_ids_by_parent(
        self,
        parent_conversation_ids: list[str],
    ) -> dict[str, list[str]]:
        """
        Return direct sub-agent child ids grouped by parent conversation.

        Uses the partial ``idx_conversations_parent`` index by filtering on
        ``kind="sub_agent"`` plus ``parent_conversation_id IN (...)``. This
        gives sidebar session-list status roll-up one batched identity query
        instead of one full child listing per visible parent row.

        :param parent_conversation_ids: Parent conversation ids to
            inspect, e.g. ``["conv_parent1", "conv_parent2"]``.
            Duplicates are tolerated.
        :returns: Mapping from every unique input parent id to direct
            child ids. Parents with no direct sub-agent children, or ids
            that do not exist, map to an empty list.
        """
        unique_ids = list(dict.fromkeys(parent_conversation_ids))
        result: dict[str, list[str]] = {parent_id: [] for parent_id in unique_ids}
        if not unique_ids:
            return result

        with self._session() as session:
            rows = session.execute(
                select(SqlConversation.parent_conversation_id, SqlConversation.id)
                .where(SqlConversation.kind == "sub_agent")
                .where(SqlConversation.parent_conversation_id.in_(unique_ids))
                .order_by(
                    SqlConversation.parent_conversation_id,
                    desc(SqlConversation.created_at),
                    desc(self._tiebreaker_col),
                )
            ).all()
            for parent_id, child_id in rows:
                if parent_id is not None:
                    result[parent_id].append(child_id)
        return result

    def set_labels(
        self,
        conversation_id: str,
        updates: dict[str, str],
        updated_at: int | None = None,
    ) -> None:
        """
        Upsert guardrails labels on a conversation.

        Single-transaction batched UPSERT — either every key
        lands or none do (POLICIES.md §6.3). The dialect-aware
        path dispatches to ``INSERT ... ON CONFLICT`` on
        SQLite / PostgreSQL; other dialects fall back to
        SELECT-then-INSERT/UPDATE inside the same transaction.
        Empty updates is a no-op.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param updates: Mapping from label key to new value.
            Example: ``{"integrity": "0"}``. Empty dict
            returns immediately without opening a transaction.
        :param updated_at: Caller-supplied timestamp
            (``None`` → current wall-clock). See the abstract
            method docstring for why callers may want to
            pass their own.
        """
        if not updates:
            return
        stamp = updated_at if updated_at is not None else now_epoch()
        with self._session() as session:
            _upsert_labels(session, conversation_id, updates, stamp)

    def set_session_state(
        self,
        conversation_id: str,
        state: dict[str, Any],
    ) -> None:
        """
        Persist the full session-state snapshot for a conversation.

        Serializes *state* as JSON and writes it to the
        ``session_state`` column on the ``conversations`` table.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param state: The complete session-state dict to persist.
        """
        import json

        with self._session() as session:
            session.execute(
                update(SqlConversation)
                .where(SqlConversation.id == conversation_id)
                .values(session_state=json.dumps(state))
            )

    def set_session_usage(
        self,
        conversation_id: str,
        usage: dict[str, Any],
    ) -> None:
        """
        Persist the cumulative LLM token usage for a conversation.

        Serializes *usage* as JSON and writes it to the
        ``session_usage`` column on the ``conversations`` table.

        :param conversation_id: The conversation to update,
            e.g. ``"conv_abc123"``.
        :param usage: The complete usage dict to persist, e.g.
            ``{"input_tokens": 1500, "output_tokens": 350,
            "total_tokens": 1850}``. May carry a nested ``"by_model"``
            sub-dict (per-model token/cost buckets), hence ``Any``.
        """
        import json

        with self._session() as session:
            session.execute(
                update(SqlConversation)
                .where(SqlConversation.id == conversation_id)
                .values(session_usage=json.dumps(usage))
            )

    def add_daily_cost(self, user_id: str, day_utc: str, delta_usd: float) -> None:
        """
        Atomically add *delta_usd* to a user's spend for one UTC day.

        Dialect-aware: SQLite and PostgreSQL both support
        ``INSERT ... ON CONFLICT ... DO UPDATE``, used here for a true
        atomic increment (``cost_usd = cost_usd + :delta``) so
        concurrent turns never lose updates. Other dialects fall back
        to a SELECT-then-INSERT/UPDATE inside the same transaction.
        ``delta_usd <= 0`` is a no-op (never creates a row).

        :param user_id: The user the cost is attributed to (session
            creator), e.g. ``"alice@example.com"``.
        :param day_utc: UTC day as ``"YYYY-MM-DD"``, e.g.
            ``"2026-06-05"``.
        :param delta_usd: USD amount to add; ``<= 0`` is a no-op.
        """
        if delta_usd <= 0:
            return
        now = now_epoch()
        with self._session() as session:
            dialect = session.bind.dialect.name if session.bind is not None else ""
            if dialect in ("sqlite", "postgresql"):
                self._upsert_daily_cost_dialect(session, dialect, user_id, day_utc, delta_usd, now)
                return
            # Generic dialect fallback — SELECT-then-INSERT/UPDATE in one
            # transaction (race-safe under SERIALIZABLE / SQLite's
            # single-writer semantics).
            existing = session.get(SqlUserDailyCost, (user_id, day_utc))
            if existing is None:
                session.add(
                    SqlUserDailyCost(
                        user_id=user_id,
                        day_utc=day_utc,
                        cost_usd=delta_usd,
                        updated_at=now,
                    )
                )
            else:
                # mypy sees the Mapped[...] descriptor types; at runtime
                # these are plain attributes accepting the Python value.
                existing.cost_usd = existing.cost_usd + delta_usd  # type: ignore[assignment]
                existing.updated_at = now  # type: ignore[assignment]

    def _upsert_daily_cost_dialect(
        self,
        session: Session,
        dialect: str,
        user_id: str,
        day_utc: str,
        delta_usd: float,
        now: int,
    ) -> None:
        """
        Atomic ``INSERT ... ON CONFLICT DO UPDATE`` increment for
        SQLite / PostgreSQL.

        Extracted from :meth:`add_daily_cost` so each method stays
        small; the outer method selects the dialect branch and this
        one executes the dedicated INSERT builder. The conflict target
        is the ``(user_id, day_utc)`` primary key; on conflict the
        existing ``cost_usd`` is incremented by the new row's value.

        :param session: Active SQLAlchemy session.
        :param dialect: ``"sqlite"`` or ``"postgresql"`` (the caller
            gates all other dialects onto the generic fallback).
        :param user_id: The user the cost is attributed to, e.g.
            ``"alice@example.com"``.
        :param day_utc: UTC day as ``"YYYY-MM-DD"``, e.g.
            ``"2026-06-05"``.
        :param delta_usd: USD amount to add (already validated ``> 0``).
        :param now: Unix epoch seconds to stamp on ``updated_at``.
        """
        # Typed as Any to sidestep the mypy variance between the two
        # dialect-specific ``Insert`` classes; their runtime shape is
        # identical for this UPSERT.
        stmt: Any
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(SqlUserDailyCost)
        else:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(SqlUserDailyCost)
        stmt = stmt.values(user_id=user_id, day_utc=day_utc, cost_usd=delta_usd, updated_at=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "day_utc"],
            set_={
                "cost_usd": SqlUserDailyCost.cost_usd + stmt.excluded.cost_usd,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        session.execute(stmt)

    def get_daily_cost(self, user_id: str, day_utc: str) -> float:
        """
        Return a user's accumulated LLM spend for one UTC day.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param day_utc: UTC day as ``"YYYY-MM-DD"``, e.g.
            ``"2026-06-05"``.
        :returns: The accumulated ``cost_usd``, or ``0.0`` when no row
            exists for ``(user_id, day_utc)``.
        """
        with self._session() as session:
            row = session.get(SqlUserDailyCost, (user_id, day_utc))
            return float(row.cost_usd) if row is not None else 0.0

    def get_daily_cost_state(self, user_id: str, day_utc: str) -> dict[str, float]:
        """
        Return a user's daily cost rollup state for one UTC day.

        Reads both fields the per-user daily cost-budget policy needs in
        a single point lookup: the accumulated spend and the highest
        soft checkpoint already approved that day.

        :param user_id: The user to read, e.g. ``"alice@example.com"``.
        :param day_utc: UTC day as ``"YYYY-MM-DD"``, e.g.
            ``"2026-06-05"``.
        :returns: ``{"cost_usd": <float>, "ask_approved_usd": <float>}``;
            both ``0.0`` when no row exists for ``(user_id, day_utc)``.
        """
        with self._session() as session:
            row = session.get(SqlUserDailyCost, (user_id, day_utc))
            if row is None:
                return {"cost_usd": 0.0, "ask_approved_usd": 0.0}
            return {
                "cost_usd": float(row.cost_usd),
                "ask_approved_usd": float(row.ask_approved_usd or 0.0),
            }

    def set_daily_ask_approved(self, user_id: str, day_utc: str, ask_approved_usd: float) -> None:
        """
        Record the highest approved soft checkpoint for a user+day.

        UPSERT that sets ``ask_approved_usd`` **without touching
        ``cost_usd``** (inserts a ``cost_usd = 0`` row when none exists
        yet, otherwise updates only the approval field). Called when a
        per-user daily cost-budget ASK is approved, so the same
        checkpoint won't re-prompt for that user again that day — even
        from a different session.

        :param user_id: The user the approval is for, e.g.
            ``"alice@example.com"``.
        :param day_utc: UTC day as ``"YYYY-MM-DD"``, e.g.
            ``"2026-06-05"``.
        :param ask_approved_usd: The crossed checkpoint value (USD) the
            user approved continuing past, e.g. ``0.05``.
        """
        now = now_epoch()
        with self._session() as session:
            dialect = session.bind.dialect.name if session.bind is not None else ""
            if dialect in ("sqlite", "postgresql"):
                # Typed as Any to sidestep the mypy variance between the
                # two dialect-specific ``Insert`` classes.
                stmt: Any
                if dialect == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                    stmt = sqlite_insert(SqlUserDailyCost)
                else:
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    stmt = pg_insert(SqlUserDailyCost)
                stmt = stmt.values(
                    user_id=user_id,
                    day_utc=day_utc,
                    cost_usd=0.0,
                    ask_approved_usd=ask_approved_usd,
                    updated_at=now,
                )
                # On conflict touch only the approval (+ stamp) — never
                # the accumulated cost.
                stmt = stmt.on_conflict_do_update(
                    index_elements=["user_id", "day_utc"],
                    set_={
                        "ask_approved_usd": stmt.excluded.ask_approved_usd,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                session.execute(stmt)
                return
            # Generic dialect fallback — SELECT-then-INSERT/UPDATE.
            existing = session.get(SqlUserDailyCost, (user_id, day_utc))
            if existing is None:
                session.add(
                    SqlUserDailyCost(
                        user_id=user_id,
                        day_utc=day_utc,
                        cost_usd=0.0,
                        ask_approved_usd=ask_approved_usd,
                        updated_at=now,
                    )
                )
            else:
                existing.ask_approved_usd = ask_approved_usd  # type: ignore[assignment]
                existing.updated_at = now  # type: ignore[assignment]

    def get_session_owner(self, conversation_id: str) -> str | None:
        """
        Return the user id that owns a session (its creator).

        Reads ``session_permissions`` and returns the
        highest-``level`` grantee: the creator's ``LEVEL_OWNER``
        (4) grant outranks any read (1) / edit (2) / manage (3)
        grant, so ``ORDER BY level DESC LIMIT 1`` yields the owner
        without hardcoding the owner-level integer. The
        ``"__public__"`` public-access sentinel is excluded, so a
        session that only carries a public grant (and no real
        owner) returns ``None`` rather than the sentinel.

        :param conversation_id: The session to look up, e.g.
            ``"conv_abc123"``.
        :returns: The owner's user id, e.g. ``"alice@example.com"``,
            or ``None`` when the session has no real (non-public)
            permission grants.
        """
        from omnigent.db.db_models import SqlSessionPermission
        from omnigent.server.auth import RESERVED_USER_PUBLIC

        with self._session() as session:
            return session.execute(
                select(SqlSessionPermission.user_id)
                .where(SqlSessionPermission.conversation_id == conversation_id)
                .where(SqlSessionPermission.user_id != RESERVED_USER_PUBLIC)
                .order_by(SqlSessionPermission.level.desc())
                .limit(1)
            ).scalar_one_or_none()

    def search(
        self,
        query: str,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[ConversationItem]:
        """
        Full-text search over conversation items.

        Uses the FTS virtual table to match items by
        ``search_text``, ranked by relevance.

        :param query: The FTS search query string,
            e.g. ``"deployment error"``.
        :param conversation_id: Optional conversation to scope
            the search to, e.g. ``"conv_abc123"``.
        :param limit: Maximum number of results to return.
        :returns: A list of matching :class:`ConversationItem`
            objects in relevance order.
        """
        with self._session() as session:
            # Dialect-specific search: the SQLite family (SQLite + D1) has
            # FTS5 virtual tables (MATCH + rank); PostgreSQL doesn't. ILIKE on
            # the JSON data column is a functional fallback there. Proper
            # tsvector indexing is a future optimization (tracked in GAPS.md).
            use_fts = _supports_fts5(self._engine.dialect.name)
            if use_fts:
                if conversation_id is not None:
                    stmt = text(
                        "SELECT item_id FROM conversation_items_fts "
                        "WHERE conversation_id = :cid "
                        "AND search_text MATCH :query "
                        "ORDER BY rank LIMIT :limit"
                    )
                else:
                    stmt = text(
                        "SELECT item_id FROM conversation_items_fts "
                        "WHERE search_text MATCH :query "
                        "ORDER BY rank LIMIT :limit"
                    )
            else:
                # PostgreSQL: ILIKE fallback (no FTS5 virtual table).
                # Full tsvector/tsquery indexing can be added later.
                like_pattern = f"%{query}%"
                if conversation_id is not None:
                    stmt = text(
                        "SELECT ci.id FROM conversation_items ci "
                        "WHERE ci.conversation_id = :cid "
                        "AND ci.data::text ILIKE :query "
                        "ORDER BY ci.created_at DESC LIMIT :limit"
                    )
                else:
                    stmt = text(
                        "SELECT ci.id FROM conversation_items ci "
                        "WHERE ci.data::text ILIKE :query "
                        "ORDER BY ci.created_at DESC LIMIT :limit"
                    )
                query = like_pattern
            params: dict[str, str | int] = {"query": query, "limit": limit}
            if conversation_id is not None:
                params["cid"] = conversation_id
            item_ids = [row[0] for row in session.execute(stmt, params).fetchall()]
            if not item_ids:
                return []
            rows = (
                session.execute(
                    select(SqlConversationItem).where(SqlConversationItem.id.in_(item_ids))
                )
                .scalars()
                .all()
            )
            # Preserve FTS rank order
            order = {iid: i for i, iid in enumerate(item_ids)}
            return [_to_item(r) for r in sorted(rows, key=lambda r: order[r.id])]

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
        List items in a conversation with cursor-based pagination.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return.
        :param after: Cursor item ID; return items appearing
            after this item in sort order,
            e.g. ``"msg_xyz789"``.
        :param before: Cursor item ID; return items appearing
            before this item in sort order.
        :param order: Sort direction on position,
            ``"asc"`` or ``"desc"``.
        :param type: Optional item type filter. When provided, only items
            with this type are returned, e.g. ``"compaction"``. ``None``
            means return all types.
        :returns: A :class:`PagedList` of
            :class:`ConversationItem` objects.
        """
        with self._session() as session:
            is_asc = order == "asc"
            sort_fn = asc if is_asc else desc
            stmt = select(SqlConversationItem).where(
                SqlConversationItem.conversation_id == conversation_id
            )
            if type is not None:
                stmt = stmt.where(SqlConversationItem.type == type)
            if after:
                sub = (
                    select(SqlConversationItem.position)
                    .where(SqlConversationItem.id == after)
                    .scalar_subquery()
                )
                # "after" = further in sort direction
                stmt = stmt.where(
                    SqlConversationItem.position > sub
                    if is_asc
                    else SqlConversationItem.position < sub
                )
            if before:
                sub = (
                    select(SqlConversationItem.position)
                    .where(SqlConversationItem.id == before)
                    .scalar_subquery()
                )
                # "before" = opposite of sort direction
                stmt = stmt.where(
                    SqlConversationItem.position < sub
                    if is_asc
                    else SqlConversationItem.position > sub
                )
            stmt = stmt.order_by(sort_fn(SqlConversationItem.position)).limit(limit + 1)
            rows = list(session.execute(stmt).scalars().all())
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
            items = [_to_item(r) for r in rows]
            return PagedList(
                data=items,
                first_id=items[0].id if items else None,
                last_id=items[-1].id if items else None,
                has_more=has_more,
            )

    def list_latest_message_items_for_conversations(
        self,
        conversation_ids: list[str],
        per_conversation_limit: int = 10,
    ) -> dict[str, list[ConversationItem]]:
        """
        Return newest message items for multiple conversations.

        Uses ``row_number() over (partition by conversation_id order by
        position desc)`` so the database returns at most
        ``per_conversation_limit`` message rows per conversation. This keeps
        child-session summary rendering to one query instead of an N+1
        ``list_items`` fan-out.

        :param conversation_ids: Conversation ids to fetch messages for,
            e.g. ``["conv_child1", "conv_child2"]``.
        :param per_conversation_limit: Maximum number of message items per
            conversation, e.g. ``10``.
        :returns: Mapping from every unique input id to its newest message
            items in descending position order.
        """
        unique_ids = list(dict.fromkeys(conversation_ids))
        result: dict[str, list[ConversationItem]] = {cid: [] for cid in unique_ids}
        if not unique_ids or per_conversation_limit <= 0:
            return result

        with self._session() as session:
            ranked = _ranked_latest_message_item_ids(unique_ids)
            rows = (
                session.execute(
                    select(SqlConversationItem)
                    .join(ranked, SqlConversationItem.id == ranked.c.item_id)
                    .where(ranked.c.row_num <= per_conversation_limit)
                    .order_by(
                        SqlConversationItem.conversation_id,
                        desc(SqlConversationItem.position),
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                result[row.conversation_id].append(_to_item(row))
        return result

    def append(
        self,
        conversation_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        """
        Append items to a conversation.

        Assigns a globally unique ID, timestamp, and incrementing
        position to each item. Also inserts FTS records for
        searchability.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param items: List of :class:`NewConversationItem` objects
            to persist.
        :returns: The persisted :class:`ConversationItem` list
            with store-assigned IDs and timestamps.
        """
        now = now_epoch()
        persisted: list[ConversationItem] = []

        with self._session() as session:
            # Lock the conversation row to serialize position writes.
            # On PostgreSQL this is a row-level FOR UPDATE lock; on
            # SQLite the database-level lock already serializes.
            self._lock_conversation(session, conversation_id)

            # Bump updated_at on the conversation.
            conv_row = session.get(SqlConversation, conversation_id)
            if conv_row is not None:
                conv_row.updated_at = now

            # Allocate item positions from the conversation's maintained
            # next_position counter instead of running a MAX(position) aggregate
            # on every append. Reading + advancing the counter under
            # _lock_conversation keeps allocation O(1), drops a query per write,
            # and stays collision-free. The aggregate is an index lookup on this
            # schema (ix_conversation_items_conversation_id_position), but a
            # maintained counter avoids the per-append round-trip regardless and
            # scales to backends where that same allocation is a full scan.
            #
            # Backwards compatibility: conversations created before this counter
            # existed have next_position = NULL; fall back to a one-time
            # MAX(position) scan (coalesce to -1 so the first item gets 0), then
            # persist the counter below so every later append is scan-free.
            if conv_row is not None and conv_row.next_position is not None:
                next_pos = conv_row.next_position
            else:
                next_pos = (
                    session.execute(
                        select(func.coalesce(func.max(SqlConversationItem.position), -1)).where(
                            SqlConversationItem.conversation_id == conversation_id
                        )
                    ).scalar_one()
                    + 1
                )

            for item in items:
                position = next_pos
                next_pos += 1
                data_dict = item.data.model_dump(exclude_none=True)
                # Strip NUL bytes before they reach a Postgres text
                # column, which rejects them outright. Tool output can
                # embed NUL (e.g. reading a binary file); without this
                # the whole INSERT aborts and the item never persists.
                data = strip_nul_bytes(json.dumps(data_dict))
                search = strip_nul_bytes(extract_search_text(item))
                item_id = generate_item_id(item.type)
                row = SqlConversationItem(
                    id=item_id,
                    conversation_id=conversation_id,
                    response_id=item.response_id,
                    created_at=now,
                    status="completed",  # items are final on append
                    position=position,
                    type=item.type,
                    data=data,
                    search_text=search,
                    created_by=item.created_by,
                )
                session.add(row)
                insert_fts(session, item_id, conversation_id, search)
                persisted.append(
                    ConversationItem(
                        id=row.id,
                        type=row.type,
                        status=row.status,
                        response_id=row.response_id,
                        created_at=row.created_at,
                        data=item.data,
                        created_by=item.created_by,
                    )
                )

            # Persist the advanced counter so the next append reads it instead
            # of scanning; this also lazily backfills a pre-counter conversation.
            if conv_row is not None:
                conv_row.next_position = next_pos

        return persisted

    def list_projects(
        self,
        accessible_by: str | None = None,
    ) -> list[str]:
        """
        Return all distinct project names, ordered alphabetically.

        Projects are implicit: they exist as long as at least one
        *non-archived* ``conversation_labels`` row with ``key="omni_project"``
        references them. Archived sessions keep their project label (so
        unarchiving restores a session to its original project), but a project
        whose every member is archived drops out of this list — that is what
        makes "Delete project" (which archives all members) remove the folder
        while leaving the sessions recoverable. The label key is namespaced
        (``omni_*``) to keep this internal storage key distinct from the
        user-facing "project" term and from any future reserved keys; it is
        never surfaced as a label in the UI.

        :param accessible_by: When set, restrict to sessions that
            ``accessible_by`` has a permission row for (mirrors the
            ``list_conversations`` ACL filter).
        :returns: List of project names ordered ascending.
        """
        with self._session() as session:
            # Join to the conversation so archived sessions don't keep an
            # otherwise-empty project alive in the sidebar.
            stmt = (
                select(SqlConversationLabel.value)
                .join(
                    SqlConversation,
                    SqlConversation.id == SqlConversationLabel.conversation_id,
                )
                .where(
                    SqlConversationLabel.key == PROJECT_LABEL_KEY,
                    SqlConversation.archived.is_(False),
                )
                .distinct()
                .order_by(SqlConversationLabel.value)
            )
            if accessible_by is not None:
                from omnigent.db.db_models import SqlSessionPermission

                accessible_ids = select(SqlSessionPermission.conversation_id).where(
                    SqlSessionPermission.user_id == accessible_by
                )
                stmt = stmt.where(SqlConversationLabel.conversation_id.in_(accessible_ids))
            return [row[0] for row in session.execute(stmt).all()]

    def delete_label(
        self,
        conversation_id: str,
        key: str,
    ) -> None:
        """
        Delete a single label key from a conversation.

        No-op if the label does not exist.

        :param conversation_id: The conversation to update.
        :param key: The label key to remove, e.g. ``"omni_project"``.
        """
        with self._session() as session:
            session.execute(
                delete(SqlConversationLabel).where(
                    SqlConversationLabel.conversation_id == conversation_id,
                    SqlConversationLabel.key == key,
                )
            )

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
        project: str | None = None,
    ) -> PagedList[Conversation]:
        """
        List conversations with cursor-based pagination.

        :param limit: Maximum number of conversations to return.
        :param after: Cursor conversation ID; return
            conversations appearing after this one in sort
            order, e.g. ``"conv_abc123"``.
        :param before: Cursor conversation ID; return
            conversations appearing before this one in sort
            order.
        :param kind: Filter to conversations of this kind.
        :param parent_conversation_id: Phase 4 — when set, only
            return conversations whose parent matches. ``None``
            disables the filter.
        :param agent_id: When set, only return conversations
            that have at least one task whose ``agent_id``
            matches. Implemented as an EXISTS subquery on
            ``tasks`` so the resulting rows stay distinct (no
            JOIN duplication). ``None`` disables the filter.
        :param agent_name: When set, only return conversations
            whose bound ``conversations.agent_id`` points at an
            agent row with this name. Unlike ``agent_id``, this
            intentionally matches session-scoped agents that share
            a user-authored name. ``None`` disables the filter.
        :param has_agent_id: When ``True``, only return
            conversations whose ``agent_id`` column is not
            ``None``. Powers ``GET /v1/sessions`` — sessions
            always have an agent binding. ``None`` disables.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :param sort_by: Column to sort on, ``"created_at"``
            or ``"updated_at"``.
        :param search_query: Case-insensitive substring filter on
            the session title OR conversation item content.
            ``None`` or empty string disables the filter;
            otherwise matches conversations where
            ``LOWER(title) LIKE %query%`` or any
            ``conversation_items.search_text`` contains the
            query. Implemented with the SQL ``LIKE`` operator
            (no FTS) so it works against both SQLite and
            Postgres without extra extensions.
        :param include_archived: When ``False`` (default), exclude
            rows where ``archived`` is true. When ``True``, include
            archived rows alongside non-archived ones.
        :param project: When set to a non-empty string, only return
            sessions that have a ``conversation_labels`` row with
            ``key="omni_project"`` and ``value=project``. When set to an
            empty string ``""``, only return sessions with NO project
            label (i.e., unfiled sessions). ``None`` disables the
            filter.
        :returns: A :class:`PagedList` of :class:`Conversation`
            objects.
        """
        sort_col = self._resolve_sort_column(sort_by)
        with self._session() as session:
            is_desc = order == "desc"
            sort_fn = desc if is_desc else asc
            stmt = select(SqlConversation)
            # Filter by kind when specified (None = no filter).
            if kind is not None:
                stmt = stmt.where(SqlConversation.kind == kind)
            if parent_conversation_id is not None:
                stmt = stmt.where(
                    SqlConversation.parent_conversation_id == parent_conversation_id,
                )
            if root_conversation_id is not None:
                stmt = stmt.where(
                    SqlConversation.root_conversation_id == root_conversation_id,
                )
            if has_agent_id is True:
                stmt = stmt.where(SqlConversation.agent_id.is_not(None))
            if not include_archived:
                stmt = stmt.where(SqlConversation.archived.is_(False))
            if agent_name is not None:
                stmt = stmt.join(SqlAgent, SqlAgent.id == SqlConversation.agent_id).where(
                    SqlAgent.name == agent_name
                )
            if agent_id is not None:
                # Filter by the agent_id column on conversations directly
                # (the tasks table has been removed). Conversations without
                # an agent binding (legacy rows) correctly return no results
                # because their agent_id column is NULL.
                stmt = stmt.where(SqlConversation.agent_id == agent_id)
            if accessible_by is not None:
                from omnigent.db.db_models import SqlSessionPermission

                accessible_ids = select(SqlSessionPermission.conversation_id).where(
                    SqlSessionPermission.user_id == accessible_by
                )
                stmt = stmt.where(SqlConversation.id.in_(accessible_ids))
            if search_query:
                pattern = f"%{search_query.lower()}%"
                title_match = func.lower(SqlConversation.title).like(pattern)
                content_match = SqlConversation.id.in_(
                    select(SqlConversationItem.conversation_id)
                    .where(func.lower(SqlConversationItem.search_text).like(pattern))
                    .distinct()
                )
                stmt = stmt.where(or_(title_match, content_match))
            if project is not None:
                if project == "":
                    # Unfiled: sessions with no project label at all.
                    stmt = stmt.where(
                        SqlConversation.id.not_in(
                            select(SqlConversationLabel.conversation_id).where(
                                SqlConversationLabel.key == PROJECT_LABEL_KEY
                            )
                        )
                    )
                else:
                    # Specific project: session must have this project label.
                    stmt = stmt.where(
                        SqlConversation.id.in_(
                            select(SqlConversationLabel.conversation_id).where(
                                SqlConversationLabel.key == PROJECT_LABEL_KEY,
                                SqlConversationLabel.value == project,
                            )
                        )
                    )
            if after:
                stmt = self._apply_cursor(
                    stmt,
                    after,
                    sort_col,
                    is_desc,
                    tiebreaker_col=self._tiebreaker_col,
                    forward=True,
                )
            if before:
                stmt = self._apply_cursor(
                    stmt,
                    before,
                    sort_col,
                    is_desc,
                    tiebreaker_col=self._tiebreaker_col,
                    forward=False,
                )
            stmt = stmt.order_by(
                sort_fn(sort_col),
                sort_fn(self._tiebreaker_col),  # insertion-order tiebreaker for timestamp ties
            ).limit(limit + 1)
            rows = list(session.execute(stmt).scalars().all())
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
            # Fetch labels for all returned conversations in a
            # single IN-clause query so the list-path is O(1)
            # queries regardless of page size. Dropping this
            # would either silently return empty-labels
            # conversations (silent data loss) or fan out to
            # N+1 per-row queries.
            labels_by_conv = _fetch_labels_bulk(
                session,
                [r.id for r in rows],
            )
            convs = [_to_conversation(r, labels_by_conv.get(r.id, {})) for r in rows]
            return PagedList(
                data=convs,
                first_id=convs[0].id if convs else None,
                last_id=convs[-1].id if convs else None,
                has_more=has_more,
            )

    @staticmethod
    def _resolve_sort_column(sort_by: str) -> QueryableAttribute[int]:
        """
        Map a ``sort_by`` string to the corresponding
        :class:`SqlConversation` column.

        :param sort_by: ``"created_at"`` or ``"updated_at"``.
        :returns: The mapped column attribute.
        :raises ValueError: If ``sort_by`` is not a valid column
            name.
        """
        allowed = {
            "created_at": SqlConversation.created_at,
            "updated_at": SqlConversation.updated_at,
        }
        col = allowed.get(sort_by)
        if col is None:
            raise ValueError(f"invalid sort_by: {sort_by!r}")
        return col

    @staticmethod
    def _apply_cursor(
        stmt: Select[tuple[SqlConversation]],
        cursor_id: str,
        sort_col: QueryableAttribute[int],
        is_desc: bool,
        tiebreaker_col: ColumnElement[Any],
        forward: bool,
    ) -> Select[tuple[SqlConversation]]:
        """
        Add a cursor-based WHERE clause to the query.

        Add a ``(sort_col, tiebreaker_col)`` composite WHERE clause so
        that cursor pagination is consistent with the ORDER BY key.

        :param stmt: The current SELECT statement to augment.
        :param cursor_id: The conversation ID acting as the page cursor,
            e.g. ``"conv_abc123"``.
        :param sort_col: Primary sort column (``created_at`` or ``updated_at``).
        :param is_desc: ``True`` for descending, ``False`` for ascending.
        :param tiebreaker_col: Secondary sort column; must match the
            secondary ORDER BY column. See ``_tiebreaker_col`` in
            ``__init__`` for the SQLite/non-SQLite choice.
        :param forward: ``True`` for ``after`` cursors, ``False`` for
            ``before`` cursors.
        :returns: The statement with the cursor WHERE clause applied.
        """
        sub = select(sort_col).where(SqlConversation.id == cursor_id).scalar_subquery()
        # When tiebreaker_col is SqlConversation.id (non-SQLite), its value for
        # the cursor row is cursor_id itself — no extra subquery needed.
        # For SQLite rowid (a literal_column), we must query the DB.
        if isinstance(tiebreaker_col, QueryableAttribute):
            tiebreaker_val: Any = cursor_id
        else:
            tiebreaker_val = (
                select(tiebreaker_col).where(SqlConversation.id == cursor_id).scalar_subquery()
            )
        # "after" (forward=True) = further in sort direction;
        # "before" (forward=False) = opposite of sort direction.
        if forward:
            ts_cmp = sort_col < sub if is_desc else sort_col > sub
            id_cmp = (
                tiebreaker_col < tiebreaker_val if is_desc else tiebreaker_col > tiebreaker_val
            )
        else:
            ts_cmp = sort_col > sub if is_desc else sort_col < sub
            id_cmp = (
                tiebreaker_col > tiebreaker_val if is_desc else tiebreaker_col < tiebreaker_val
            )
        return stmt.where(or_(ts_cmp, and_(sort_col == sub, id_cmp)))

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

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :param title: New title, or ``None`` to leave unchanged.
        :param reasoning_effort: Per-session reasoning effort,
            e.g. ``"high"``. ``None`` leaves unchanged.
        :param _unset_reasoning_effort: When ``True``, clear
            ``reasoning_effort`` to ``None``.
        :param model_override: Per-session LLM model override,
            e.g. ``"claude-opus-4-7"``. ``None`` leaves unchanged.
        :param _unset_model_override: When ``True``, clear
            ``model_override`` to ``None``.
        :param cost_control_mode_override: Per-session cost-control
            switch, ``"on"`` or ``"off"``. ``None`` leaves unchanged.
        :param _unset_cost_control_mode_override: When ``True``, clear
            ``cost_control_mode_override`` to ``None``.
        :param harness_override: Per-session brain-harness override,
            e.g. ``"pi"``. ``None`` leaves unchanged; set once at
            session create, no ``_unset`` variant.
        :param terminal_launch_args: Per-session native-terminal
            pass-through args, e.g.
            ``["--dangerously-skip-permissions"]``. ``None`` leaves
            unchanged; a list (including ``[]``) replaces the stored
            value wholesale (resume is last-write-wins, never an
            append). JSON-encoded into the column.
        :param archived: New archived state. ``True`` archives,
            ``False`` unarchives, ``None`` leaves unchanged.
        :returns: The updated :class:`Conversation`, or ``None``
            if the conversation does not exist.
        """
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if not row:
                return None
            changed = False
            if title is not None:
                row.title = title
                changed = True
            if archived is not None:
                row.archived = archived
                changed = True
            if _unset_reasoning_effort:
                row.reasoning_effort = None
                changed = True
            elif reasoning_effort is not None:
                row.reasoning_effort = reasoning_effort
                changed = True
            if _unset_model_override:
                row.model_override = None
                changed = True
            elif model_override is not None:
                row.model_override = model_override
                changed = True
            if _unset_cost_control_mode_override:
                row.cost_control_mode_override = None
                changed = True
            elif cost_control_mode_override is not None:
                row.cost_control_mode_override = cost_control_mode_override
                changed = True
            if harness_override is not None:
                row.harness_override = harness_override
                changed = True
            if terminal_launch_args is not None:
                row.terminal_launch_args = json.dumps(terminal_launch_args)
                changed = True
            if changed:
                row.updated_at = now_epoch()
            return _to_conversation(row, _fetch_labels(session, conversation_id))

    def set_runner_id(self, conversation_id: str, runner_id: str) -> bool:
        """
        Pin a conversation to a runner via atomic
        ``UPDATE ... WHERE runner_id IS NULL``.

        See :meth:`ConversationStore.set_runner_id` for the
        contract. Implementation: a single ``UPDATE`` statement
        whose ``WHERE`` clause matches both the conversation id
        and ``runner_id IS NULL``. Concurrent first-dispatches
        racing to pin the same conversation are serialized by
        the database — exactly one wins, the other's UPDATE
        affects zero rows and returns ``False``. The caller can
        then re-read the row to discover the winning runner.

        :param conversation_id: Conversation to pin.
        :param runner_id: Runner UUID to pin to.
        :returns: ``True`` if this call won the race and
            transitioned the row from NULL → ``runner_id``;
            ``False`` if the row was already pinned or doesn't
            exist.
        """
        from sqlalchemy import update

        with self._session() as session:
            stmt = (
                update(SqlConversation)
                .where(SqlConversation.id == conversation_id)
                .where(SqlConversation.runner_id.is_(None))
                .values(runner_id=runner_id)
            )
            result = session.execute(stmt)
            return result.rowcount == 1

    def replace_runner_id(self, conversation_id: str, runner_id: str) -> Conversation:
        """
        Atomically overwrite ``conversations.runner_id``.

        Public ``PATCH /v1/sessions/{id}`` callers validate
        session-scoped agent ownership in the route before calling
        this method. Internal sub-agent code may also use this to
        rebind child conversations to their parent's current runner.

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param runner_id: New runner id, e.g. ``"runner_abc123"``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            exists for ``conversation_id``.
        """
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if row is None:
                raise ConversationNotFoundError(
                    f"conversation {conversation_id!r} does not exist",
                )
            row.runner_id = runner_id
            row.updated_at = now_epoch()
            return _to_conversation(row, _fetch_labels(session, conversation_id))

    def clear_runner_id(self, conversation_id: str) -> Conversation:
        """
        Null out ``conversations.runner_id``. Atomic last-write-wins.

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            exists for ``conversation_id``.
        """
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if row is None:
                raise ConversationNotFoundError(
                    f"conversation {conversation_id!r} does not exist",
                )
            row.runner_id = None
            row.updated_at = now_epoch()
            return _to_conversation(row, _fetch_labels(session, conversation_id))

    def clear_host_binding(self, conversation_id: str) -> Conversation:
        """
        NULL ``host_id``/``workspace``/``git_branch``/``runner_id`` together.

        Single-transaction full unbind — see
        :meth:`ConversationStore.clear_host_binding`. ``host_id`` and
        ``workspace`` are cleared together so the row never violates
        ``ck_conversations_workspace_required_for_host`` mid-update.

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            exists for ``conversation_id``.
        """
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if row is None:
                raise ConversationNotFoundError(
                    f"conversation {conversation_id!r} does not exist",
                )
            row.host_id = None
            row.workspace = None
            row.git_branch = None
            row.runner_id = None
            row.updated_at = now_epoch()
            return _to_conversation(row, _fetch_labels(session, conversation_id))

    def usage_totals_for_user(self, user_id: str) -> UsageTotals:
        """Sum session_usage across the user's OWNED top-level sessions.

        Cost is attributed to the owner (the ``LEVEL_OWNER`` grantee), so
        a user merely invited to a session is not charged for it. See
        base class.
        """
        from omnigent.db.db_models import SqlSessionPermission
        from omnigent.server.auth import LEVEL_OWNER

        with self._session() as session:
            owned_ids = select(SqlSessionPermission.conversation_id).where(
                SqlSessionPermission.user_id == user_id,
                SqlSessionPermission.level >= LEVEL_OWNER,
            )
            rows = (
                session.execute(
                    select(SqlConversation.session_usage).where(
                        SqlConversation.id.in_(owned_ids),
                        SqlConversation.kind == "default",
                    )
                )
                .scalars()
                .all()
            )
        cost = 0.0
        tokens = 0
        for raw in rows:
            if not raw:
                continue
            try:
                usage = json.loads(raw)
            except (ValueError, TypeError):
                continue
            cost += float(usage.get("total_cost_usd") or 0.0)
            tokens += int(usage.get("total_tokens") or 0)
        return UsageTotals(cost_usd=cost, total_tokens=tokens, session_count=len(rows))

    def list_conversations_by_host_id(
        self,
        host_id: str,
    ) -> list[Conversation]:
        """
        Return all conversations with the given ``host_id``.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :returns: List of :class:`Conversation` entities.
        """
        with self._session() as session:
            rows = session.query(SqlConversation).filter(SqlConversation.host_id == host_id).all()
            return [_to_conversation(row) for row in rows]

    def list_conversations_by_runner_id(
        self,
        runner_id: str,
    ) -> list[Conversation]:
        """
        Return all conversations bound to the given ``runner_id``.

        :param runner_id: Runner identifier, e.g.
            ``"runner_token_a1b2c3d4..."``.
        :returns: List of :class:`Conversation` entities.
        """
        with self._session() as session:
            rows = (
                session.query(SqlConversation).filter(SqlConversation.runner_id == runner_id).all()
            )
            return [_to_conversation(row) for row in rows]

    def set_host_id(
        self,
        conversation_id: str,
        host_id: str,
        workspace: str | None = None,
        git_branch: str | None = None,
    ) -> Conversation:
        """
        Set the host that launched (or should launch) the runner.

        Last-write-wins — mirrors :meth:`replace_runner_id`.

        ``workspace`` is updated together with ``host_id`` when
        provided so the row never violates
        ``ck_conversations_workspace_required_for_host`` mid-update.
        Callers that already populated ``workspace`` at session
        create can pass ``None`` to leave it untouched.

        :param conversation_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param workspace: Optional canonical absolute workspace
            path to set alongside ``host_id``, e.g.
            ``"/Users/corey/projects/myapp"``. ``None`` (default)
            leaves the existing workspace value untouched —
            useful when the workspace was set at session create.
        :param git_branch: Optional git branch checked out in a
            server-created worktree, e.g. ``"feature/login"``. Set
            together with ``host_id``/``workspace`` when binding an
            existing session to a freshly created worktree (the fork
            resume path). ``None`` (default) leaves it untouched.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            exists for ``conversation_id``.
        :raises IntegrityError: If the resulting row violates
            ``ck_conversations_workspace_required_for_host`` (i.e.
            ``host_id`` is being set on a row with no ``workspace``
            and the caller did not supply one).
        """
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if row is None:
                raise ConversationNotFoundError(
                    f"conversation {conversation_id!r} does not exist",
                )
            row.host_id = host_id
            if workspace is not None:
                row.workspace = workspace
            if git_branch is not None:
                row.git_branch = git_branch
            row.updated_at = now_epoch()
            return _to_conversation(row, _fetch_labels(session, conversation_id))

    def set_external_session_id(
        self,
        conversation_id: str,
        value: str,
    ) -> Conversation:
        """
        Persist the runtime-native session id this conversation wraps.

        Idempotent on same-value writes; raises ``ValueError`` on
        attempted overwrite of an existing different value. See
        :meth:`ConversationStore.set_external_session_id` for the
        full contract.

        :param conversation_id: Conversation to update, e.g.
            ``"conv_abc123"``.
        :param value: Runtime-native session id, e.g.
            ``"a1b2c3d4-..."``.
        :returns: The updated :class:`Conversation`.
        :raises ConversationNotFoundError: If no conversation row
            exists for ``conversation_id``.
        :raises ValueError: If the row already has a different
            ``external_session_id``.
        """
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if row is None:
                raise ConversationNotFoundError(
                    f"conversation {conversation_id!r} does not exist",
                )
            existing = row.external_session_id
            if existing is not None and existing != value:
                raise ValueError(
                    f"conversation {conversation_id!r} already has "
                    f"external_session_id={existing!r}; refusing to "
                    f"overwrite with {value!r}",
                )
            if existing != value:
                row.external_session_id = value
                row.updated_at = now_epoch()
            return _to_conversation(row, _fetch_labels(session, conversation_id))

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
        Atomically insert a conversation row and session-scoped agent.

        The two rows share one managed SQLAlchemy session, so the
        context manager commits them together on success and rolls
        both back on any exception. The insert order creates the
        conversation with ``agent_id=NULL``, creates the agent with
        ``session_id`` pointing at that conversation, then backfills
        ``conversations.agent_id``.

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
            session for display, e.g.
            ``"/Users/corey/projects/myapp"``. CLI-launched
            sessions populate this with ``os.getcwd()``;
            multipart bundle uploads from the Web UI may pass
            ``None``. ``None`` is allowed because this path
            doesn't set ``host_id`` (so the
            ``ck_conversations_workspace_required_for_host``
            constraint isn't active).
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
            inherit the parent's binding through this field so
            runner dispatch remains explicit in store state.
        :returns: A :class:`CreatedSession` with both entities.
        :raises ConversationNotFoundError: If
            ``parent_conversation_id`` is set but no such
            conversation exists.
        """
        from omnigent.stores.conversation_store import ConversationNotFoundError

        now = now_epoch()
        conversation_id = generate_conversation_id()
        with self._session() as session:
            root_conversation_id: str | None = None
            if parent_conversation_id is not None:
                parent_row = session.get(SqlConversation, parent_conversation_id)
                if parent_row is None:
                    raise ConversationNotFoundError(
                        f"parent conversation {parent_conversation_id!r} does not exist"
                    )
                # Inherit the parent's root: nested sub-agents all
                # share the same root with their top-level ancestor.
                root_conversation_id = parent_row.root_conversation_id
            conversation_row = _new_session_conversation_row(
                conversation_id,
                now,
                title,
                reasoning_effort,
                workspace,
                terminal_launch_args,
                parent_conversation_id=parent_conversation_id,
                root_conversation_id=root_conversation_id,
                runner_id=runner_id,
            )
            session.add(conversation_row)
            session.flush()

            agent_row = _new_session_agent_row(
                agent_id=agent_id,
                agent_name=agent_name,
                agent_bundle_location=agent_bundle_location,
                agent_description=agent_description,
                conversation_id=conversation_id,
                now=now,
            )
            session.add(agent_row)
            session.flush()

            conversation_row.agent_id = agent_id
            if labels:
                _upsert_labels(session, conversation_id, labels, now)
            session.flush()

            return _created_session_from_rows(conversation_row, agent_row, labels)

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
        model_override: str | None = None,
        carry_history_into_native: bool = False,
        resume_source_native_session: bool = True,
        presentation_labels: dict[str, str] | None = None,
        up_to_response_id: str | None = None,
    ) -> Conversation:
        """
        Deep-copy a conversation and its items into a new conversation.

        Reads the source conversation and all its items in one
        transaction, creates a new top-level ``SqlConversation``
        (``kind="default"``, ``parent_conversation_id=None``)
        with the source's ``reasoning_effort``,
        ``terminal_launch_args``, and (unless overridden)
        ``agent_id``, copies each item with a fresh ID and position,
        and inserts FTS records for each copied item. Identity-bound
        columns (``external_session_id``, ``workspace``,
        ``git_branch``) are deliberately NOT copied — a fork is a
        fresh session that re-binds those on its own launch. Source
        labels are copied EXCEPT instance-scoped ones
        (:data:`_INSTANCE_SCOPED_LABEL_KEYS` — native bridge ids,
        context metrics), which belong to the source's running instance
        and would mis-route or mis-display on the clone.
        When the source had a ``workspace``, the fork is additionally
        stamped with ``FORK_SOURCE_LABEL_KEY`` (value = source id) so the
        unbound clone reports offline until it rebinds a directory (see
        :class:`SessionConnectivity`).

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
            bundle into a new session-scoped agent row (id
            ``agent_id``) created atomically in this transaction, so a
            fork failure rolls it back instead of orphaning a
            ``session_id IS NULL`` built-in. ``None`` keeps the legacy
            bind-existing behavior.
        :param cloned_agent_description: Optional description for the
            cloned agent row. Ignored unless
            ``cloned_agent_bundle_location`` is set.
        :param copy_model_settings: When ``True`` (default), copy the
            source's ``model_override`` and ``reasoning_effort``. When
            ``False``, both are left ``None`` so the fork falls back to
            the bound agent's defaults — used when the fork switches to
            an agent in a different provider family, where the source's
            model id is meaningless (a model is provider-bound).
        :param model_override: When set, the fork's ``model_override`` is
            this value instead of the source's copied one — the
            "restart with model" path, where the whole point is to launch
            the clone on a different model. Wins over the
            ``copy_model_settings`` copy; ``reasoning_effort`` still follows
            ``copy_model_settings`` (a same-family model switch keeps the
            effort). ``None`` (default) leaves the copy behavior unchanged.
        :param carry_history_into_native: When ``True``, stamp
            :data:`FORK_CARRY_HISTORY_LABEL_KEY` on the fork so a native
            target harness rebuilds its transcript instead of starting
            fresh. Set by the route only for native targets whose harness can
            replay fork history.
        :param resume_source_native_session: When ``True`` (default), a
            full fork of a source with a native session stamps
            :data:`FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY` so the runner
            clones the source's local native transcript. ``False`` on a
            cross-family agent switch: the source's native transcript is
            the wrong format for the target harness, so the directive is
            skipped and the runner builds the native transcript from the
            copied Omnigent items instead.
        :param presentation_labels: When not ``None``, drop the source's
            ``omnigent.ui`` / ``omnigent.wrapper`` labels from the clone
            and apply these instead, so the clone's Web UI mode matches the
            switched-to TARGET harness (native → ``{ui: terminal, wrapper:
            ...}``; SDK → ``{}``). ``None`` keeps the copied labels (same-
            agent fork).
        :param up_to_response_id: When set, copy only the items up to and
            including the last item of this response (by position), e.g.
            ``"resp_abc123"`` — a "fork from this response" truncation.
            A truncated fork skips the
            :data:`FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY` directive so a
            native target rebuilds its transcript from the truncated
            items (the carry-history fork-rebuild path) instead of
            resuming the source's full native transcript; when the
            response is the source's last one the copy is equivalent to a
            full fork, so the directive is kept. ``None`` (default)
            copies the full history.
        :returns: The newly created :class:`Conversation`.
        :raises LookupError: If no conversation with
            *source_conversation_id* exists.
        :raises ValueError: If *up_to_response_id* is set but no item in
            the source conversation has that ``response_id``.
        """
        now = now_epoch()
        with self._session() as session:
            source = session.get(SqlConversation, source_conversation_id)
            if source is None:
                raise LookupError(f"conversation not found: {source_conversation_id!r}")

            fork_title = (
                title
                if title is not None
                else (
                    f"Fork of {source.title}"
                    if source.title
                    else f"Fork of {source_conversation_id[:16]}…"
                )
            )
            # Cloning the agent in-transaction: start the conversation with
            # agent_id=NULL (the row doesn't exist yet — an autoflush would
            # else break the agent_id FK) and backfill after inserting it.
            creating_clone = cloned_agent_bundle_location is not None
            new_conv_id = generate_conversation_id()
            new_conv = SqlConversation(
                id=new_conv_id,
                created_at=now,
                updated_at=now,
                title=fork_title,
                kind="default",
                # A fork is a fresh top-level conversation, so its
                # root mirrors its own id (matches the
                # ``_new_session_conversation_row`` invariant).
                root_conversation_id=new_conv_id,
                agent_id=(
                    None
                    if creating_clone
                    else (agent_id if agent_id is not None else source.agent_id)
                ),
                reasoning_effort=source.reasoning_effort if copy_model_settings else None,
                # An explicit override wins over the copied value — this is
                # the "restart with model" launch model. Otherwise fall back
                # to the source's copied model (gated by copy_model_settings).
                model_override=(
                    model_override
                    if model_override is not None
                    else (source.model_override if copy_model_settings else None)
                ),
                # The brain-harness override is family-bound like the model,
                # so it follows the same copy gate.
                harness_override=source.harness_override if copy_model_settings else None,
                # Raw column-to-column copy of the JSON text; the
                # fork should launch with the same native args.
                terminal_launch_args=source.terminal_launch_args,
            )
            session.add(new_conv)

            # Resolve the truncation cutoff: the position of the LAST item
            # of the selected response, so the fork never ends mid-turn.
            # When the selected response is also the conversation's last
            # one, the "truncation" copies everything — treat it as a full
            # fork (``truncated`` stays False) so the native fork-resume
            # directive below is preserved and the runner can still clone
            # the source's native transcript verbatim.
            truncated = False
            cutoff_position: int | None = None
            if up_to_response_id is not None:
                cutoff_position = session.execute(
                    select(func.max(SqlConversationItem.position)).where(
                        SqlConversationItem.conversation_id == source_conversation_id,
                        SqlConversationItem.response_id == up_to_response_id,
                    )
                ).scalar_one()
                if cutoff_position is None:
                    raise ValueError(
                        f"response not found in conversation "
                        f"{source_conversation_id!r}: {up_to_response_id!r}"
                    )
                last_position = session.execute(
                    select(func.max(SqlConversationItem.position)).where(
                        SqlConversationItem.conversation_id == source_conversation_id
                    )
                ).scalar_one()
                truncated = cutoff_position < last_position

            # Copy items ordered by position so the fork preserves
            # the original chronological order.
            items_query = (
                select(SqlConversationItem)
                .where(SqlConversationItem.conversation_id == source_conversation_id)
                .order_by(SqlConversationItem.position.asc())
            )
            if cutoff_position is not None:
                items_query = items_query.where(SqlConversationItem.position <= cutoff_position)
            source_items = session.execute(items_query).scalars().all()

            for pos, src_item in enumerate(source_items):
                new_item_id = generate_item_id(src_item.type)
                new_item = SqlConversationItem(
                    id=new_item_id,
                    conversation_id=new_conv.id,
                    response_id=src_item.response_id,
                    created_at=now,
                    status=src_item.status,
                    position=pos,
                    type=src_item.type,
                    data=src_item.data,
                    search_text=src_item.search_text,
                    created_by=src_item.created_by,
                )
                session.add(new_item)
                insert_fts(
                    session,
                    new_item_id,
                    new_conv.id,
                    src_item.search_text or "",
                )

            # The clone copied len(source_items) items at dense positions
            # 0..N-1, so its position allocator starts at N. Seed it from the
            # snapshot (not the source row's counter) so the fork is correct
            # even when the source predates the counter.
            new_conv.next_position = len(source_items)

            # Create/bind the fork's session-scoped agent atomically.
            if creating_clone:
                # Mint the clone here so it's born with session_id set (never
                # NULL) and rolls back with the fork on failure — never
                # leaking as a phantom built-in.
                assert (
                    agent_id is not None
                    and cloned_agent_name is not None
                    and cloned_agent_bundle_location is not None
                )
                session.add(
                    _new_session_agent_row(
                        agent_id=agent_id,
                        agent_name=cloned_agent_name,
                        agent_bundle_location=cloned_agent_bundle_location,
                        agent_description=cloned_agent_description,
                        conversation_id=new_conv.id,
                        now=now,
                    )
                )
                session.flush()
                new_conv.agent_id = agent_id
            elif agent_id is not None:
                agent_row = session.get(SqlAgent, agent_id)
                if agent_row is not None:
                    agent_row.session_id = new_conv.id

            # Copy labels from the source conversation, minus the
            # instance-scoped ones (native bridge ids, context metrics)
            # — those belong to the source's running instance and would
            # mis-route or mis-display on the clone
            # (see _INSTANCE_SCOPED_LABEL_KEYS). When the source had a
            # working directory, also stamp the fork-source label: the
            # clone is unbound (workspace/host not copied) and must rebind
            # a directory before it can run, so the online-dot reports it
            # offline and the UI opens the directory picker on the first
            # message instead of dropping it. Forks of chat-only sources
            # (no workspace) get no such label and resume in-process like
            # a brand-new chat session.
            fork_labels = {
                key: value
                for key, value in _fetch_labels(session, source_conversation_id).items()
                if key not in _INSTANCE_SCOPED_LABEL_KEYS
            }
            if source.workspace is not None:
                fork_labels[FORK_SOURCE_LABEL_KEY] = source_conversation_id
            # Carry the source's native session id as a one-shot fork
            # directive so a native harness can resume + branch the source's
            # local transcript into the clone (see
            # FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY). external_session_id
            # itself stays NULL — the clone isn't that session yet. A
            # TRUNCATED fork must not resume the source's full transcript,
            # and a CROSS-FAMILY fork can't (wrong transcript format —
            # ``resume_source_native_session=False``); in both cases the
            # directive is skipped so the runner's carry-history
            # fork-rebuild path synthesizes the native transcript from the
            # copied items instead.
            if source.external_session_id and not truncated and resume_source_native_session:
                fork_labels[FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY] = source.external_session_id
            # When the fork binds a native target, mark it so the runner
            # rebuilds the native transcript (clone the source's native
            # transcript when same-family, else build from the copied
            # Omnigent items) rather than launching fresh (see
            # FORK_CARRY_HISTORY_LABEL_KEY).
            if carry_history_into_native:
                fork_labels[FORK_CARRY_HISTORY_LABEL_KEY] = "1"
            # On an agent switch, the harness-presentation labels
            # (omnigent.ui / omnigent.wrapper) must reflect the TARGET
            # harness, not the source's: copying the source's would leave an
            # SDK clone of a claude-native session wrongly in terminal-first
            # mode (a stale interactive terminal + the source's transcript).
            # Drop the source's and apply the route-computed target labels.
            if presentation_labels is not None:
                for _pkey in (UI_MODE_LABEL_KEY, WRAPPER_LABEL_KEY):
                    fork_labels.pop(_pkey, None)
                fork_labels.update(presentation_labels)
            if fork_labels:
                _upsert_labels(session, new_conv.id, fork_labels, now)

            return _to_conversation(new_conv, fork_labels)

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

        See :meth:`ConversationStore.switch_conversation_agent` for the
        full contract. Mutates the same conversation row in one
        transaction: deletes the current session-scoped agent, creates
        the new one, repoints ``agent_id``, resets model settings on a
        cross-family switch, clears ``external_session_id``, and
        replaces the harness-presentation / carry-history labels.

        :param conversation_id: Session to switch, e.g. ``"conv_abc123"``.
        :param new_agent_id: Pre-generated id for the new agent row.
        :param new_agent_name: Name for the new agent row.
        :param new_agent_bundle_location: Artifact-store key to clone.
        :param new_agent_description: Optional spec description.
        :param copy_model_settings: Keep model settings when ``True``,
            else reset to ``None`` (cross-family switch).
        :param carry_history_into_native: Stamp / clear
            :data:`FORK_CARRY_HISTORY_LABEL_KEY`.
        :param presentation_labels: Target-harness ui/wrapper labels.
        :param previous_builtin_id: Built-in switched away from, or
            ``None``.
        :returns: The updated :class:`Conversation`.
        :raises LookupError: If *conversation_id* does not exist.
        """
        now = now_epoch()
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if row is None:
                raise LookupError(f"conversation not found: {conversation_id!r}")

            # Replace the session-scoped agent. Ordering matters for two
            # constraints: (1) ``conversations.agent_id`` → ``agents.id`` is
            # ON DELETE CASCADE, so deleting the old agent while the row still
            # references it would cascade-delete the WHOLE conversation; null
            # the reference first. (2) ``ix_agents_session_id`` is UNIQUE, so
            # the old agent must be gone before the new one claims
            # ``session_id``. Hence: null agent_id → delete old → insert new →
            # repoint agent_id. The delete is guarded on
            # ``session_id == conversation_id`` so a (mistakenly bound)
            # built-in agent is never deleted.
            old_agent_id = row.agent_id
            row.agent_id = None
            session.flush()
            if old_agent_id is not None:
                old_agent = session.get(SqlAgent, old_agent_id)
                if old_agent is not None and old_agent.session_id == conversation_id:
                    session.delete(old_agent)
                    session.flush()

            new_agent = _new_session_agent_row(
                agent_id=new_agent_id,
                agent_name=new_agent_name,
                agent_bundle_location=new_agent_bundle_location,
                agent_description=new_agent_description,
                conversation_id=conversation_id,
                now=now,
            )
            session.add(new_agent)
            session.flush()

            row.agent_id = new_agent_id
            # A model id is provider-bound, so a cross-family switch resets
            # both; a same-family switch keeps the session's current values.
            if not copy_model_settings:
                row.model_override = None
                row.reasoning_effort = None
            # The harness override belonged to the OLD agent's brain; the
            # new agent runs on its own spec-declared harness.
            row.harness_override = None
            # The native runtime session belongs to the OLD harness. Clearing
            # it makes the next turn cold-start the NEW harness, which rebuilds
            # the native transcript from this session's own AP items when
            # ``carry_history_into_native`` stamped the carry-history label.
            row.external_session_id = None
            row.updated_at = now

            # Replace the label set. Drop instance-scoped labels (old
            # harness's bridge id, stopped marker, context metrics) and any
            # stale fork directives, then re-derive the harness-presentation
            # and carry-history labels for the TARGET. Labels removed here
            # must be DELETEd — ``_upsert_labels`` only inserts/updates.
            existing = _fetch_labels(session, conversation_id)
            drop_keys = (
                set(_INSTANCE_SCOPED_LABEL_KEYS)
                | {FORK_SOURCE_LABEL_KEY, FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY}
                | {UI_MODE_LABEL_KEY, WRAPPER_LABEL_KEY}
                # Always drop the previous-builtin pointer, then re-stamp below
                # only when this switch supplies one — otherwise a stale pointer
                # from an earlier switch survives and offers the wrong "switch
                # back" target (the label is overwritten on each switch).
                | {SWITCH_PREVIOUS_BUILTIN_LABEL_KEY}
            )
            if not carry_history_into_native:
                drop_keys.add(FORK_CARRY_HISTORY_LABEL_KEY)
            present_drop = [key for key in drop_keys if key in existing]
            if present_drop:
                session.execute(
                    delete(SqlConversationLabel).where(
                        SqlConversationLabel.conversation_id == conversation_id,
                        SqlConversationLabel.key.in_(present_drop),
                    )
                )

            upserts: dict[str, str] = dict(presentation_labels)
            if carry_history_into_native:
                upserts[FORK_CARRY_HISTORY_LABEL_KEY] = "1"
            if previous_builtin_id is not None:
                upserts[SWITCH_PREVIOUS_BUILTIN_LABEL_KEY] = previous_builtin_id
            if upserts:
                _upsert_labels(session, conversation_id, upserts, now)

            return _to_conversation(row, _fetch_labels(session, conversation_id))

    async def delete_conversation(self, conversation_id: str) -> bool:
        """
        Delete a conversation, its items, related tasks, and FTS
        records.

        Deletes in FK-safe order: tasks, FTS records, items,
        then the conversation itself.

        :param conversation_id: Unique conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` if the conversation existed,
            ``False`` otherwise.
        """
        with self._session() as session:
            row = session.get(SqlConversation, conversation_id)
            if not row:
                return False
            # Delete conversation items and FTS before the conversation row
            # (FK constraints: items reference the conversation).
            delete_fts_by_conversation(session, conversation_id)
            session.execute(
                delete(SqlConversationItem).where(
                    SqlConversationItem.conversation_id == conversation_id
                )
            )
            session.delete(row)
            return True
