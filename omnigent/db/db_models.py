"""SQLAlchemy table definitions for the omnigent database."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all omnigent tables."""


class SqlAgent(Base):
    """
    SQLAlchemy model for the ``agents`` table.

    Each row represents a registered agent in the system.

    :param id: Unique agent identifier, e.g. ``"ag_0f1a2b3c..."``.
    :param created_at: Unix epoch seconds when the agent was created.
    :param name: Human-readable agent name. Registered template
        agents require unique names; session-scoped copies may reuse
        the same name across different sessions.
    :param bundle_location: Artifact store key for the current bundle.
        Content-addressed (SHA-256 hex), e.g.
        ``"ag_abc123/a1b2c3d4e5f6..."``.
    :param version: Monotonic version counter. Starts at 1, incremented
        on each update via ``PUT /api/agents/{id}``.
    :param description: Optional free-text description of the agent's
        purpose. ``None`` when not provided.
    :param updated_at: Unix epoch seconds of the last update, or
        ``None`` if the agent has never been updated.
    :param session_id: Owning conversation/session id for a
        session-scoped agent. ``None`` for template agents uploaded
        through ``POST /api/agents``.
    """

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(256))
    bundle_location: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_agents_created_at", "created_at"),
        Index(
            "ix_agents_template_name",
            "name",
            unique=True,
            sqlite_where=text("session_id IS NULL"),
            postgresql_where=text("session_id IS NULL"),
        ),
        Index("ix_agents_session_id", "session_id", unique=True),
    )


class SqlFile(Base):
    """
    SQLAlchemy model for the ``files`` table.

    Each row represents an uploaded file tracked by the system.

    :param id: Unique file identifier, e.g. ``"file_a1b2c3d4..."``.
    :param created_at: Unix epoch seconds when the file record was
        created.
    :param filename: Original filename as provided by the uploader,
        max 512 characters. e.g. ``"report.pdf"``.
    :param bytes: Size of the file in bytes.
    :param content_type: MIME type of the file, e.g.
        ``"application/pdf"``. ``None`` when not provided.
    """

    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_files_created_at", "created_at"),
        Index("ix_files_session_id_created_at", "session_id", "created_at", "id"),
    )


