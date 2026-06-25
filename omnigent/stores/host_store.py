"""
Persistent store for host registrations.

Hosts are machines connected via ``omnigent host``. The store
tracks which hosts have ever connected, their names, owners, and
online/offline status. The ``hosts`` table is the source of truth
for ``GET /v1/hosts`` — all server replicas query it. Live WebSocket
connection state is tracked separately in the in-memory
``HostRegistry`` (one per replica).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import Engine, select, update
from sqlalchemy import delete as sql_delete
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConversation, SqlHost
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch

# A host is considered live only if its row was touched (connect or
# heartbeat) within this window. The host tunnel's ping loop writes a
# heartbeat every PING_INTERVAL_S (30s); three missed heartbeats means
# the host is gone. This freshness gate is the safety net for every
# path that never runs set_offline — hard crash, OOM, deploy/replica
# restart, silent network drop, or a connect that died after the online
# upsert. It must stay >= the tunnel's ping-miss window
# (PING_INTERVAL_S * PING_MISS_THRESHOLD) so a healthy host that is
# still heart-beating is never falsely aged out.
HOST_LIVENESS_TTL_S = 90
HarnessAvailability = bool | str


@dataclass
class Host:
    """
    A registered host machine.

    :param host_id: Stable identifier from the host's local
        ``~/.omnigent/config.yaml``, e.g. ``"host_a1b2c3d4..."``.
    :param name: Human-readable name, e.g. ``"corey-laptop"``.
    :param owner: User ID from the Databricks auth Bearer token,
        e.g. ``"corey.zumar@databricks.com"``.
    :param status: ``"online"`` or ``"offline"``.
    :param created_at: Unix epoch seconds of first registration.
    :param updated_at: Unix epoch seconds the row was last touched —
        a status change (connect/disconnect) or a tunnel heartbeat.
        Used as the host's last-seen for the liveness freshness gate
        (see :data:`HOST_LIVENESS_TTL_S`).
    :param sandbox_provider: Sandbox provider backing a SERVER-MANAGED
        host (``host_type="managed"`` sessions), e.g. ``"modal"``.
        ``None`` for external (user-connected) hosts — non-``None``
        marks the host as server-managed.
    :param sandbox_id: Provider-assigned id of the sandbox currently
        backing a managed host, e.g. ``"sb-a1b2c3"`` — what
        termination is issued against. ``None`` for external hosts.
    :param configured_harnesses: Per-harness readiness reported in the
        host's last ``host.hello`` frame, e.g.
        ``{"claude-sdk": True, "codex": False}``. ``None`` when the
        host has never reported it (older host build) — unknown, not
        "nothing configured".
    """

    host_id: str
    name: str
    owner: str
    status: str
    created_at: int
    updated_at: int
    sandbox_provider: str | None = None
    sandbox_id: str | None = None
    configured_harnesses: dict[str, HarnessAvailability] | None = None


def host_is_live(host: Host, now: int | None = None) -> bool:
    """
    Return whether a :class:`Host` is online and recently seen.

    Pure helper over an already-loaded entity (no DB access), so
    callers that already hold a :class:`Host` — or a list of them —
    don't re-query per row. A host is live only when its ``status`` is
    ``"online"`` **and** its last-seen (``updated_at``) is within
    :data:`HOST_LIVENESS_TTL_S`; the freshness half is what catches a
    host that died without a graceful disconnect.

    :param host: The host entity to evaluate.
    :param now: Unix epoch seconds to measure freshness against;
        defaults to the current time. Pass an explicit value to
        classify many hosts against one consistent clock.
    :returns: ``True`` when the host is online and fresh.
    """
    ref = now if now is not None else now_epoch()
    return host.status == "online" and host.updated_at >= ref - HOST_LIVENESS_TTL_S


_logger = logging.getLogger(__name__)


def _parse_configured_harnesses(raw: str | None) -> dict[str, HarnessAvailability] | None:
    """
    Parse the JSON-encoded ``hosts.configured_harnesses`` column.

    Tolerant: ``NULL``, malformed JSON, or a non-object payload all
    map to ``None`` ("unknown") — a corrupt column value must degrade
    to no-warning in the UI, never break host listing. Entries with a
    non-bool/string value are dropped for the same reason.

    :param raw: The raw column value, e.g.
        ``'{"claude-sdk": true, "codex": false}'`` or ``None``.
    :returns: The readiness map, or ``None`` when absent or unparseable.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("Ignoring malformed hosts.configured_harnesses value")
        return None
    if not isinstance(parsed, dict):
        return None
    return {k: v for k, v in parsed.items() if isinstance(k, str) and isinstance(v, (bool, str))}


