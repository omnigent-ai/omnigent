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


# Hex length of a bare uuid4 id, the canonical Python-side form.
_UUID_HEX_LEN = 32

# Prefixes ids carried before they became bare 32-char hex. ``uuid_to_bytes``
# strips exactly these (so old URLs/clients keep resolving) and nothing else —
# an unknown prefix fails loud rather than silently storing a wrong-typed id's
# hex tail (e.g. a ``resp_``/``runner_token_`` value mis-passed to a uuid column).
_LEGACY_ID_PREFIXES = frozenset(
    {
        "ag",
        "conv",
        "host",
        "pol",
        "file",
        "cmt",
        # conversation-item per-type prefixes
        "msg",
        "fc",
        "fco",
        "err",
        "rs",
        "cmp",
        "nt",
        "rse",
        "sc",
        "tc",
        "rd",
        # runner-internal conversation binding
        "agy_conv",
    }
)


class InvalidUuidError(ValueError):
    """An id string could not be normalised to a 32-char hex uuid.

    Subclasses ``ValueError`` so existing ``except ValueError`` sites keep
    working. Surfaced (wrapped in ``sqlalchemy.exc.StatementError``) when a
    malformed id reaches a ``Uuid16`` column bind; the server maps it to a 404
    so a bad id in a URL is not-found rather than a 500.
    """


def uuid_to_bytes(value: str | uuid.UUID) -> bytes:
    """Normalise an id to the 16 raw bytes stored in a ``Uuid16`` column.

    Accepts, reducing them all to the same 16 bytes: a :class:`uuid.UUID`
    object; the bare 32-char hex form (what generators emit); the dashed
    canonical uuid (``str(uuid4())``); and a legacy id carrying one of the
    known :data:`_LEGACY_ID_PREFIXES` (``conv_<hex>``, ``ag_<hex>``, …) — so
    old bookmarked URLs, pasted ids, and pre-migration clients keep resolving.
    Anything else — a truncated id, non-hex text, an unknown prefix — fails
    loud rather than silently storing the wrong bytes.

    :param value: A ``uuid.UUID``, or a 32-char hex uuid optionally dashed or
        legacy-prefixed.
    :returns: The 16-byte big-endian value.
    :raises InvalidUuidError: If *value* is not a 32-char hex uuid.
    """
    if isinstance(value, uuid.UUID):
        return value.bytes
    normalized = value.replace("-", "")
    if "_" in normalized:
        prefix, _, tail = normalized.rpartition("_")
        if prefix in _LEGACY_ID_PREFIXES and len(tail) == _UUID_HEX_LEN:
            normalized = tail
    if len(normalized) != _UUID_HEX_LEN:
        raise InvalidUuidError(f"expected a 32-char hex uuid, got {value!r}")
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise InvalidUuidError(f"invalid hex uuid: {value!r}") from exc


def normalize_uuid(value: str | None) -> str | None:
    """Return the bare 32-char hex form of *value*, or *value* unchanged.

    The forgiving companion to :func:`uuid_to_bytes` for **Python-side** id
    comparisons (e.g. a store's scope check against an ORM attribute, which
    always reads back bare hex). A legacy-prefixed or dashed input normalises
    to bare hex; a malformed input is returned as-is so the comparison simply
    mismatches — preserving the pre-migration "unknown id = not found"
    behaviour instead of raising. ``None`` passes through.

    :param value: Any caller-supplied id string, or ``None``.
    :returns: The bare 32-char hex form, or *value* verbatim if not a uuid.
    """
    if value is None:
        return None
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError:
        return value


