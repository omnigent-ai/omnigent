"""Persistence for the ``accounts`` auth provider.

Sibling to :class:`omnigent.stores.permission_store.PermissionStore`
— same database, separate API surface. Lives here (not under
``stores/``) because it's a server-only concept: only the accounts
provider's routes and bootstrap touch it, never the runtime or the
runner. Internal hosted deploys that run header/OIDC don't
instantiate this store at all, so the new code path is invisible
to them.

The split is deliberate. PermissionStore is a stable contract that
many subsystems depend on (permission checks, session lookups,
admin-flag gating) and polluting it with accounts-specific methods
muddles that boundary. Accounts mode owns its own persistence
surface; PermissionStore stays exactly as it is on ``main``.

Schema:

- Reads / writes three columns on the existing ``users`` table —
  ``password_hash``, ``created_at``, ``last_login_at`` — added by
  the ``g1a2b3c4d5e6`` migration. Those columns are nullable, so
  rows created in header/OIDC mode (where ``PermissionStore.ensure_user``
  is the writer) leave them unset and accounts-specific reads
  return ``None``.
- Owns the ``account_tokens`` table outright — invite + magic-login
  tokens, atomic single-use via ``UPDATE … WHERE redeemed_at IS NULL``.
"""

from __future__ import annotations

import time

from sqlalchemy import and_, delete, exists, select, update
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    SqlAccountToken,
    SqlSessionPermission,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.enum_codecs import decode_account_token_kind, encode_account_token_kind
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.entities import Account, AccountToken
from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC

_HIDDEN_LIST_USERS = frozenset({RESERVED_USER_PUBLIC, RESERVED_USER_LOCAL})


def _to_account(row: SqlUser) -> Account:
    """Convert a :class:`SqlUser` ORM row to an :class:`Account` entity.

    Strips ``password_hash`` — it never leaves the store via this
    conversion. Callers that need the hash use
    :meth:`SqlAlchemyAccountStore.get_password_hash` explicitly.
    """
    return Account(
        id=row.id,
        is_admin=row.is_admin,
        created_at=row.created_at,
        last_login_at=row.last_login_at,
        has_password=row.password_hash is not None,
    )


def _to_account_token(row: SqlAccountToken) -> AccountToken:
    """Convert a :class:`SqlAccountToken` row to a domain entity."""
    return AccountToken(
        id=row.id,
        kind=decode_account_token_kind(row.kind),
        user_id=row.user_id,
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        invited_is_admin=row.invited_is_admin,
    )