class SqlUser(Base):
    """
    SQLAlchemy model for the ``users`` table.

    Each row represents a user. In header / OIDC modes, ``id`` is
    the upstream identity (email or ``"local"``); the row is
    upserted on first sight and ``password_hash`` stays ``NULL``.
    In ``accounts`` mode, rows are created explicitly by the admin
    or via invite redemption with a populated ``password_hash``.

    :param id: User identifier — email in header/OIDC modes, chosen
        username in accounts mode, ``"local"`` in single-user.
    :param is_admin: When ``True``, the user bypasses all
        permission checks. ``False`` by default.
    :param password_hash: argon2id hash of the user's password.
        ``NULL`` for users created via header/OIDC modes (their
        password is the upstream IdP's).
    :param created_at: Unix epoch seconds when the row was inserted.
        Populated for accounts-mode users; ``NULL`` for legacy rows
        backfilled by the original permissions migration.
    :param last_login_at: Unix epoch seconds of the most recent
        successful ``/auth/login`` (accounts mode). ``NULL`` until
        the first login.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_login_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SqlAccountToken(Base):
    """
    SQLAlchemy model for the ``account_tokens`` table.

    Backs both invite tokens (admin-issued, allow self-serve
    registration) and magic-login tokens (CLI-minted, hand off a
    signed-in session into the web UI). Both have the same
    short-TTL single-use lifecycle, so they share one table.

    :param id: Opaque random token string (43+ URL-safe base64
        chars). This is the secret — the user presents it as a
        query param. Stored verbatim because we need
        constant-time lookup; rotation = delete + recreate.
    :param kind: ``"invite"`` (anyone can redeem; creates a new
        user) or ``"magic"`` (the bound ``user_id`` is signed in).
    :param user_id: For ``magic``, the user the token signs in as.
        For ``invite``, ``NULL`` (the username is chosen at
        redemption time).
    :param created_by: User id of the admin who issued an invite
        (``NULL`` for magic tokens, which are self-issued).
    :param created_at: Unix epoch seconds when the token was
        minted. ``expires_at = created_at + ttl_seconds``.
    :param expires_at: Unix epoch seconds when the token stops
        being redeemable. Single-use enforcement is via
        ``redeemed_at``, this just bounds the window.
    :param redeemed_at: Unix epoch seconds when the token was
        consumed. ``NULL`` until then. After being set, the token
        is dead — redeem checks this column atomically.
    :param invited_is_admin: For invite tokens, whether the
        resulting user should be created with admin rights. False
        for magic tokens.
    """

    __tablename__ = "account_tokens"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    redeemed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invited_is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())

    __table_args__ = (
        CheckConstraint("kind IN ('invite', 'magic')", name="ck_account_tokens_kind"),
        Index("ix_account_tokens_expires_at", "expires_at"),
    )


class SqlSessionPermission(Base):
    """
    SQLAlchemy model for the ``session_permissions`` table.

    Junction table mapping ``(user_id, conversation_id)`` to a
    numeric permission level. PK is ``(user_id, conversation_id)``
    — optimized for the hot path ("list sessions I can access"
    = prefix scan on ``user_id``).

    The ``"__public__"`` sentinel ``user_id`` represents public
    read access to a session.

    :param user_id: The grantee, e.g. ``"alice@example.com"``
        or ``"__public__"`` for public access.
    :param conversation_id: The session being shared, e.g.
        ``"conv_e4f5a6b7..."``.
    :param level: Numeric permission level: ``1`` = read,
        ``2`` = edit, ``3`` = manage. Each level subsumes the
        ones below it (comparison is ``>=``).
    """

    __tablename__ = "session_permissions"

    user_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("level IN (1, 2, 3, 4)", name="ck_session_permissions_level"),
        Index("ix_session_permissions_conversation_id", "conversation_id"),
    )


class SqlConversation(Base):
    """
    SQLAlchemy model for the ``conversations`` table.

    Each row represents a conversation thread that contains one or
    more conversation items.

    :param id: Unique conversation identifier, e.g.
        ``"conv_e4f5a6b7..."``.
    :param created_at: Unix epoch seconds when the conversation was
        created.
    :param updated_at: Unix epoch seconds when the conversation was
        last updated (item append, title change, etc.).
    :param title: Optional human-readable title for the conversation.
        ``None`` when not provided.
    :param kind: Conversation type. ``"default"`` for user-initiated,
        ``"sub_agent"`` for sub-agent execution conversations.
    :param parent_conversation_id: For Phase 4 named sub-agents,
        points at the parent conversation. ``None`` for top-level
        conversations. ``ON DELETE CASCADE`` so removing a parent
        cleans up the entire sub-tree.
    :param root_conversation_id: Id of the root (top-level)
        conversation in the spawn tree. Equal to ``id`` for
        top-level conversations. Indexed so ``sys_session_get_history`` /
        ``sys_session_close`` can verify that a target
        ``conversation_id`` lives in the caller's tree in O(1) —
        any agent in the tree can address any other by
        ``conversation_id``. ``ON DELETE CASCADE`` to keep it
        consistent with ``parent_conversation_id`` when a root is
        deleted.
    :param agent_id: Foreign key to the agent bound to this
        conversation at creation time. ``None`` for legacy
        conversations created without an agent binding (these are
        excluded from ``GET /v1/sessions`` results).
    :param runner_id: Runner the conversation is pinned to (hard
        affinity per ``designs/RUNNER.md`` §5). ``None`` until the
        first dispatch claims a runner; thereafter every subsequent
        dispatch routes to this runner while it is online (or fails
        with ``runner_unavailable`` if it isn't). No FK because
        runner records are not persisted in v1 — the registry is
        purely in-memory.
    :param external_session_id: Runtime-native session id this
        conversation wraps, e.g. Claude Code's session uuid for
        ``omnigent claude`` sessions. ``None`` for regular
        AP-only conversations. Populated by the wrapper bridge
        from the underlying runtime and used by ``--resume`` to
        recover the external session's prior transcript. Generic
        across runtimes — at most one external session per
        conversation. No FK because the id is generated externally
        (by Claude Code, Codex, Pi, etc.) and is not tracked in
        any AP-side table.
    :param workspace: Absolute path on disk where the runner should
        start, e.g. ``"/Users/corey/universe/src/foo"``. Required
        when ``host_id`` is set (enforced by check constraint
        ``ck_conversations_workspace_required_for_host``); optional
        for CLI-launched sessions that record their starting cwd
        for display. Stored as the canonicalized realpath returned
        by ``host.stat`` at session-create time; runtime symlinks
        are pre-resolved so the boundary check on the agent's
        ``os_env.cwd`` cannot be smuggled past via a symlink.
        Immutable after creation —
        designs/SESSION_WORKSPACE_SELECTION.md. When a git worktree
        was created for the session, this is the worktree directory
        path rather than the picked source repo.
    :param git_branch: Git branch checked out in the session's
        worktree, e.g. ``"feature/login"``. Set only when the
        session was created with a server-created git worktree;
        ``None`` otherwise. ``git_branch IS NOT NULL`` gates worktree
        cleanup on session delete. See
        designs/SESSION_GIT_WORKTREE.md.
    :param archived: Whether the session is archived. Archived
        sessions are hidden from the default ``GET /v1/sessions``
        listing (and the sidebar); the listing returns them only when
        ``include_archived=True``. ``False`` for normal sessions.
        Reversible via ``PATCH /v1/sessions/{id}``.
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="default")
    parent_conversation_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )
    root_conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
    )
    runner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Host that launched (or should launch) the runner for this
    # session. Set when a session is created via the Web UI on a
    # specific host. FK to hosts.host_id (a unique column); ON DELETE
    # SET NULL so removing a host clears the binding rather than
    # orphaning it — and host_id -> NULL keeps the
    # workspace-required CHECK below satisfied.
    host_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("hosts.host_id", ondelete="SET NULL"),
        nullable=True,
    )
    # Per-session reasoning-effort hint, e.g. "high". Nullable;
    # None means use the agent default.
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Per-session LLM model override, e.g. "claude-opus-4-7". Nullable;
    # None means use the agent default from the spec.
    model_override: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Per-session cost-control switch: "on" | "off". Nullable; None
    # means use the spec default (see entities.Conversation).
    cost_control_mode_override: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Per-session brain-harness override, e.g. "pi". Nullable; None
    # means use the spec's executor.config.harness (see entities.Conversation).
    harness_override: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Sub-agent type name within the parent's spec tree, e.g.
    # "summarizer". The runner uses this to load the sub-agent's
    # AgentSpec instead of the parent's. Replaces task.agent_name
    # from the removed task store. None for top-level sessions.
    sub_agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Monotonic allocator for the next item position in this conversation.
    # append() reads and advances this instead of scanning
    # MAX(SqlConversationItem.position) on every write, making position
    # assignment O(1) and collision-free under the conversation lock. New rows
    # start at 0 (column default); NULL marks a row created before this column
    # existed, which append() backfills via a one-time scan on its next write.
    next_position: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # JSON-serialized mutable per-conversation key/value store
    # used by policy callables to accumulate state across turns.
    # NULL when no policy has written state yet; empty JSON object
    # "{}" is equivalent. Stored as Text (not a native JSON column)
    # for SQLite compatibility.
    session_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-serialized cumulative LLM token usage for policy
    # callables. Shape: {"input_tokens": N, "output_tokens": M,
    # "total_tokens": T, "cache_read_input_tokens": C1,
    # "cache_creation_input_tokens": C2, "total_cost_usd": X}.
    # NULL when no LLM calls have been recorded yet.
    session_usage: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Pass-through CLI args for a native terminal wrapper (claude /
    # codex), JSON-encoded list of strings, e.g.
    # '["--dangerously-skip-permissions"]'. NULL for non-native
    # sessions. The runner reconstructs the terminal launch command
    # from these plus the harness binary; the command itself and all
    # bridge / AP-URL / auth wiring are runner-owned and never stored
    # here. A flat list (not a dict) is deliberate: there is no key for
    # a user to smuggle internal wiring through. See
    # designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    terminal_launch_args: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Absolute path on the host where the runner cd's. Required
    # when host_id is set; CHECK constraint below. When a git worktree
    # was created for the session, this is the worktree directory path.
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Git branch checked out in the session's worktree, e.g.
    # "feature/login". Set only when the session was created with a
    # server-created git worktree; None otherwise. Gates worktree
    # cleanup on delete. See designs/SESSION_GIT_WORKTREE.md.
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Whether the session is archived (hidden from the default
    # /v1/sessions listing and the sidebar). False for normal
    # sessions; server_default false backfills existing rows on the
    # migration that adds this column. Low-cardinality, so no index —
    # the listing's accessible_by subquery is the selective filter.
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('default', 'sub_agent')", name="ck_conversations_kind"),
        CheckConstraint(
            "host_id IS NULL OR workspace IS NOT NULL",
            name="ck_conversations_workspace_required_for_host",
        ),
        Index("ix_conversations_created_at", "created_at"),
        Index("ix_conversations_updated_at", "updated_at"),
        Index("ix_conversations_kind", "kind"),
        # Reconnect reconciliation queries conversations by host_id on
        # every host reconnect; index it to avoid a full scan.
        Index("ix_conversations_host_id", "host_id"),
        Index("ix_conversations_root_conversation_id", "root_conversation_id"),
        # Phase 4: partial unique index on (parent_conversation_id,
        # title) prevents two same-named children under the same
        # parent (G36 race protection at the DB layer). The
        # ``sqlite_where`` / ``postgresql_where`` clauses scope the
        # index so multiple top-level conversations (NULL parent)
        # remain valid.
        Index(
            "ix_conversations_parent_title_unique",
            "parent_conversation_id",
            "title",
            unique=True,
            sqlite_where=text("parent_conversation_id IS NOT NULL"),
            postgresql_where=text("parent_conversation_id IS NOT NULL"),
        ),
        # Partial composite index for child-session listing
        # (list_conversations(kind="sub_agent", parent_conversation_id=...)).
        Index(
            "idx_conversations_parent",
            "parent_conversation_id",
            text("created_at DESC"),
            text("id DESC"),
            sqlite_where=text("kind = 'sub_agent'"),
            postgresql_where=text("kind = 'sub_agent'"),
        ),
    )


class SqlConversationItem(Base):
    """
    SQLAlchemy model for the ``conversation_items`` table.

    Each row represents a single item (message, function call,
    function call output, or reasoning block) within a conversation.

    :param id: Unique item identifier with a type-based prefix,
        e.g. ``"msg_a1b2c3..."``, ``"fc_d4e5f6..."``.
    :param conversation_id: Foreign key to
        :class:`SqlConversation.id`. Cascades on delete.
    :param response_id: The task/response ID this item belongs to,
        e.g. ``"resp_d8e9f0a1..."``.
    :param created_at: Unix epoch seconds when the item was created.
    :param status: Item status string. Defaults to ``"completed"``.
    :param position: Zero-based ordering index within the
        conversation. Used for deterministic item ordering.
    :param type: Item type discriminator, one of ``"message"``,
        ``"function_call"``, ``"function_call_output"``,
        ``"reasoning"``.
    :param data: JSON-serialized item payload. Structure varies by
        ``type``.
    :param search_text: Plain-text extraction of ``data`` used for
        full-text search indexing.
    :param created_by: Identity of the human actor who authored the
        item, or ``None`` for agent/tool/system items and single-user
        mode. Mirrors :class:`SqlComment.created_by`.
    """

    __tablename__ = "conversation_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    response_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    position: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(32))
    data: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index(
            "ix_conversation_items_conversation_id_position",
            "conversation_id",
            "position",
            unique=True,
        ),
        Index("ix_conversation_items_response_id", "response_id"),
    )