class Uuid16(TypeDecorator[str]):
    """A uuid stored as 16 raw bytes, presented to Python as bare 32-char hex.

    Our ids are opaque 128-bit uuid4s stored as raw bytes — ``BYTEA``
    (PostgreSQL), ``BLOB`` (SQLite / D1), fixed-length ``BINARY(16)`` (MySQL,
    where a BLOB is not indexable without a key-prefix length). The rest of
    the system keeps the readable bare 32-char hex form (entities, JSON
    blobs, URLs, the FTS mirror), so this type converts at the column
    boundary and nothing else has to change. Binds accept bare, dashed, or
    legacy-prefixed uuids; results always come back as bare lowercase hex.
    Result values guard the same driver variance ``CompressedText`` does:
    ``bytes``, ``memoryview`` (some drivers), or ``str`` (already hex).
    """

    impl = LargeBinary(16)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "mysql":
            return dialect.type_descriptor(MySQLBinary(16))
        return dialect.type_descriptor(LargeBinary(16))

    def process_bind_param(self, value: str | uuid.UUID | None, _dialect: object) -> bytes | None:
        if value is None:
            return None
        return uuid_to_bytes(value)

    def process_result_value(
        self, value: bytes | memoryview | str | None, _dialect: object
    ) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return bytes(value).hex()


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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)

    __table_args__ = (
        # Files are only ever listed per session (WHERE session_id = ?),
        # so a session-scoped composite is the only index needed. There is
        # no session-less "all files" listing.
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


class SqlDeviceGrant(OmnigentBase):
    """
    SQLAlchemy model for the ``device_grants`` table.

    Backs the generic OAuth 2.0 Device Authorization Grant (RFC 8628) —
    any browserless client (the Slack integration is the first, but the
    mechanism is not Slack-specific) obtains a delegated, per-user access
    token without a user credential passing through the client. One row per
    device-authorization request; it moves ``pending`` → ``approved`` /
    ``denied`` (browser consent) → ``redeemed`` (token issued) and can be
    ``revoked`` at any time.

    Secrets are stored **hashed** (never raw): the client's ``device_code``
    and the current ``refresh_token`` are HMAC-SHA256 digests, so a
    database read cannot recover a usable token.

    :param id: Opaque grant id (also the ``grant_id`` JWT claim on issued
        access tokens, used for revocation).
    :param device_code_hash: HMAC-SHA256 hex digest of the secret
        ``device_code`` the client polls with. Never store the raw code.
    :param user_code: Short human-readable code shown on the verification
        page (also carried in ``verification_uri_complete``).
    :param status: ``pending`` / ``approved`` / ``denied`` / ``redeemed`` /
        ``revoked`` (see :data:`omnigent.db.enum_codecs.DEVICE_GRANT_STATUS`).
    :param client_id: The RFC 8628 client identifier — a public string
        naming the requesting application (e.g. ``"slack"``), the same for
        every grant that application initiates. Shown on the consent page
        and recorded in the issued token's ``act`` claim for audit.
        Display/audit only — not a security-decision key.
    :param user_id: The Omnigent identity that approved the grant, set at
        consent time. ``NULL`` while pending. The delegated token's ``sub``.
    :param refresh_token_hash: HMAC-SHA256 hex digest of the current
        refresh token. Rotated on every refresh; a presented token that no
        longer matches (and isn't the current one) is a reuse signal that
        revokes the grant.
    :param created_at: Unix epoch seconds when the grant was created.
    :param expires_at: Unix epoch seconds after which the ``device_code``
        can no longer be exchanged (the authorization request expires).
    :param approved_at: Unix epoch seconds when the grant was approved,
        starting its absolute lifetime clock. ``NULL`` until approved.
        Refresh is refused once ``approved_at`` is older than the absolute
        max lifetime, forcing periodic re-consent.
    :param last_polled_at: Unix epoch seconds of the last token-poll,
        used to enforce the RFC 8628 ``interval`` / ``slow_down``.
    """

    __tablename__ = "device_grants"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    device_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # DEVICE_GRANT_STATUS). The store converts to/from the name at the
    # row↔entity boundary.
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Digest of the just-superseded refresh token, kept only so a replay
    # of the previous token can be recognised as reuse (token theft) and
    # revoke the grant. Cleared on revoke.
    prev_refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_polled_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (1, 2, 3, 4, 5)", name="ck_device_grants_status"),
        # Poll path looks up by device_code_hash; a top-level index keeps it
        # a point lookup rather than a partition scan.
        Index("ix_device_grants_device_code_hash", "workspace_id", "device_code_hash"),
        Index("ix_device_grants_user_code", "workspace_id", "user_code"),
        Index("ix_device_grants_expires_at", "workspace_id", "expires_at", "id"),
        # Refresh + revoke look up by the current / previous refresh-token
        # digest; index both so those paths stay point lookups.
        Index("ix_device_grants_refresh_hash", "workspace_id", "refresh_token_hash"),
        Index("ix_device_grants_prev_refresh_hash", "workspace_id", "prev_refresh_token_hash"),
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
        Uuid16(),
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    # Enum stored as a stable int code (CONVERSATION_KIND: default=1, sub_agent=2).
    kind: Mapped[int] = mapped_column(SmallInteger, default=1)
    runner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # No FK: host records are managed outside this table.
    host_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    sub_agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_state: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    session_usage: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # JSON-encoded list of strings. NULL for non-native sessions.
    terminal_launch_args: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Required when host_id is set; enforced by check constraint below.
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Live-state columns, written by the replica holding the runner
    # tunnel so any replica can serve the sidebar's live fields.
    # Writes must never bump conversations.updated_at (it drives
    # sidebar ordering).
    # Epoch seconds the bound runner's tunnel was last seen alive;
    # runner_online is derived from freshness (like host_is_live).
    runner_last_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Last relay-observed turn status (enum_codecs.SESSION_LIVE_STATUS);
    # NULL means no relay has ever reported on this session.
    live_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Outstanding elicitation (approval-prompt) count; NULL = never written.
    pending_elicitation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # First-class project membership. Relates to projects.id; no DB FK
    # (Rule R032). NULL = unfiled. Coexists with the implicit ``omni_project``
    # label via the store's dual-read until labels are consolidated.
    project_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_conversation_metadata_kind"),
        CheckConstraint(
            "host_id IS NULL OR workspace IS NOT NULL",
            name="ck_conversation_metadata_workspace_required_for_host",
        ),
        # Supports list_conversations_by_runner_id and get_runner_ids.
        Index("ix_conversation_metadata_runner_id", "workspace_id", "runner_id", "id"),
        # "list sessions in project X" + per-project counts (GROUP BY project_id).
        Index("ix_conversation_metadata_project_id", "workspace_id", "project_id", "id"),
    )