def _row_to_host(row: SqlHost) -> Host:
    """
    Convert a :class:`SqlHost` ORM row to a :class:`Host` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Host` dataclass instance.
    """
    return Host(
        host_id=row.host_id,
        name=row.name,
        owner=row.owner,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        sandbox_provider=row.sandbox_provider,
        sandbox_id=row.sandbox_id,
        configured_harnesses=_parse_configured_harnesses(row.configured_harnesses),
    )


def hash_host_launch_token(token: str) -> str:
    """
    Digest a managed-host launch token for storage / lookup.

    Only the digest is ever persisted (``hosts.token_hash``), so a
    database leak does not leak usable credentials, and the
    tunnel-side lookup is by digest — the raw token never touches a
    query.

    :param token: The raw launch token, e.g. the value of
        ``secrets.token_urlsafe(32)``.
    :returns: Hex SHA-256 digest, e.g. ``"9f86d08..."`` (64 chars).
    """
    return hashlib.sha256(token.encode()).hexdigest()


class HostStore:
    """
    Persistent store for host registrations backed by SQLAlchemy.

    :param storage_location: SQLAlchemy database URI, e.g.
        ``"sqlite:///hosts.db"``.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the host store.

        :param storage_location: SQLAlchemy database URI, e.g.
            ``"sqlite:///hosts.db"``.
        """
        self._engine: Engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def upsert_on_connect(
        self,
        host_id: str,
        name: str,
        owner: str,
        *,
        allow_host_id_reown: bool = False,
        configured_harnesses: dict[str, HarnessAvailability] | None = None,
    ) -> Host:
        """
        Register or update a host on WebSocket connect.

        Inserts a new row if ``host_id`` does not exist, otherwise
        updates ``name``, ``owner``, ``status``, and ``updated_at``.
        Called by the host tunnel endpoint when a host sends its
        ``host.hello`` frame.

        The upsert keys on the ``(owner, name)`` primary key, but
        ``host_id`` carries its own UNIQUE constraint. When the same
        physical host re-registers under a *different* owner (e.g. a
        local server respawned with a flipped auth posture changes the
        owner between an accounts user and the reserved ``local`` user),
        the ``(owner, name)`` lookup misses and a plain INSERT would
        collide on ``host_id``. That collision is a deliberate W2-class
        boundary in shared deployments — a different user must not be
        able to claim another user's host_id — so re-owning is gated
        behind *allow_host_id_reown*, which the server sets only for the
        loopback single-user local server. Remote / multi-user servers
        never set it, so the hijack boundary stays intact (the INSERT
        raises ``IntegrityError`` and fails the handshake closed).

        :param host_id: Stable host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param name: Human-readable name from ``config.yaml``, e.g.
            ``"corey-laptop"``.
        :param owner: Authenticated user ID from the Bearer token,
            e.g. ``"corey.zumar@databricks.com"``.
        :param allow_host_id_reown: When ``True`` and a row already
            exists for *host_id* under a different ``(owner, name)``,
            re-own that row in place (preserving the ``host_id`` and its
            conversation bindings) instead of inserting. Intended solely
            for the single-user loopback local server.
        :param configured_harnesses: Per-harness readiness from the
            host's ``host.hello`` frame, e.g. ``{"claude-sdk": True}``.
            Written on every connect — including ``None`` from an older
            host that doesn't report it, which correctly resets any
            stale value back to "unknown".
        :returns: The upserted :class:`Host`.
        """
        now = now_epoch()
        harnesses_json = (
            json.dumps(configured_harnesses) if configured_harnesses is not None else None
        )
        with self._session() as session:
            row = session.get(SqlHost, (owner, name))
            if row is None and allow_host_id_reown:
                reowned = self._reown_host_id(
                    session,
                    host_id=host_id,
                    name=name,
                    owner=owner,
                    configured_harnesses_json=harnesses_json,
                )
                if reowned is not None:
                    return reowned
            if row is not None:
                if row.host_id != host_id:
                    # Same logical host ((owner, name) is the PK) reconnected
                    # with a rotated host_id — e.g. its local identity file
                    # was regenerated after a fresh install or a wiped
                    # ~/.omnigent. host_id is a UNIQUE column that
                    # conversations.host_id references via
                    # fk_conversations_host_id_hosts (ON DELETE SET NULL, NO
                    # ON UPDATE CASCADE). Renaming it in place while child
                    # conversations still point at the old value raises a
                    # ForeignKeyViolation on Postgres, which crashes the host
                    # tunnel handler — the host then reconnect-loops forever
                    # and never registers (no host shows in the UI). SQLite
                    # dev doesn't enforce FKs by default, so this only bites
                    # on the hosted Postgres/Lakebase deploy.
                    #
                    # _rotate_host_id repoints the children across the rename
                    # in this one transaction so the conversation→host binding
                    # survives the identity rotation. It sets row.host_id
                    # itself (ordering matters for the FK), so we only touch
                    # status/timestamp here.
                    self._rotate_host_id(session, row, host_id)
                row.status = "online"
                row.updated_at = now
                row.configured_harnesses = harnesses_json
            else:
                row = SqlHost(
                    owner=owner,
                    name=name,
                    host_id=host_id,
                    status="online",
                    created_at=now,
                    updated_at=now,
                    configured_harnesses=harnesses_json,
                )
                session.add(row)
            return _row_to_host(row)

    @staticmethod
    def _rotate_host_id(session: Session, row: SqlHost, new_host_id: str) -> None:
        """Repoint a host's conversations across a host_id rename.

        Changing ``hosts.host_id`` in place fails when child
        ``conversations`` rows still reference the old value (the FK
        has no ``ON UPDATE CASCADE``). Because ``conversations.host_id``
        is nullable, we bounce it through ``NULL``:

        1. capture the conversation ids bound to the old host_id,
        2. NULL them so nothing references the old host_id,
        3. rename ``row.host_id`` to ``new_host_id`` (now unreferenced),
        4. reattach the captured conversations to ``new_host_id``.

        Flushes between steps so each statement's immediate FK check
        sees a consistent state. Runs inside the caller's transaction,
        so a failure rolls the whole upsert back.

        :param session: The active SQLAlchemy session.
        :param row: The existing host row whose ``host_id`` rotates.
        :param new_host_id: The host_id the host reconnected with.
        """
        old_host_id = row.host_id
        bound_ids = list(
            session.execute(
                select(SqlConversation.id).where(SqlConversation.host_id == old_host_id)
            ).scalars()
        )
        if bound_ids:
            session.execute(
                update(SqlConversation)
                .where(SqlConversation.host_id == old_host_id)
                .values(host_id=None)
            )
            session.flush()
        row.host_id = new_host_id
        session.flush()
        if bound_ids:
            session.execute(
                update(SqlConversation)
                .where(SqlConversation.id.in_(bound_ids))
                .values(host_id=new_host_id)
            )
            session.flush()

    def _reown_host_id(
        self,
        session: Session,
        *,
        host_id: str,
        name: str,
        owner: str,
        configured_harnesses_json: str | None = None,
    ) -> Host | None:
        """Re-own an existing host_id row under a new ``(owner, name)``.

        Used only when ``upsert_on_connect`` opts in via
        ``allow_host_id_reown`` (the single-user loopback local server).
        Updates ``owner``, ``name``, ``status``, and ``updated_at`` on the
        row that already holds *host_id*, leaving ``host_id`` itself
        unchanged so the ``conversations.host_id`` foreign-key bindings
        survive the owner change. ``owner`` / ``name`` are the table's
        primary key, so the change is issued as a Core ``UPDATE`` rather
        than mutating the ORM object's PK in place.

        :param session: The active SQLAlchemy session.
        :param host_id: Host identifier whose row should be re-owned,
            e.g. ``"host_a1b2c3d4..."``.
        :param name: New host name to record, e.g. ``"corey-laptop"``.
        :param owner: New owner to record, e.g. ``"local"`` or
            ``"corey.zumar@databricks.com"``.
        :param configured_harnesses_json: JSON-encoded readiness map from
            the connecting host's hello, e.g.
            ``'{"claude-sdk": true}'``, or ``None`` when unreported.
            Written like the normal connect paths so a re-owned row
            carries fresh (not stale) readiness.
        :returns: The re-owned :class:`Host`, or ``None`` if no row holds
            *host_id* (caller falls through to a normal insert).
        """
        existing = session.execute(
            select(SqlHost).where(SqlHost.host_id == host_id)
        ).scalar_one_or_none()
        if existing is None:
            return None
        created_at = existing.created_at
        now = now_epoch()
        session.execute(
            update(SqlHost)
            .where(SqlHost.host_id == host_id)
            .values(
                owner=owner,
                name=name,
                status="online",
                updated_at=now,
                configured_harnesses=configured_harnesses_json,
            )
        )
        return Host(
            host_id=host_id,
            name=name,
            owner=owner,
            status="online",
            created_at=created_at,
            updated_at=now,
            sandbox_provider=existing.sandbox_provider,
            sandbox_id=existing.sandbox_id,
            configured_harnesses=_parse_configured_harnesses(configured_harnesses_json),
        )

    def set_offline(self, host_id: str) -> None:
        """
        Mark a host as offline when its WebSocket disconnects.

        No-op if the host does not exist (the disconnect callback
        may fire after a failed registration).

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHost).where(SqlHost.host_id == host_id)
            ).scalar_one_or_none()
            if row is not None:
                row.status = "offline"
                row.updated_at = now_epoch()

    def heartbeat(self, host_id: str) -> None:
        """
        Refresh a host's last-seen timestamp while its tunnel is alive.

        Bumps ``updated_at`` to now so the liveness freshness gate
        (see :data:`HOST_LIVENESS_TTL_S`) keeps treating the host as
        online. Called from the host tunnel's ping loop every
        ``PING_INTERVAL_S``. Does not change ``status`` — a host whose
        ping loop is running is, by construction, still ``"online"``.

        No-op if the host does not exist.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        """
        # Single UPDATE rather than SELECT-then-mutate: this runs every
        # ping interval for every connected host, so the extra read is
        # pure overhead. A missing host simply matches no rows (a no-op).
        with self._session() as session:
            session.execute(
                update(SqlHost).where(SqlHost.host_id == host_id).values(updated_at=now_epoch())
            )

    def is_online(self, host_id: str) -> bool:
        """
        Return whether a host is currently live, cross-replica.

        A host counts as live only when its row is ``status="online"``
        **and** its last-seen (``updated_at``) is within
        :data:`HOST_LIVENESS_TTL_S`. The freshness check is what
        catches a host that died without a graceful disconnect: the
        ``status`` flag alone stays ``"online"`` forever in that case
        (set_offline only runs on a clean tunnel close), so a stale
        timestamp is the only reliable signal that the host is gone.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :returns: ``True`` when the host is online and its last-seen is
            fresh; ``False`` if unknown, offline, or stale.
        """
        row = self.get_host(host_id)
        return row is not None and host_is_live(row)

    def online_host_ids(self, host_ids: list[str]) -> set[str]:
        """
        Return the subset of ``host_ids`` that are currently live.

        Bulk variant of :meth:`is_online` for the sidebar online-dot
        batch path: one ``SELECT ... WHERE host_id IN (...)`` instead
        of a per-host query. Liveness applies the same
        status-plus-freshness gate as :meth:`is_online`, classifying
        every row against one consistent clock.

        :param host_ids: Host identifiers to check, e.g.
            ``["host_abc123", "host_def456"]``. Duplicates are
            tolerated; empty input returns an empty set without
            touching the database.
        :returns: The set of ids whose host row is online and fresh.
            Unknown, offline, or stale ids are absent.
        """
        if not host_ids:
            return set()
        unique_ids = list(set(host_ids))
        ref = now_epoch()
        with self._session() as session:
            rows = session.execute(
                select(SqlHost.host_id, SqlHost.status, SqlHost.updated_at).where(
                    SqlHost.host_id.in_(unique_ids)
                )
            ).all()
        return {
            row.host_id
            for row in rows
            if row.status == "online" and row.updated_at >= ref - HOST_LIVENESS_TTL_S
        }

    def list_hosts(self, owner: str) -> list[Host]:
        """
        List all hosts owned by a specific user.

        Returns both online and offline hosts, ordered by
        ``updated_at`` descending (most recently active first).

        :param owner: User ID to filter by, e.g.
            ``"corey.zumar@databricks.com"``.
        :returns: List of :class:`Host` entities.
        """
        with self._session() as session:
            rows = (
                session.query(SqlHost)
                .filter(SqlHost.owner == owner)
                .order_by(SqlHost.updated_at.desc())
                .all()
            )
            return [_row_to_host(row) for row in rows]

    def get_host(self, host_id: str) -> Host | None:
        """
        Fetch a single host by ID.

        :param host_id: Host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :returns: The :class:`Host` if found, otherwise ``None``.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHost).where(SqlHost.host_id == host_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            return _row_to_host(row)

    def register_managed_host(
        self,
        *,
        host_id: str,
        name: str,
        owner: str,
        token: str,
        provider: str,
        sandbox_id: str,
        token_expires_at: int,
    ) -> Host:
        """
        Pre-register a server-managed sandbox host with its credential.

        Called by the managed-launch orchestration after the sandbox is
        provisioned and BEFORE the in-sandbox host process starts, so
        the launch token is resolvable by the time the host first dials
        the tunnel. The row is created ``"offline"``; the tunnel's
        normal ``upsert_on_connect`` flips it online when the host
        registers.

        If a row already exists for *host_id* (a RELAUNCH: the host
        identity is durable across sandbox generations so session
        bindings survive a dead sandbox), the credential and sandbox
        columns are overwritten in place — which atomically revokes the
        previous generation's token, since its digest no longer matches
        anything.

        :param host_id: Server-generated host identifier, e.g.
            ``"host_a1b2c3d4..."``.
        :param name: Display name for the host picker, e.g.
            ``"managed-a1b2c3d4"``. Part of the table's
            ``(owner, name)`` primary key.
        :param owner: User the managed host acts for, e.g.
            ``"alice@example.com"``.
        :param token: The RAW launch token (hashed here, never stored),
            e.g. the value of ``secrets.token_urlsafe(32)``.
        :param provider: Sandbox provider name, e.g. ``"modal"``.
        :param sandbox_id: Provider-assigned sandbox id, e.g.
            ``"sb-a1b2c3"``.
        :param token_expires_at: Unix epoch seconds after which the
            token no longer authenticates.
        :returns: The registered :class:`Host`.
        :raises ValueError: If a row for *host_id* exists under a
            DIFFERENT owner — a relaunch may only re-credential a host
            the same user owns.
        """
        now = now_epoch()
        token_hash = hash_host_launch_token(token)
        with self._session() as session:
            existing = session.execute(
                select(SqlHost).where(SqlHost.host_id == host_id)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.owner != owner:
                    # Fail closed (W2-class boundary): re-crediting a host
                    # row hands its launch token holder the row owner's
                    # identity, so a cross-owner overwrite would be a host
                    # hijack. host_id is server-generated today (uuid4 per
                    # launch), so this can only fire on a bug or a forged
                    # id — refuse rather than re-own.
                    raise ValueError(
                        f"host {host_id!r} is registered to a different owner; "
                        "refusing to re-credential it"
                    )
                existing.token_hash = token_hash
                existing.token_expires_at = token_expires_at
                existing.sandbox_provider = provider
                existing.sandbox_id = sandbox_id
                existing.updated_at = now
                return _row_to_host(existing)
            row = SqlHost(
                owner=owner,
                name=name,
                host_id=host_id,
                status="offline",
                created_at=now,
                updated_at=now,
                token_hash=token_hash,
                token_expires_at=token_expires_at,
                sandbox_provider=provider,
                sandbox_id=sandbox_id,
            )
            session.add(row)
            return _row_to_host(row)

    def resolve_launch_token(self, token: str) -> Host | None:
        """
        Resolve a presented launch token to its managed host, if valid.

        The host tunnel's auth path for managed hosts. Lookup is by
        SHA-256 digest — the comparison happens inside an indexed
        equality query on a uniformly distributed hash, which is not
        byte-by-byte comparable from the network (the standard
        reset-token pattern; no timing oracle on the raw token).
        Expired tokens do not authenticate; the expiry is checked
        atomically with the lookup.

        :param token: The raw token presented by a connecting host.
        :returns: The matching :class:`Host` whose token is unexpired,
            or ``None`` when the token is unknown or expired.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHost).where(SqlHost.token_hash == hash_host_launch_token(token))
            ).scalar_one_or_none()
            # token_expires_at is written together with token_hash, so a
            # matched row always carries it; the None arm is mypy
            # narrowing that doubles as fail-closed.
            if row is None or row.token_expires_at is None or row.token_expires_at < now_epoch():
                return None
            return _row_to_host(row)

    def delete_host(self, host_id: str) -> None:
        """
        Delete a host row entirely.

        Managed-host teardown: removes the host from the picker AND
        revokes its launch token in one operation (the row IS the
        credential). ``conversations.host_id`` references this row with
        ``ON DELETE SET NULL``, so any remaining session bindings are
        nulled rather than blocking the delete. No-op when the row does
        not exist — deletion is invoked from best-effort cleanup paths
        that may race.

        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        """
        with self._session() as session:
            session.execute(sql_delete(SqlHost).where(SqlHost.host_id == host_id))

    def revoke_launch_token(self, host_id: str) -> None:
        """
        Clear a managed host's launch credential, keeping the row.

        Relaunch-failure cleanup: a failed sandbox RELAUNCH must revoke
        the token it armed (the new sandbox never came up to use it)
        without deleting the durable host row — the session binding
        survives, and the next relaunch attempt re-arms a fresh token
        via :meth:`register_managed_host`. Contrast :meth:`delete_host`,
        which is full teardown. No-op when the row does not exist.

        :param host_id: Host identifier, e.g. ``"host_a1b2c3d4..."``.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlHost).where(SqlHost.host_id == host_id)
            ).scalar_one_or_none()
            if row is None:
                return
            row.token_hash = None
            row.token_expires_at = None
            row.updated_at = now_epoch()