# Width of the ``conversation_labels.value`` column. Exported so the store
# (and the session-status error-label path) can clamp values to fit instead
# of letting an over-length write raise ``DataError`` on PostgreSQL.
LABEL_VALUE_MAX_LEN = 256


class SqlConversationLabel(Base):
    """
    SQLAlchemy model for the ``conversation_labels`` table.

    One row per (conversation, label-key) pair. Labels live in
    a dedicated table rather than a JSON column on
    ``conversations`` so per-key UPDATEs are atomic without
    read-modify-write (see POLICIES.md §6). The table is keyed
    only by ``conversation_id`` + ``key``, so it is untouched
    by compaction (which rewrites ``conversation_items``) —
    labels set turn 3 still exist turn 20 even after the
    earlier turns have been folded into a summary.

    :param conversation_id: The conversation this label belongs
        to. Composite PK member. Deleted with the conversation
        via ``ON DELETE CASCADE``.
    :param key: The label key, e.g. ``"integrity"``,
        ``"sensitivity"``. Composite PK member.
    :param value: The label value as a string, e.g. ``"0"``,
        ``"confidential"``. All label values are string-typed
        regardless of what the YAML author wrote — the parser
        coerces scalar / list values during spec load
        (POLICIES.md §14).
    :param updated_at: Unix epoch seconds of the last write.
        Single timestamp for each row; on UPSERT the row's
        timestamp is refreshed even when the value is
        unchanged (matches omnigent parity and keeps
        debugging timelines accurate).
    """

    __tablename__ = "conversation_labels"

    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(LABEL_VALUE_MAX_LEN))
    updated_at: Mapped[int] = mapped_column(Integer)


