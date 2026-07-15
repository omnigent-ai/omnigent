"""SQLAlchemy table definitions for the omnigent database."""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from omnigent.db.compression import CompressedText

# 32-byte sha256 digest column. LargeBinary → BYTEA (Postgres) / BLOB (SQLite),
# but MySQL cannot index a BLOB without a key-prefix length, so use fixed-length
# BINARY(32) there — an exact fit for the digest and fully indexable.
_CKSUM32 = LargeBinary(32).with_variant(MySQLBinary(32), "mysql")


class Uuid16(TypeDecorator[str]):
    """A UUID primary key stored as 16 raw bytes.

    Python-side values are bare 32-char hex strings (e.g.
    ``"a1b2c3d4..."``, no dashes — matching the schema-wide UUID
    convention); the database stores the 16-byte form —
    ``BINARY(16)`` on MySQL (fixed-length and fully indexable, the same
    reasoning as :data:`_CKSUM32`) and ``BLOB`` / ``BYTEA`` elsewhere.
    Halves the storage of a 32-char hex string and indexes tighter.

    ``process_bind_param`` accepts either a canonical/hex string or a
    :class:`uuid.UUID`; ``process_result_value`` always returns the
    bare 32-char hex string (``.hex``, no dashes).
    """

    impl = LargeBinary(16)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "mysql":
            return dialect.type_descriptor(MySQLBinary(16))
        return dialect.type_descriptor(LargeBinary(16))

    def process_bind_param(self, value: str | uuid.UUID | None, dialect: Any) -> bytes | None:  # noqa: ARG002
        if value is None:
            return None
        return (value if isinstance(value, uuid.UUID) else uuid.UUID(value)).bytes

    def process_result_value(self, value: bytes | None, dialect: Any) -> str | None:  # noqa: ARG002
        if value is None:
            return None
        return uuid.UUID(bytes=bytes(value)).hex


class OmnigentBase(DeclarativeBase):
    """Declarative base for the Omnigent operational tables.

    Covers agents, files, users, tokens, session permissions,
    conversation metadata, comments, policies, hosts, and daily costs.
    Grouped under their own ``metadata`` so schema creation and Alembic
    autogenerate can target the Omnigent side independently of the
    conversation tables.
    """


class ConversationBase(DeclarativeBase):
    """Declarative base for the conversation tables.

    Covers ``conversations``, ``conversation_items``, and
    ``conversation_labels`` — the user-facing conversation surface
    (the Agent-Platform-side tables). Kept under their own ``metadata``
    so they can be created and, when ``conversation_storage_location``
    is configured, hosted on a separate physical database from the
    Omnigent tables.
    """


# Default workspace id stamped on every row and used as the leading
# member of every composite primary key. 0 is the single-workspace /
# unassigned sentinel: with no workspace bound to the request, all rows
# live in workspace 0.
DEFAULT_WORKSPACE_ID = 0

# Ambient per-request workspace id. Stores are process-wide singletons, so
# the active workspace can't ride on the store instance — it lives here.
# OSS leaves this at the default (single-workspace 0); a multi-tenant
# deployment (e.g. universe) sets it per request from the authenticated
# context (via ``workspace_scope`` in middleware). Reads and inserts
# resolve it through ``current_workspace_id()`` so the same store code
# scopes to the caller's workspace without threading the id through every
# signature — keeping this file byte-identical across deployments.
_current_workspace_id: ContextVar[int] = ContextVar(
    "omnigent_workspace_id", default=DEFAULT_WORKSPACE_ID
)


def current_workspace_id() -> int:
    """Return the workspace id bound to the active request/context.

    Defaults to :data:`DEFAULT_WORKSPACE_ID` (0) — the single-workspace OSS
    deployment. Multi-tenant deployments set it per request so every
    primary-key lookup, filter, and insert scopes to that workspace.
    """
    return _current_workspace_id.get()


@contextlib.contextmanager
def workspace_scope(workspace_id: int) -> Iterator[None]:
    """Bind *workspace_id* for the duration of the ``with`` block.

    Used by multi-tenant request middleware (and tests) to scope all
    store access to one workspace; resets to the prior value on exit so
    nested / concurrent contexts don't leak.
    """
    token = _current_workspace_id.set(workspace_id)
    try:
        yield
    finally:
        _current_workspace_id.reset(token)