class SqlAlchemyAccountStore:
    """SQLAlchemy-backed persistence for accounts-mode credentials and tokens.

    Concrete class (no ABC) — accounts persistence has exactly one
    backend today and a Protocol can be extracted later if a second
    appears. Constructor matches PermissionStore so the wiring in
    ``create_app`` is mechanical.

    :param storage_location: SQLAlchemy database URI, e.g.
        ``"sqlite:///omnigent.db"``. Shares the connection pool
        with PermissionStore via :func:`get_or_create_engine`.
    """

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    # ── User credentials (extends rows in the `users` table) ──────

    def create_user_with_password(
        self,
        user_id: str,
        password_hash: str,
        *,
        is_admin: bool = False,
    ) -> Account:
        """Insert a user row with a password hash.

        Used by ``/auth/register`` (invite redemption), by the
        first-boot admin bootstrap, and by admin "create user"
        flows. Raises if the user already exists — registration
        UX should check uniqueness first to give a clean error.

        :param user_id: Chosen username, e.g. ``"alice"``.
        :param password_hash: Pre-hashed password (see
            :mod:`omnigent.server.passwords`). Plaintext never
            crosses this boundary.
        :param is_admin: Admin flag at creation. Defaults False;
            the first-boot admin bootstrap passes True.
        :returns: The created :class:`Account`.
        :raises ValueError: If a user with this id already exists.
        """
        now = int(time.time())
        with self._session() as session:
            existing = session.get(SqlUser, (current_workspace_id(), user_id))
            if existing is not None:
                raise ValueError(f"user {user_id!r} already exists")
            row = SqlUser(
                id=user_id,
                is_admin=is_admin,
                password_hash=password_hash,
                created_at=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                # TOCTOU: another worker / request inserted the same
                # user_id between our SELECT and our INSERT. Surface
                # as the same ValueError the SELECT path raises so
                # callers handle uniqueness violation in one place.
                raise ValueError(f"user {user_id!r} already exists") from exc
            return _to_account(row)

    def get_user(self, user_id: str) -> Account | None:
        """Look up a user by id. Returns ``None`` if missing."""
        with self._session() as session:
            row = session.get(SqlUser, (current_workspace_id(), user_id))
            return _to_account(row) if row is not None else None

    def is_admin(self, user_id: str) -> bool:
        """Whether ``user_id`` has the admin flag set.

        Duplicates :meth:`PermissionStore.is_admin` reading the
        same column on ``users`` — kept here so the accounts
        routes don't have to wire in a PermissionStore reference
        just to gate admin endpoints. The two stores agree by
        construction (single source of truth: the column).
        """
        with self._session() as session:
            row = session.get(SqlUser, (current_workspace_id(), user_id))
            return row is not None and row.is_admin

    def set_admin(self, user_id: str, is_admin: bool) -> None:
        """Set the admin flag on an existing user row.

        The accounts-mode counterpart to
        :meth:`PermissionStore.set_admin` — both write the same
        ``users.is_admin`` column (single source of truth). Used by
        the file-backed admin-list promotion at login
        (:func:`omnigent.server.admin_list.promote_if_listed`), which
        only ever promotes (passes ``True``). No-op if the row is
        missing (the login path ensures it first).

        :param user_id: The username to update, e.g. ``"alice"``.
        :param is_admin: The flag value to set.
        """
        with self._session() as session:
            session.execute(
                update(SqlUser)
                .where(
                    SqlUser.workspace_id == current_workspace_id(),
                    SqlUser.id == user_id,
                )
                .values(is_admin=is_admin)
            )

    def list_users(self) -> list[Account]:
        """Return all users for the admin members page.

        Excludes two sentinel rows that aren't actionable in
        accounts mode:

        - ``"__public__"`` — anonymous-grant sentinel, never a
          real user.
        - ``"local"`` — backfilled by the original session-permissions
          migration so pre-accounts deploys had a default owner row
          for existing conversations. In accounts mode the name is
          reserved (can't authenticate, can't be reset, can't be
          promoted), so showing it as an "External" member on the
          Members page is dead weight. The row stays in the DB so
          a deploy that ever flipped back to header single-user
          mode would still find its legacy permission grants.

        Result is unordered; UI sorts.
        """
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlUser).where(SqlUser.workspace_id == current_workspace_id())
                )
                .scalars()
                .all()
            )
            return [_to_account(r) for r in rows if r.id not in _HIDDEN_LIST_USERS]

    def delete_user(self, user_id: str) -> bool:
        """Delete a user row and their permission grants.

        Explicitly deletes all ``session_permissions`` rows for the user
        before removing the user row — the DB no longer cascades this.

        :returns: ``True`` if a user row was deleted, ``False`` otherwise.
        """
        with self._session() as session:
            session.execute(
                delete(SqlSessionPermission).where(
                    SqlSessionPermission.workspace_id == current_workspace_id(),
                    SqlSessionPermission.user_id == user_id,
                )
            )
            result = session.execute(
                delete(SqlUser).where(
                    SqlUser.workspace_id == current_workspace_id(),
                    SqlUser.id == user_id,
                )
            )
            return result.rowcount > 0

    def get_password_hash(self, user_id: str) -> str | None:
        """Fetch a user's password hash for verification.

        ONLY method that surfaces the hash. Routes that call this
        must pass the result straight into
        :func:`omnigent.server.passwords.verify_password` — never
        log, return, or store the value elsewhere.
        """
        with self._session() as session:
            row = session.get(SqlUser, (current_workspace_id(), user_id))
            return row.password_hash if row is not None else None

    def update_password(self, user_id: str, password_hash: str) -> None:
        """Replace a user's stored password hash.

        Used by self-serve ``/auth/users/me/password`` and
        admin-initiated reset. No-op silently if the user does
        not exist (the route should 404 first).
        """
        with self._session() as session:
            session.execute(
                update(SqlUser)
                .where(
                    SqlUser.workspace_id == current_workspace_id(),
                    SqlUser.id == user_id,
                )
                .values(password_hash=password_hash)
            )

    def mark_logged_in(self, user_id: str, when_epoch_seconds: int) -> None:
        """Bump ``last_login_at`` on every successful login.

        :param when_epoch_seconds: Login timestamp. Tests pass a
            fixed value for determinism.
        """
        with self._session() as session:
            session.execute(
                update(SqlUser)
                .where(
                    SqlUser.workspace_id == current_workspace_id(),
                    SqlUser.id == user_id,
                )
                .values(last_login_at=when_epoch_seconds)
            )

    # ── Account tokens (invite + magic-link) ──────────────────────

    def create_token(
        self,
        token_id: str,
        *,
        kind: str,
        user_id: str | None,
        created_by: str | None,
        created_at: int,
        expires_at: int,
        invited_is_admin: bool = False,
    ) -> AccountToken:
        """Persist a new invite or magic token.

        The token id (the secret) is generated by the caller —
        see :func:`secrets.token_urlsafe`. The store does not
        validate entropy. Bounds:

        - ``kind`` must be ``"invite"`` or ``"magic"``
          (enforced by a DB check constraint).
        - For ``"invite"``, ``user_id`` is ``None`` and
          ``created_by`` is the admin's id.
        - For ``"magic"``, ``user_id`` is the user being signed
          in and ``created_by`` is ``None`` (self-issued).

        :raises ValueError: On an unknown kind. Fail fast so the
            DB-level error never has to surface to the route.
        """
        if kind not in ("invite", "magic"):
            raise ValueError(f"unknown token kind {kind!r}")
        with self._session() as session:
            row = SqlAccountToken(
                id=token_id,
                kind=encode_account_token_kind(kind),
                user_id=user_id,
                created_by=created_by,
                created_at=created_at,
                expires_at=expires_at,
                invited_is_admin=invited_is_admin,
            )
            session.add(row)
            session.flush()
            return _to_account_token(row)

    def redeem_token(
        self, token_id: str, *, kind: str, now_epoch_seconds: int
    ) -> AccountToken | None:
        """Atomically mark a token as redeemed.

        A naive "SELECT then UPDATE" race would let two concurrent
        requests both succeed. A single
        ``UPDATE … WHERE redeemed_at IS NULL`` + rowcount check
        makes the redeem step itself atomic — at most one caller
        sees ``rowcount == 1`` even under concurrent redeem
        attempts.

        Returns ``None`` for missing / wrong-kind / already-redeemed
        / expired tokens. Caller can't distinguish (intentional —
        opaque-to-bruteforce-guessing).
        """
        with self._session() as session:
            result = session.execute(
                update(SqlAccountToken)
                .where(
                    and_(
                        SqlAccountToken.workspace_id == current_workspace_id(),
                        SqlAccountToken.id == token_id,
                        SqlAccountToken.kind == encode_account_token_kind(kind),
                        SqlAccountToken.redeemed_at.is_(None),
                        SqlAccountToken.expires_at > now_epoch_seconds,
                    )
                )
                .values(redeemed_at=now_epoch_seconds)
            )
            if result.rowcount == 0:
                return None
            row = session.get(SqlAccountToken, (current_workspace_id(), token_id))
            return _to_account_token(row) if row is not None else None

    def purge_expired_tokens(self, now_epoch_seconds: int) -> int:
        """Delete tokens whose ``expires_at`` is in the past.

        Called periodically (e.g. on app startup) so the table
        doesn't accumulate stale rows. Single-use enforcement is
        via ``redeemed_at`` regardless of expiry, so purging is
        purely housekeeping.

        :returns: The number of rows deleted.
        """
        with self._session() as session:
            result = session.execute(
                delete(SqlAccountToken).where(
                    SqlAccountToken.workspace_id == current_workspace_id(),
                    SqlAccountToken.expires_at <= now_epoch_seconds,
                )
            )
            return result.rowcount

    # ── OIDC invited emails (opt-in pre-authorization) ────────────
    #
    # No dedicated table: the OIDC invite reuses the existing
    # ``account_tokens`` rows (kind="invite"). The
    # single-use token is minted with ``user_id=NULL``; at the OIDC
    # callback we atomically redeem it AND stamp the redeeming email
    # into ``user_id``. That stamped, redeemed row IS the durable
    # pre-authorization — ``is_email_invited`` just looks for one. This
    # keeps the OSS-only invite feature from adding a table that would
    # ship (empty, unused) into the hosted / Databricks-Apps schema.

    def redeem_oidc_invite(self, token_id: str, email: str, *, now_epoch_seconds: int) -> bool:
        """Atomically redeem an OIDC invite token and bind it to ``email``.

        A single ``UPDATE … WHERE redeemed_at IS NULL`` makes redemption
        single-use even under concurrent callbacks, and stamps
        ``user_id=email`` so the redeemed row doubles as the durable
        pre-authorization that :meth:`is_email_invited` later finds.

        :param token_id: The invite token secret from the invite URL.
        :param email: The IdP-returned email, lowercased by the caller,
            e.g. ``"contractor@gmail.com"``.
        :param now_epoch_seconds: Current time; the token must not be
            expired or already redeemed.
        :returns: ``True`` if this call redeemed the token, ``False`` if
            it was missing / wrong-kind / already-redeemed / expired.
        """
        with self._session() as session:
            result = session.execute(
                update(SqlAccountToken)
                .where(
                    and_(
                        SqlAccountToken.workspace_id == current_workspace_id(),
                        SqlAccountToken.id == token_id,
                        SqlAccountToken.kind == encode_account_token_kind("invite"),
                        SqlAccountToken.redeemed_at.is_(None),
                        SqlAccountToken.expires_at > now_epoch_seconds,
                    )
                )
                .values(redeemed_at=now_epoch_seconds, user_id=email)
            )
            return result.rowcount == 1

    def is_email_invited(self, email: str) -> bool:
        """Whether ``email`` redeemed an OIDC invite (durable pre-auth).

        Looks for a redeemed invite token stamped with this email by
        :meth:`redeem_oidc_invite`. Persists across logins, so an invited
        off-domain user stays admitted. Accounts-mode invites leave
        ``user_id`` NULL, so they never match here.

        :param email: The email to check, lowercased, e.g.
            ``"contractor@gmail.com"``.
        :returns: ``True`` if a redeemed invite token is bound to it.
        """
        with self._session() as session:
            return session.execute(
                select(
                    exists().where(
                        and_(
                            SqlAccountToken.workspace_id == current_workspace_id(),
                            SqlAccountToken.kind == encode_account_token_kind("invite"),
                            SqlAccountToken.user_id == email,
                            SqlAccountToken.redeemed_at.is_not(None),
                        )
                    )
                )
            ).scalar_one()