class SqlComment(Base):
    """SQLAlchemy model for the ``comments`` table.

    Stores per-review comments associated with a conversation.
    Each comment is anchored to a character range in the file expressed
    as absolute document-level offsets. Comments survive server restarts
    and are cleaned up when the owning conversation is deleted.

    :param id: UUID primary key, e.g. ``"a1b2c3d4-..."``.
    :param conversation_id: The conversation this comment belongs to.
    :param path: File path relative to the workspace root,
        e.g. ``"src/App.tsx"``.
    :param start_index: 0-based absolute character offset (inclusive)
        within the file where the anchor range begins.
    :param end_index: 0-based absolute character offset (exclusive)
        within the file where the anchor range ends.
    :param body: The comment text.
    :param status: One of ``"draft"``, ``"addressed"``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch **microseconds** of the last
        body/status mutation; set at creation for never-edited
        comments. Feeds the per-session comments fingerprint surfaced
        on ``GET /v1/sessions`` so clients can detect comment changes;
        microsecond precision keeps back-to-back mutations within one
        second distinguishable while remaining an exact integer in
        JavaScript. ``BigInteger`` because epoch-µs overflows a
        32-bit column on PostgreSQL.
    :param anchor_content: Plain-text snapshot of the selected range at
        comment creation time. Used to re-anchor the comment (e.g. via
        content search) when the file is subsequently edited.
        ``NULL`` for legacy comments created before anchor support.
    :param created_by: Email of the user who created this comment,
        e.g. ``"alice@example.com"``. ``NULL`` for legacy comments or
        comments created in single-user mode.
    """

    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(4096))
    start_index: Mapped[int] = mapped_column(Integer)
    end_index: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    anchor_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_comments_conversation_id", "conversation_id"),
        Index("ix_comments_created_at", "created_at"),
    )