AGENT_KIND_TEMPLATE = "template"
AGENT_KIND_SESSION = "session"

POLICY_SCOPE_DEFAULT = "default"
POLICY_SCOPE_SESSION = "session"


class SqlAgent(OmnigentBase):
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
    :param kind: ``"template"`` for server-wide registered agents;
        ``"session"`` for per-conversation copies.
    :param description: Optional free-text description of the agent's
        purpose. ``None`` when not provided.
    :param updated_at: Unix epoch seconds of the last update, or
        ``None`` if the agent has never been updated.
    """

    __tablename__ = "agents"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(256))
    bundle_location: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # AGENT_KIND: template=1, session=2). The store converts to/from the
    # string name at the row↔entity boundary.
    kind: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_agents_kind"),
        Index("ix_agents_created_at", "workspace_id", "created_at", "id"),
        # Template agents have unique names; session-scoped agents (kind=2)
        # may reuse the same name. That "unique only within the template set"
        # rule can't be a partial unique index (MySQL has none), so it is
        # enforced in the store (SqlAlchemyAgentStore.create). This plain index
        # backs the (workspace_id, name, kind) lookup that check and get_by_name
        # do — kind is included so the seek skips same-named session copies
        # straight to the template row.
        Index("ix_agents_name", "workspace_id", "name", "kind", "id"),
    )


class SqlFile(OmnigentBase):
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_files_created_at", "workspace_id", "created_at", "id"),
        Index(
            "ix_files_session_id_created_at",
            "workspace_id",
            "session_id",
            "created_at",
            "id",
        ),
    )


class SqlUser(OmnigentBase):
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_login_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SqlAccountToken(OmnigentBase):
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ACCOUNT_TOKEN_KIND: invite=1, magic=2). The store converts to/from
    # the string name at the row↔entity boundary.
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    redeemed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invited_is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_account_tokens_kind"),
        Index("ix_account_tokens_expires_at", "workspace_id", "expires_at", "id"),
    )


class SqlSessionPermission(OmnigentBase):
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("level IN (1, 2, 3, 4)", name="ck_session_permissions_level"),
        # Lookups by conversation (get_session_owner) filter workspace_id +
        # conversation_id; user_id trails to complete the PK.
        Index(
            "ix_session_permissions_conversation_id",
            "workspace_id",
            "conversation_id",
            "user_id",
        ),
    )


class SqlConversationMetadata(OmnigentBase):
    """
    SQLAlchemy model for the ``omnigent_conversation_metadata`` table.

    Omnigent-side operational state for a conversation: runner/host
    bindings, native-session linkage, policy accumulators, and launch
    arguments. Paired 1-to-1 with :class:`SqlConversation` by
    ``(workspace_id, id)``; rows are created and deleted together.
    """

    __tablename__ = "omnigent_conversation_metadata"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Enum stored as a stable int code (CONVERSATION_KIND: default=1, sub_agent=2).
    kind: Mapped[int] = mapped_column(SmallInteger, default=1)
    runner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # No FK: host records are managed outside this table.
    host_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sub_agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_state: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    session_usage: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # JSON-encoded list of strings. NULL for non-native sessions.
    terminal_launch_args: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Required when host_id is set; enforced by check constraint below.
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_conversation_metadata_kind"),
        CheckConstraint(
            "host_id IS NULL OR workspace IS NOT NULL",
            name="ck_conversation_metadata_workspace_required_for_host",
        ),
        # Supports list_conversations kind filter.
        Index("ix_conversation_metadata_kind", "workspace_id", "kind", "id"),
        # Supports list_conversations_by_runner_id and get_runner_ids.
        Index("ix_conversation_metadata_runner_id", "workspace_id", "runner_id", "id"),
    )


class SqlAgentConfiguration(ConversationBase):
    """
    SQLAlchemy model for the ``agent_configuration`` table.

    The agent bound to a conversation and its per-session config
    overrides. Paired 1-to-1 with :class:`SqlConversation` by
    ``(workspace_id, conversation_id)``; both tables live on the
    Conversation base, so the pair is created and deleted in one
    transaction.

    :param conversation_id: Conversation this row belongs to, e.g.
        ``"conv_e4f5a6b7..."``.
    :param agent_id: Agent bound to the conversation at creation
        time. ``None`` for conversations created without an agent
        binding.
    :param reasoning_effort: Per-session reasoning-effort hint.
    :param model_override: Per-session LLM model override.
    :param cost_control_mode_override: Per-session cost-control switch.
    :param harness_override: Per-session brain-harness override.
    """

    __tablename__ = "agent_configuration"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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

    __table_args__ = (
        # Agent lookups: find the conversation(s) that own a given agent.
        # Covering: the reverse lookup and the list filters read only
        # conversation_id, so they resolve as index-only scans.
        Index(
            "ix_agent_configuration_agent_id",
            "workspace_id",
            "agent_id",
            "conversation_id",
        ),
    )


class SqlConversation(ConversationBase):
    """
    SQLAlchemy model for the ``conversations`` table.

    Agent Platform (AP) fields for a conversation: identity, timestamps,
    title, hierarchy, and the next_position allocator. The agent binding
    and per-session overrides live in :class:`SqlAgentConfiguration`; Omnigent
    operational state in :class:`SqlConversationMetadata`.

    :param id: Unique conversation identifier, e.g.
        ``"conv_e4f5a6b7..."``.
    :param created_at: Unix epoch seconds when the conversation was
        created.
    :param updated_at: Unix epoch seconds when the conversation was
        last updated (item append, title change, etc.).
    :param title: Human-readable title; empty string when untitled.
    :param parent_conversation_id: For Phase 4 named sub-agents,
        points at the parent conversation. ``None`` for top-level
        conversations.
    :param root_conversation_id: Id of the root (top-level)
        conversation in the spawn tree. Equal to ``id`` for
        top-level conversations.
    :param next_position: Monotonic allocator for the next item position.
    """

    __tablename__ = "conversations"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(768), nullable=False, server_default="")
    parent_conversation_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    root_conversation_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    # Monotonic allocator for the next item position in this conversation.
    next_position: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    # Whether the session is archived (hidden from the default sidebar). Lives
    # here on the AP table so list_conversations can filter it inline alongside
    # the created_at/updated_at sort keys, instead of pre-fetching ids from the
    # Omnigent metadata DB.
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        Index("ix_conversations_created_at", "workspace_id", "created_at", "id"),
        Index("ix_conversations_updated_at", "workspace_id", "updated_at", "id"),
        # Default sidebar filters archived=false and sorts by updated_at DESC;
        # archived leads as an equality so the page walk stays index-only.
        Index("ix_conversations_archived_updated", "workspace_id", "archived", "updated_at", "id"),
        Index(
            "ix_conversations_root_conversation_id",
            "workspace_id",
            "root_conversation_id",
            "id",
        ),
        # Unique index on (parent_conversation_id, title) prevents two
        # same-named children under the same parent. NULLs are distinct in a
        # unique index, so top-level conversations (NULL parent) are exempt.
        Index(
            "ix_conversations_parent_title_unique",
            "workspace_id",
            "parent_conversation_id",
            "title",
            unique=True,
            mysql_length={"title": 512},
        ),
        # Composite index for child-session listing.
        Index(
            "idx_conversations_parent",
            "workspace_id",
            "parent_conversation_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class SqlConversationItem(ConversationBase):
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    # conversation_id leads id in the PK so a conversation's items stay
    # contiguous for the per-conversation prefix scans that dominate reads.
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    response_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(Integer)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ITEM_STATUS: completed=1). Only "completed" is written today, but the
    # CHECK admits the wider OpenAI-style status vocabulary reserved there.
    status: Mapped[int] = mapped_column(SmallInteger, default=1)
    position: Mapped[int] = mapped_column(Integer)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ITEM_TYPE). The store converts to/from the string name at the
    # row↔entity boundary.
    type: Mapped[int] = mapped_column(SmallInteger)
    data: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index(
            "ix_conversation_items_conversation_id_position",
            "workspace_id",
            "conversation_id",
            "position",
            unique=True,
        ),
        # Fork-truncation looks up by workspace_id + conversation_id +
        # response_id; id trails to complete the PK.
        Index(
            "ix_conversation_items_response_id",
            "workspace_id",
            "conversation_id",
            "response_id",
            "id",
        ),
        # Latest-message previews scan one type per conversation ordered by
        # position DESC (list_latest_message_items_for_conversations). Ordering
        # type before position lets the scan seek to (workspace_id,
        # conversation_id, type) and walk position DESC directly, avoiding a
        # heap recheck on type — which no other index covers.
        Index(
            "ix_conversation_items_conv_type_position",
            "workspace_id",
            "conversation_id",
            "type",
            text("position DESC"),
        ),
        CheckConstraint(
            "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)",
            name="ck_conversation_items_type",
        ),
        CheckConstraint("status IN (1, 2, 3, 4)", name="ck_conversation_items_status"),
    )


# Width of the ``conversation_labels.value`` column. Exported so the store
# (and the session-status error-label path) can clamp values to fit instead
# of letting an over-length write raise ``DataError`` on PostgreSQL.
LABEL_VALUE_MAX_LEN = 256


class SqlConversationLabel(ConversationBase):
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(LABEL_VALUE_MAX_LEN))
    updated_at: Mapped[int] = mapped_column(Integer)


class SqlComment(OmnigentBase):
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(4096))
    start_index: Mapped[int] = mapped_column(Integer)
    end_index: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(CompressedText)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # COMMENT_STATUS: draft=1, addressed=2).
    status: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    anchor_content: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (1, 2)", name="ck_comments_status"),
        # Serves list_for_conversation: WHERE workspace_id + conversation_id
        # ORDER BY created_at, id. Folds created_at in (over a bare
        # conversation_id index) so the sort is index-ordered; trails id to
        # complete the PK.
        Index(
            "ix_comments_conversation_id",
            "workspace_id",
            "conversation_id",
            "created_at",
            "id",
        ),
    )


def policy_name_cksum(name: str) -> bytes:
    """Return the sha256 digest of a policy name.

    This 32-byte digest is what the name-uniqueness indexes key on instead
    of the raw ``VARCHAR(256)`` name — a fixed, compact index entry. Two
    names collide iff their digests do, so uniqueness is preserved.
    """
    return hashlib.sha256(name.encode("utf-8")).digest()


def _default_policy_name_cksum(context: Any) -> bytes:
    """Column default: derive ``name_cksum`` from the bound ``name`` on INSERT.

    Mirrors the ``workspace_id`` default pattern so every ORM insert stamps
    the checksum without the caller setting it. Column defaults do not fire
    on UPDATE, so renames recompute it explicitly in the store.
    """
    return policy_name_cksum(context.get_current_parameters()["name"])


class SqlPolicy(OmnigentBase):
    """
    SQLAlchemy model for the ``policies`` table.

    Policies are either session-scoped (``session_id`` set, FK to
    ``conversations.id``) or server-wide defaults
    (``session_id IS NULL``).

    Session-scoped policies are created via
    ``POST /v1/sessions/{session_id}/policies``. Default policies
    are created via ``POST /v1/policies``.

    :param id: Opaque PK, e.g. ``"pol_a1b2c3..."``.
    :param name: Human-readable name. UNIQUE per session for
        session policies; globally unique for default policies
        (``session_id IS NULL``). Uniqueness is enforced on
        ``name_cksum`` rather than this column.
    :param name_cksum: sha256 digest of ``name`` (32 bytes). The
        name-uniqueness indexes key on this compact digest instead
        of the wide ``VARCHAR(256)`` name. Stamped on INSERT by a
        column default; recomputed by the store on rename.
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
    :param scope: ``"default"`` for server-wide policies;
        ``"session"`` for session-scoped policies. Explicit
        discriminator so queries filter by column value instead
        of checking ``session_id IS NULL``.
    :param created_by: User ID of the admin who created this
        policy. ``None`` in single-user mode or for
        session-scoped policies.
    """

    __tablename__ = "policies"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    # sha256(name) — the value the name-uniqueness indexes key on instead of
    # the wide name column. Stamped from `name` on INSERT via the column
    # default; the store recomputes it on rename (defaults don't fire on UPDATE).
    name_cksum: Mapped[bytes] = mapped_column(_CKSUM32, default=_default_policy_name_cksum)
    # Nullable: NULL for server-wide default policies.
    session_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Handler discriminator stored as a stable int code (see
    # omnigent.db.enum_codecs POLICY_TYPE: python=1, url=2).
    type: Mapped[int] = mapped_column(SmallInteger)
    # Dotted import path (type="python") or HTTPS URL
    # (type="url") for the policy handler.
    handler: Mapped[str] = mapped_column(Text)
    # JSON-encoded dict of factory kwargs for type="python" when
    # the handler is a factory function. NULL when the handler is
    # a direct callable or for type="url". See the design doc's
    # FunctionRef.arguments pattern.
    factory_params: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    # "default" for server-wide policies; "session" for per-conversation
    # copies. Mirrors the agents.kind pattern so queries filter by column
    # value rather than session_id IS NULL. Enum stored as a stable int
    # code (see omnigent.db.enum_codecs POLICY_SCOPE: default=1, session=2).
    scope: Mapped[int] = mapped_column(SmallInteger)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("type IN (1, 2)", name="ck_policies_type"),
        CheckConstraint("scope IN (1, 2)", name="ck_policies_scope"),
        Index("ix_policies_created_at", "workspace_id", "created_at", "id"),
        Index("ix_policies_session_id", "workspace_id", "session_id", "id"),
        # Name uniqueness keys on name_cksum (sha256 of name) rather than the
        # wide name column, for a compact 32-byte index entry.
        UniqueConstraint(
            "workspace_id",
            "session_id",
            "name_cksum",
            name="uq_policies_session_id_name_cksum",
        ),
        # Default policies must have unique names; session-scoped policies
        # may reuse the same name. That "unique only within the default set"
        # rule can't be a partial unique index (MySQL has none), so it is
        # enforced in the store (add_default / update_default). This plain
        # index just backs the name_cksum lookup those checks perform.
        Index("ix_policies_name_cksum", "workspace_id", "name_cksum", "id"),
    )


class SqlHost(OmnigentBase):
    """
    SQLAlchemy model for the ``hosts`` table.

    Each row represents a machine that has connected to the server
    via ``omnigent host``. The row is upserted on first connect
    and updated on subsequent reconnects (name, status, timestamps).

    :param host_id: Stable host identifier from the host's local
        ``~/.omnigent/config.yaml``, e.g. ``"host_a1b2c3d4e5f6..."``.
    :param name: Human-readable name from ``config.yaml``, e.g.
        ``"corey-laptop"``. Displayed in the Web UI host picker. Max 64
        characters.
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    host_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # HOST_STATUS: online=1, offline=2).
    status: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sandbox_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    configured_harnesses: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (1, 2)",
            name="ck_hosts_status",
        ),
        # (workspace_id, owner, name) was the old PK; keep it unique so the
        # upsert-on-connect logic (look up by owner+name to detect host_id
        # rotation) stays consistent.
        UniqueConstraint("workspace_id", "owner", "name", name="uq_hosts_workspace_owner_name"),
        # resolve_launch_token filters workspace_id + token_hash, so scoping
        # the unique to the workspace keeps that lookup index-served.
        UniqueConstraint("workspace_id", "token_hash", name="uq_hosts_token_hash"),
    )