class SqlProject(OmnigentBase):
    """
    SQLAlchemy model for the ``projects`` table.

    A user-defined, owner-private container that groups sessions (see
    ``designs/PROJECTS_PRD.md``). A project row exists independently of its
    member sessions, so it can be empty, renamed, and carry its own config —
    the things the implicit ``omni_project`` label could not. Session
    membership lives on ``omnigent_conversation_metadata.project_id``, not
    here; there is no DB foreign key (Rule R032).

    Ownership is stamped on the row via ``owner_user_id`` (like
    ``scheduled_tasks``), not derived from a permission table the way session
    ownership is — projects have no ACL of their own and are never shared.

    :param id: Uuid16 primary key (bare 32-char hex in Python).
    :param name: Human-readable project name; unique per owner (enforced in
        the store, since ``owner_user_id`` is NULL in single-user mode and a DB
        unique index treats NULLs as distinct).
    :param owner_user_id: Owning user, or ``None`` in single-user mode.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    """

    __tablename__ = "projects"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Default session settings as a compact JSON object (host/workspace/harness/
    # model/reasoning_effort/git base-branch, …), or NULL for "no defaults". The
    # keys are an opaque, client-owned vocabulary: the value is read and written
    # whole with the row and never filtered in SQL, so new keys need no schema
    # change. Stored values are hints the new-chat dialog pre-fills and the user
    # can always override.
    config: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # "list my projects" — prefix scan on (workspace_id, owner_user_id) with
        # created_at in the key so the ORDER BY created_at, id is served by the
        # index (no filesort). Server returns a stable order; reorder, if ever
        # added, is a client-only concern, so there is no ``position`` column.
        Index(
            "ix_projects_owner_user_id",
            "workspace_id",
            "owner_user_id",
            "created_at",
            "id",
        ),
        # Enforces per-owner name uniqueness at the DB layer for NON-NULL owners
        # (closing the store's check-then-insert race under concurrency). SQL
        # treats NULLs as distinct, so single-user rows (owner_user_id IS NULL)
        # can still collide on name — the store's _name_taken check covers that
        # case. Also backs the get-by-name lookup.
        Index(
            "ix_projects_name",
            "workspace_id",
            "owner_user_id",
            "name",
            unique=True,
        ),
    )