class SqlPolicy(Base):
    """
    SQLAlchemy model for the ``policies`` table.

    Policies are either session-scoped (``session_id`` set, FK to
    ``conversations.id``) or server-wide defaults
    (``session_id IS NULL``).

    Session-scoped policies are created via
    ``POST /v1/sessions/{session_id}/policies``. Default policies
    are created via ``POST /v1/policies``.

    :param id: Opaque PK, e.g. ``"pol_a1b2c3..."``.
    :param name: Human-readable name. UNIQUE per
        ``(session_id, name)`` for session policies; globally
        unique for default policies (``session_id IS NULL``).
    :param session_id: FK to ``conversations.id``. ``None`` for
        server-wide default policies. ``ON DELETE CASCADE`` so
        removing a session cleans up its policies.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write,
        ``None`` if the row has never been updated.
    :param type: Handler discriminator: ``"python"``,
        ``"url"``.
    :param handler: Dotted import path (``type="python"``)
        or HTTPS URL (``type="url"``).
    :param factory_params: JSON-encoded dict of kwargs passed to
        the handler when it is a factory function. ``None`` when
        the handler is a direct callable or for ``type="url"``.
    :param enabled: Whether the engine consults this row.
        Defaults to true.
    :param created_by: User ID of the admin who created this
        policy. ``None`` in single-user mode or for
        session-scoped policies.
    """

    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    # Nullable: NULL for server-wide default policies.
    session_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(16))
    # Dotted import path (type="python") or HTTPS URL
    # (type="url") for the policy handler.
    handler: Mapped[str] = mapped_column(Text)
    # JSON-encoded dict of factory kwargs for type="python" when
    # the handler is a factory function. NULL when the handler is
    # a direct callable or for type="url". See the design doc's
    # FunctionRef.arguments pattern.
    factory_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_policies_created_at", "created_at"),
        Index("ix_policies_session_id", "session_id"),
        UniqueConstraint("session_id", "name", name="uq_policies_session_id_name"),
    )