class SqlUserDailyCost(OmnigentBase):
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

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day_utc: Mapped[str] = mapped_column(String(10), primary_key=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    ask_approved_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(Integer)


class SqlScheduledTask(OmnigentBase):
    """
    SQLAlchemy model for the ``scheduled_tasks`` table.

    A scheduled task is a saved, scheduled instruction that fires an agent
    session on a recurring cron schedule (``cron_expression``).

    :param id: UUID primary key stored as 16 raw bytes (see :class:`Uuid16`),
        surfaced as a bare 32-char hex string (no dashes).
    :param name: Human-readable task name, e.g. ``"nightly triage"``.
    :param prompt: The instruction dispatched to the agent on each firing.
    :param cron_expression: The required cron string for the recurring trigger,
        e.g. ``"0 9 * * *"``.
    :param owner_user_id: User the spawned session's ``LEVEL_OWNER`` grant is
        written for — who the run belongs to, e.g. ``"alice@example.com"``.
        ``None`` in single-user / OSS mode; the fire path resolves it to the
        reserved ``"local"`` user.
    :param agent_id: The agent bound to this task (relates to
        ``agents.id``). Cascade cleanup on agent deletion is application-owned
        — there is no DB-level foreign key (schema Rule R032).
    :param model_override: Per-task LLM model override, e.g.
        ``"claude-opus-4-7"``. ``None`` means use the agent default.
    :param reasoning_effort: Per-task reasoning-effort hint, e.g. ``"high"``.
        ``None`` means use the agent default.
    :param workspace: Absolute path on disk where a fired session's runner
        should start (the source repo / working dir). ``None`` when unset.
    :param base_branch: Git base ref a firing branches FROM when it creates a
        worktree at fire time (mirrors session-create's ``git.base_branch``
        input). Pairs with ``workspace``:
        ``workspace`` is where, ``base_branch`` is what to branch from. ``None``
        when unset. The per-run *output* branch is not stored on the definition.
    :param execution_target: Where a firing runs —
        ``connected_host``/``managed_sandbox``. ``connected_host`` resolves the
        owner's live host at fire time (see ``host_id``); ``managed_sandbox``
        provisions/adopts a sandbox at fire time. Stored as a stable int code
        (see omnigent.db.enum_codecs SCHEDULED_TASK_EXECUTION_TARGET); the store
        converts to/from the string name at the row↔entity boundary. Defaults to
        ``connected_host``.
    :param host_id: For ``execution_target=connected_host``, the specific host
        to run on (relates to ``hosts.host_id``; no DB foreign key, Rule R032).
        ``None`` means "the owner's freshest online host". Always ``None`` for
        ``managed_sandbox`` (the sandbox is provisioned/adopted under a
        deterministic id at fire time, so there is nothing to pin).
    :param timezone: IANA timezone the trigger is evaluated in, e.g.
        ``"America/Los_Angeles"``.
    :param state: Lifecycle state — ``active``/``paused``/``deleted``.
        The scheduler only dispatches ``active`` tasks.
        Stored as a stable int code (see omnigent.db.enum_codecs
        SCHEDULED_TASK_STATE); the store converts to/from the string name at the
        row↔entity boundary. Defaults to ``active``.
    :param last_run_at: Unix epoch seconds of the most recent firing, or
        ``None`` if it has never fired.
    :param last_run_conversation_id: The conversation created by the most recent
        firing (relates to ``conversations.id``). ``None`` if never fired or the
        referenced conversation was deleted (application-owned SET-NULL cleanup;
        no DB foreign key).
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    """

    __tablename__ = "scheduled_tasks"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Opaque free text, never SQL-queried — stored compressed (CompressedText).
    prompt: Mapped[str] = mapped_column(CompressedText, nullable=False)
    # e.g. "0 9 * * *"
    cron_expression: Mapped[str] = mapped_column(String(255), nullable=False)
    # Session-owner identity: the spawned run's LEVEL_OWNER grant is written
    # for this user. Nullable — None in single-user/OSS mode (the fire path
    # resolves null to the reserved "local" user). String(128) to match
    # session_permissions.user_id (the column the LEVEL_OWNER grant is
    # written into) and every other user-identity column in this schema.
    owner_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Relates to agents.id. No DB foreign key (Rule R032); cascade is app-owned.
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Per-task overrides — None means fall back to the agent default. Widths
    # mirror the matching conversations.* override columns.
    model_override: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Git base ref a firing branches from when it creates a worktree at fire
    # time (mirrors session-create's git.base_branch input). None when unset.
    base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Where a firing runs, as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_EXECUTION_TARGET: connected_host=1, managed_sandbox=2).
    # connected_host → resolve the owner's live host at fire time (see host_id);
    # managed_sandbox → provision/adopt a sandbox at fire time. Defaults to
    # connected_host so existing rows keep the V1 behavior. The store converts
    # to/from the string name at the row↔entity boundary.
    execution_target: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    # For execution_target=connected_host: the specific host to run on (relates
    # to hosts.host_id; No DB foreign key, Rule R032). None = "the owner's
    # freshest online host, whichever". Always None for managed_sandbox (the
    # sandbox is provisioned/adopted under a deterministic id at fire time, so
    # there is nothing to pin here).
    host_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_STATE: active=1, paused=2, deleted=3). The
    # store converts to/from the string name at the row↔entity boundary.
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    last_run_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Relates to conversations.id. No DB foreign key (Rule R032); the
    # application nulls this out when the referenced conversation is deleted.
    last_run_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("state IN (1, 2, 3)", name="ck_scheduled_tasks_state"),
        CheckConstraint("execution_target IN (1, 2)", name="ck_scheduled_tasks_execution_target"),
        Index("ix_scheduled_tasks_created_at", "workspace_id", "created_at", "id"),
        Index("ix_scheduled_tasks_owner_user_id", "workspace_id", "owner_user_id", "id"),
        # Covers the scheduler's read path:
        # WHERE workspace_id + state ORDER BY created_at, id.
        Index("ix_scheduled_tasks_state", "workspace_id", "state", "created_at", "id"),
    )