class SqlConversation(ConversationBase):
    """
    SQLAlchemy model for the ``conversations`` table.

    Agent Platform (AP) fields for a conversation: identity, timestamps,
    title, hierarchy, the next_position allocator, and the agent binding
    (``agent_id`` + the ``session_overrides`` JSON blob). Omnigent
    operational state lives in :class:`SqlConversationMetadata`.

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
    :param agent_id: Agent bound to the conversation at creation time.
        ``None`` for conversations created without an agent binding.
    :param session_overrides: Compact JSON blob of per-session config
        overrides (reasoning_effort, model_override,
        cost_control_mode_override, harness_override). ``None`` when the
        session uses all agent/spec defaults.
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(768), nullable=False, server_default="")
    parent_conversation_id: Mapped[str | None] = mapped_column(
        Uuid16(),
        nullable=True,
    )
    root_conversation_id: Mapped[str] = mapped_column(
        Uuid16(),
        nullable=False,
    )
    # Monotonic allocator for the next item position in this conversation.
    next_position: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    # Agent bound to this conversation at creation time. NULL for conversations
    # created without an agent binding. Indexed for the agent→conversation
    # reverse lookup and the list filters (agent_id / has_agent_id / agent_name).
    agent_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    # Per-session config overrides packed as a compact JSON object, e.g.
    # ``{"model_override":"claude-opus-4-8","reasoning_effort":"high"}``. Keys:
    # reasoning_effort, model_override, cost_control_mode_override,
    # harness_override. NULL when the session uses all agent/spec defaults; only
    # set keys are stored. Never filtered in SQL — read and written whole with
    # the row (see the store's _encode/_decode_session_overrides).
    session_overrides: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Whether the session is archived (hidden from the default sidebar). Lives
    # here on the AP table so list_conversations can filter it inline alongside
    # the created_at/updated_at sort keys, instead of pre-fetching ids from the
    # Omnigent metadata DB.
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        # No bare created_at/updated_at indexes: the sessions list is ACL-scoped
        # (id IN (...)) and resolves via the PK; the default sidebar (archived=
        # false, updated_at DESC) is served by the archived_updated index below.
        Index("ix_conversations_archived_updated", "workspace_id", "archived", "updated_at", "id"),
        Index(
            "ix_conversations_root_conversation_id",
            "workspace_id",
            "root_conversation_id",
            "id",
        ),
        # Agent→conversation reverse lookup and the agent_id / has_agent_id /
        # agent_name list filters. id trails to complete the PK (index-only).
        Index(
            "ix_conversations_agent_id",
            "workspace_id",
            "agent_id",
            "id",
        ),
        # Child-session listing, and the per-parent title lookup that backs the
        # application-level (parent, title) uniqueness check in create_conversation
        # (no DB unique constraint: the check seeks this parent's children here and
        # filters title as a residual).
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
        Uuid16(),
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    response_id: Mapped[str] = mapped_column(String(64))
    # In the PK so deployments can PARTITION BY (created_at) with pure DDL —
    # both PostgreSQL and MySQL require the partition key in the PK and in
    # every unique index. Immutable: items are insert/delete-only.
    created_at: Mapped[int] = mapped_column(Integer, primary_key=True)
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
        # Backs the per-conversation position-ordered scan (the dominant read).
        # Non-unique on purpose: the real position allocator is the next_position
        # counter advanced under _lock_conversation, which never reuses a
        # position; the DB is not relied on to enforce it (nothing catches a
        # collision). Being non-unique also means it needs no partition key, so
        # created_at is left out — the PK still carries it for partition-readiness.
        Index(
            "ix_conversation_items_conversation_id_position",
            "workspace_id",
            "conversation_id",
            "position",
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
        Uuid16(),
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
    # conversation_id leads id in the PK so a conversation's comments stay
    # contiguous for the per-conversation prefix scans that dominate reads
    # (list_for_conversation, fingerprints, cascade delete). This subsumes the
    # old ix_comments_conversation_id secondary index; list_for_conversation's
    # ORDER BY created_at now filesorts the (small) per-conversation row set.
    conversation_id: Mapped[str] = mapped_column(
        Uuid16(),
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
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

    __table_args__ = (CheckConstraint("status IN (1, 2)", name="ck_comments_status"),)


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
        (``session_id IS NULL``). Uniqueness is enforced in the
        store (application layer), not by a DB constraint, and
        keys on ``name_cksum`` rather than this column.
    :param name_cksum: sha256 digest of ``name`` (32 bytes). The
        store's name-uniqueness checks key on this compact digest
        instead of the wide ``VARCHAR(256)`` name, backed by
        ``ix_policies_name_cksum``. Stamped on INSERT by a column
        default; recomputed by the store on rename.
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
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    # sha256(name) — the value the name-uniqueness indexes key on instead of
    # the wide name column. Stamped from `name` on INSERT via the column
    # default; the store recomputes it on rename (defaults don't fire on UPDATE).
    name_cksum: Mapped[bytes] = mapped_column(_CKSUM32, default=_default_policy_name_cksum)
    # Nullable: NULL for server-wide default policies.
    session_id: Mapped[str | None] = mapped_column(
        Uuid16(),
        nullable=True,
    )
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Handler discriminator stored as a stable int code (see
    # omnigent.db.enum_codecs POLICY_TYPE: python=1, url=2).
    type: Mapped[int] = mapped_column(SmallInteger)
    # Dotted import path (type="python") or HTTPS URL
    # (type="url") for the policy handler. Opaque; never SQL-filtered
    # — stored compressed (CompressedText).
    handler: Mapped[str] = mapped_column(CompressedText)
    # JSON-encoded dict of factory kwargs for type="python" when
    # the handler is a factory function. NULL when the handler is
    # a direct callable or for type="url". See the design doc's
    # FunctionRef.arguments pattern. Opaque; never SQL-filtered
    # — stored compressed (CompressedText).
    factory_params: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
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
        # One index serves both listing paths. scope leads (the global-vs-
        # session discriminator), then session_id:
        #   - list_defaults:     WHERE workspace_id=? AND scope='default'
        #   - list_for_session:  WHERE workspace_id=? AND scope='session'
        #                              AND session_id=?
        # scope must precede session_id so the defaults query (which does not
        # constrain session_id) can still seek. Both listings sort their small
        # result set by created_at in memory (created_at is deliberately left
        # out — it can't cover both sorts, see migration d4c1b9e6f3a2).
        # Any future session_id lookup must also constrain scope to seek here.
        Index("ix_policies_scope_session", "workspace_id", "scope", "session_id", "id"),
        # Name uniqueness is enforced in the store, not by a DB constraint:
        # default policies must have globally-unique names while session
        # policies must be unique only within their session — a "unique within
        # a subset" rule that can't be a partial unique index (MySQL has none).
        # The store's create/update checks (session) and create_default/
        # update_default (default) key on name_cksum; this plain index backs
        # those lookups.
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
    :param user_id: User ID from the Databricks auth Bearer token
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
    host_id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    # Session-owner identity from the Databricks auth Bearer token. String(128)
    # matches session_permissions.user_id and every other user-identity column
    # in this schema.
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
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
    # Opaque; never SQL-filtered — stored compressed (CompressedText).
    configured_harnesses: Mapped[str | None] = mapped_column(CompressedText, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (1, 2)",
            name="ck_hosts_status",
        ),
        # (workspace_id, user_id, name) was the old PK; keep it unique so the
        # upsert-on-connect logic (look up by user_id+name to detect host_id
        # rotation) stays consistent.
        UniqueConstraint(
            "workspace_id", "user_id", "name", name="uq_hosts_workspace_user_id_name"
        ),
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
    session on a recurring schedule (``rrule``).

    :param id: UUID primary key stored as 16 raw bytes (see :class:`Uuid16`),
        surfaced as a bare 32-char hex string (no dashes).
    :param name: Human-readable task name, e.g. ``"nightly triage"``.
    :param prompt: The instruction dispatched to the agent on each firing.
    :param rrule: The required RFC 5545 recurrence rule for the recurring
        trigger, e.g. ``"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"``. Evaluated in
        ``timezone``.
    :param user_id: User the spawned session's ``LEVEL_OWNER`` grant is
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
    # RFC 5545 recurrence rule, e.g. "FREQ=DAILY;BYHOUR=9;BYMINUTE=0".
    rrule: Mapped[str] = mapped_column(String(512), nullable=False)
    # Session-owner identity: the spawned run's LEVEL_OWNER grant is written
    # for this user. Nullable — None in single-user/OSS mode (the fire path
    # resolves null to the reserved "local" user). String(128) to match
    # session_permissions.user_id (the column the LEVEL_OWNER grant is
    # written into) and every other user-identity column in this schema.
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Relates to agents.id. No DB foreign key (Rule R032); cascade is app-owned.
    agent_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
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
    # connected_host so existing rows keep connected-host behavior. The store
    # converts to/from the string name at the row↔entity boundary.
    execution_target: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    # For execution_target=connected_host: the specific host to run on (relates
    # to hosts.host_id; No DB foreign key, Rule R032). None = "the owner's
    # freshest online host, whichever". Always None for managed_sandbox (the
    # sandbox is provisioned/adopted under a deterministic id at fire time, so
    # there is nothing to pin here).
    host_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_STATE: active=1, paused=2, deleted=3). The
    # store converts to/from the string name at the row↔entity boundary.
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    last_run_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Relates to conversations.id. No DB foreign key (Rule R032); the
    # application nulls this out when the referenced conversation is deleted.
    last_run_conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("state IN (1, 2, 3)", name="ck_scheduled_tasks_state"),
        CheckConstraint("execution_target IN (1, 2)", name="ck_scheduled_tasks_execution_target"),
        # One user-scoped listing index. Covers "a user's tasks ordered by
        # created_at" (GET /scheduled-tasks) as a covered seek; the scheduler's
        # state scan reads whole rows regardless of any index.
        Index(
            "ix_scheduled_tasks_user_scope",
            "workspace_id",
            "user_id",
            "created_at",
            "id",
        ),
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
    conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
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
        # Reverse lookup conversation_id -> run for the event-driven completion
        # hook (get_running_run_by_conversation), which fires on every turn's
        # terminal edge; without this the lookup is a full-table scan.
        Index(
            "ix_scheduled_task_runs_conversation_id",
            "workspace_id",
            "conversation_id",
        ),
    )