class SqlWorkItem(Base):
    """
    SQLAlchemy model for the ``work_items`` table.

    A work item is a tracked unit of work — created by hand or ingested
    from Slack / email / GitHub / Jira — that an agent processes in a
    linked conversation. It is a thin layer over conversations (the
    conversation tree already provides each item's sub-session/thread,
    plan, and history); it deliberately does NOT revive the removed
    ``tasks`` table.

    :param id: Opaque PK, e.g. ``"wi_a1b2c3..."``.
    :param source: Intake source: ``"manual"``, ``"slack"``, ``"email"``,
        ``"github"``, or ``"jira"``.
    :param external_id: Source-native id (issue/PR number, message ts),
        or ``None`` for manual items.
    :param dedup_key: Globally-UNIQUE idempotency key, e.g.
        ``"github:acme/app#123"`` — a repeated intake of the same event
        resolves to the existing row instead of inserting a duplicate.
    :param title: Short human-readable title.
    :param body: Optional longer description / original payload text.
    :param status: Lifecycle state: ``"new"``, ``"planned"``,
        ``"in_progress"``, ``"blocked"``, ``"needs_review"``, ``"done"``.
        Defaults to ``"new"``.
    :param pr_url: Optional pull-request URL once produced.
    :param conversation_id: FK to ``conversations.id`` for the
        conversation/sub-session doing the work. ``ON DELETE SET NULL`` so
        deleting that conversation keeps the work-item record.
    :param assignee_user_id: Assigned user id, or ``None``.
    :param created_by: Creating user id, or ``None`` (service-ingested).
    :param plan: Optional free-text/JSON plan recorded by the agent.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, ``None`` if
        never updated.
    """

    __tablename__ = "work_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), server_default=text("'new'"))
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    assignee_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_work_items_dedup_key"),
        Index("ix_work_items_status", "status"),
        Index("ix_work_items_conversation_id", "conversation_id"),
        Index("ix_work_items_created_at", "created_at"),
    )


class SqlSchedule(Base):
    """
    SQLAlchemy model for the ``schedules`` table.

    A schedule drives recurring agent work within a conversation: a
    ``"loop"`` fires ``prompt`` on a ``cron`` cadence, a ``"monitor"``
    streams ``command`` and fires ``prompt`` per output line. The
    scheduler service reads enabled rows to arm jobs.

    :param id: Opaque PK, e.g. ``"sch_a1b2c3..."``.
    :param conversation_id: FK to ``conversations.id`` the schedule fires
        into. ``ON DELETE CASCADE`` so removing a conversation drops its
        schedules.
    :param created_by_user_id: Creating user id, or ``None``.
    :param name: Name, UNIQUE per ``(conversation_id, name)``.
    :param kind: ``"loop"`` or ``"monitor"``.
    :param prompt: Prompt (or monitor template) fired on each tick.
    :param cron: Cron expression for loops, else ``None``.
    :param command: Shell command to stream for monitors, else ``None``.
    :param enabled: Whether the scheduler runs it. Defaults to true.
    :param status: Lifecycle/health state (``"idle"`` / ``"running"`` /
        ``"errored"``). Defaults to ``"idle"``.
    :param last_fired_at: Unix epoch seconds of the last fire, or ``None``.
    :param last_run_id: Id of the run from the last fire, or ``None``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, ``None`` if
        never updated.
    """

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
    )
    created_by_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(16))
    prompt: Mapped[str] = mapped_column(Text)
    cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    status: Mapped[str] = mapped_column(String(16), server_default=text("'idle'"))
    last_fired_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("conversation_id", "name", name="uq_schedules_conversation_id_name"),
        Index("ix_schedules_conversation_id", "conversation_id"),
        Index("ix_schedules_enabled", "enabled"),
    )