class SqlScheduledTaskRun(OmnigentBase):
    """
    SQLAlchemy model for the ``scheduled_task_runs`` table.

    One row per firing of a scheduled task — the run history. Recorded and
    advanced by the scheduler as a firing moves through its lifecycle.

    :param id: UUID primary key stored as 16 raw bytes (see :class:`Uuid16`),
        surfaced as a bare 32-char hex string (no dashes).
    :param scheduled_task_id: The task this run belongs to (relates to
        ``scheduled_tasks.id``; also a :class:`Uuid16`). Indexed for per-task
        history listing. Cascade cleanup on task deletion is application-owned —
        no DB foreign key (Rule R032).
    :param conversation_id: The conversation created by this firing (relates to
        ``conversations.id``). ``None`` before dispatch, or after the referenced
        conversation is deleted (application-owned SET-NULL; no DB foreign key).
    :param status: Lifecycle state —
        ``scheduled``/``running``/``succeeded``/``failed``/``skipped``. Stored
        as a stable int code (see omnigent.db.enum_codecs
        SCHEDULED_TASK_RUN_STATUS); the store converts to/from the string name
        at the row↔entity boundary.
    :param scheduled_at: Unix epoch seconds the firing was scheduled for.
    :param fired_at: Unix epoch seconds dispatch actually began, or ``None`` if
        it has not fired yet.
    :param finished_at: Unix epoch seconds the run reached a terminal state, or
        ``None`` if still pending/running.
    :param error: Failure detail when ``status = 'failed'``; ``None`` otherwise.
    :param error_code: Short failure classification (e.g. ``"timeout"``,
        ``"rate_limited"``) for future retryable-vs-terminal retry logic;
        ``None`` unless ``status = 'failed'``.
    """

    __tablename__ = "scheduled_task_runs"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    # Relates to scheduled_tasks.id. No DB foreign key (Rule R032); cascade is
    # app-owned.
    scheduled_task_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    # Relates to conversations.id. No DB foreign key; app nulls on delete.
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_RUN_STATUS: scheduled=1, running=2, succeeded=3, failed=4,
    # skipped=5). The store converts to/from the string name at the
    # row↔entity boundary.
    status: Mapped[int] = mapped_column(SmallInteger)
    scheduled_at: Mapped[int] = mapped_column(Integer)
    fired_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Opaque free-text error blob, never SQL-queried — stored compressed.
    error: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Short, queryable failure classification token (e.g. "timeout",
    # "rate_limited") for future retry logic. Bounded plain string, not a blob;
    # no CHECK constraint (no code taxonomy defined yet).
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (1, 2, 3, 4, 5)",
            name="ck_scheduled_task_runs_status",
        ),
        Index(
            "ix_scheduled_task_runs_scheduled_task_id",
            "workspace_id",
            "scheduled_task_id",
            "scheduled_at",
            "id",
        ),
    )