class SqlCanvas(Base):
    """
    SQLAlchemy model for the ``canvases`` table.

    One agent-authored canvas artifact per conversation (Cursor-style): the
    ``set_canvas`` tool upserts HTML/Markdown content, and the web UI renders
    it in a right-rail Canvas tab.

    :param id: Opaque PK, e.g. ``"cnv_a1b2c3..."``.
    :param conversation_id: FK to ``conversations.id``; UNIQUE (one canvas per
        conversation). ``ON DELETE CASCADE`` drops it with the conversation.
    :param title: Tab title.
    :param content: Artifact body — HTML or Markdown source.
    :param content_type: ``"html"`` or ``"markdown"``. Defaults to ``"html"``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, ``None`` if never
        updated since creation.
    """

    __tablename__ = "canvases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.id", ondelete="CASCADE"),
    )
    title: Mapped[str] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(16), server_default=text("'html'"))
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (UniqueConstraint("conversation_id", name="uq_canvases_conversation_id"),)


class SqlPushSubscription(Base):
    """
    SQLAlchemy model for the ``push_subscriptions`` table.

    A browser Web Push registration (#8): one row per (user, endpoint). The
    endpoint is UNIQUE so re-subscribing the same browser upserts in place
    rather than duplicating. Not FK'd to a conversation — subscriptions are
    user-scoped and outlive any single session.

    :param id: Opaque PK, e.g. ``"push_a1b2c3..."``.
    :param user_id: The owning user's identity (indexed for the sender's
        per-user lookup).
    :param endpoint: Push-service URL to POST encrypted payloads to; UNIQUE.
    :param p256dh: Client public key (base64url, uncompressed P-256 point).
    :param auth: Client auth secret (base64url, 16 bytes).
    :param created_at: Unix epoch seconds at row creation.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    endpoint: Mapped[str] = mapped_column(String(512))
    p256dh: Mapped[str] = mapped_column(String(256))
    auth: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),)


class SqlUserCredential(Base):
    """
    SQLAlchemy model for the ``user_credentials`` table (#5).

    One encrypted secret per (user, name) in the per-user vault.
    ``secret_encrypted`` holds a Fernet token (AES-128-CBC + HMAC), never
    plaintext. UNIQUE(user_id, name) so re-storing a named secret upserts. Not
    FK'd to a conversation — credentials are user-scoped and outlive sessions.

    :param id: Opaque PK, e.g. ``"cred_a1b2c3..."``.
    :param user_id: The owning user (indexed; only they can read/use it).
    :param name: Logical name, e.g. ``"github"`` / ``"databricks:prod"``.
    :param secret_encrypted: Fernet ciphertext of the secret value.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, ``None`` if never.
    """

    __tablename__ = "user_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    name: Mapped[str] = mapped_column(String(128))
    secret_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_credentials_user_name"),
    )


class SqlHost(Base):
    """
    SQLAlchemy model for the ``hosts`` table.

    Each row represents a machine that has connected to the server
    via ``omnigent host``. The row is upserted on first connect
    and updated on subsequent reconnects (name, status, timestamps).

    :param host_id: Stable host identifier from the host's local
        ``~/.omnigent/config.yaml``, e.g. ``"host_a1b2c3d4e5f6..."``.
    :param name: Human-readable name from ``config.yaml``, e.g.
        ``"corey-laptop"``. Displayed in the Web UI host picker.
    :param owner: User ID from the Databricks auth Bearer token
        presented during the host's WebSocket handshake, e.g.
        ``"corey.zumar@databricks.com"``.
    :param status: ``"online"`` when the host has an active WebSocket
        connection, ``"offline"`` when disconnected.
    :param created_at: Unix epoch seconds when the host was first
        registered (first ``omnigent host``).
    :param updated_at: Unix epoch seconds the row was last touched — a
        status change (connect/disconnect) or a tunnel heartbeat. Doubles
        as the host's last-seen for the liveness freshness gate, so a
        host that crashed without a graceful disconnect ages out of the
        "online" set once this stops advancing.
    :param token_hash: Hex SHA-256 digest of the launch token that
        authenticates a SERVER-MANAGED sandbox host's tunnel connection
        (``host_type="managed"`` sessions) — never the raw token.
        ``NULL`` for external (user-connected) hosts. Overwritten when
        the sandbox is relaunched, which atomically revokes the
        previous generation's token.
    :param token_expires_at: Unix epoch seconds after which the launch
        token no longer authenticates. Scoped to the TOKEN, not the
        host — the host row is durable across sandbox generations; the
        expiry is set past the provider's maximum sandbox lifetime so a
        live sandbox can always reconnect while a token leaked from a
        dead one cannot. ``NULL`` for external hosts.
    :param sandbox_provider: Sandbox provider backing a managed host,
        e.g. ``"modal"``. ``NULL`` for external hosts — non-NULL is the
        "this host is server-managed" discriminator.
    :param sandbox_id: Provider-assigned id of the sandbox currently
        backing the host, e.g. ``"sb-a1b2c3"`` — what termination is
        issued against. ``NULL`` for external hosts.
    :param configured_harnesses: JSON-encoded per-harness readiness map
        reported in the host's last ``host.hello`` frame, e.g.
        ``'{"claude-sdk": true, "codex": false}'``. ``NULL`` when the
        host has never reported it (older host build) — unknown, not
        "nothing configured". Surfaced via ``GET /v1/hosts`` so the web
        agent picker can warn about unconfigured harnesses.
    """

    __tablename__ = "hosts"

    owner: Mapped[str] = mapped_column(String(256), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    host_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sandbox_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    configured_harnesses: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('online', 'offline')",
            name="ck_hosts_status",
        ),
        UniqueConstraint("host_id", name="uq_hosts_host_id"),
        UniqueConstraint("token_hash", name="uq_hosts_token_hash"),
    )


class SqlUserDailyCost(Base):
    """
    SQLAlchemy model for the ``user_daily_cost`` table.

    A running per-user, per-UTC-day rollup of LLM spend, used by
    cost-aware policies (e.g. the "downgrade expensive model once a
    user has spent >$X today" sample policy) to read a user's
    accumulated daily cost as a single O(1) point lookup instead of
    aggregating the per-session ``conversations.session_usage`` blobs
    on every policy evaluation.

    One row per ``(user_id, day_utc)``. Incremented (UPSERT
    ``cost_usd = cost_usd + delta``) at each turn boundary from the
    cost write sites — but only when the session runs under at least
    one policy, so the table is never touched in deployments that
    have no policies configured (this keeps the shared server code
    inert against a database that lacks this table).

    :param user_id: The user the cost is attributed to — the session
        creator (``LEVEL_OWNER`` grantee), e.g.
        ``"alice@example.com"``.
    :param day_utc: The UTC calendar day the spend occurred, as an
        ISO date string ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        Bucketed by the turn's wall-clock time, so a session spanning
        midnight splits its cost across both days correctly.
    :param cost_usd: Cumulative USD spend for this user on this day.
        Starts at the first turn's delta and grows by each subsequent
        turn's delta.
    :param ask_approved_usd: Highest soft warning checkpoint (USD) the
        user has already approved continuing past for this day — read
        and written by the per-user daily cost-budget policy so an
        approved checkpoint prompts at most once per day (across all of
        the user's sessions), not once per session. ``0.0`` (the
        server default) means no checkpoint approved yet.
    :param updated_at: Unix epoch seconds of the last increment.
    """

    __tablename__ = "user_daily_cost"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day_utc: Mapped[str] = mapped_column(String(10), primary_key=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    ask_approved_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(Integer)
