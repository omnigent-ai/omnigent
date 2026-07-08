"""Provider-agnostic identity remap for the accounts → OIDC switch.

When a deployment moves from the ``accounts`` provider to ``oidc``, the
identity *key* changes: accounts keys a user by their chosen username
(``"alice"``, ``getpass.getuser()`` → ``"dhruv.gupta"``), while OIDC
keys them by the IdP-returned email (``"alice@example.com"``). Every
row that references a user id — permission grants, conversation
ownership-by-grant, comments, policies, invite/magic tokens, host
ownership — is keyed on the *old* string. A naive provider switch would
make the same human a brand-new principal: not admin, and unable to see
any of their own sessions.

This module rewrites those identity strings in one transaction so the
team keeps its admin and its data across the switch. It operates purely
on the relational tables (it does not import the accounts or OIDC
providers), so it works regardless of which provider originally wrote
the rows, and the same code migrates a Postgres or a SQLite deployment.

User-id-bearing columns rewritten (the full set as of this schema):

- ``users.id`` (the principal row — PK)
- ``session_permissions.user_id`` (FK → users.id; PK part)
- ``account_tokens.user_id`` and ``account_tokens.created_by``
- ``comments.created_by``
- ``policies.created_by``
- ``hosts.owner`` (PK part)

Ordering within a mapping is load-bearing: the new ``users`` row is
created first (so FK-bearing children can point at it), children are
repointed next, and the old ``users`` row is deleted last (so the
``ON DELETE CASCADE`` on ``session_permissions`` never fires against
grants we still need).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlAccountToken,
    SqlComment,
    SqlHost,
    SqlPolicy,
    SqlSessionPermission,
    SqlUser,
    current_workspace_id,
)
from omnigent.server.auth import _RESERVED_USERS


@dataclass
class RemapReport:
    """What an identity remap did (or would do, for a dry run).

    :param mapping: The resolved ``old -> new`` identity map applied,
        e.g. ``{"alice": "alice@example.com"}``.
    :param per_table: Count of rows changed per table, e.g.
        ``{"users": 2, "session_permissions": 5, "comments": 1}``.
        Only tables that changed appear.
    :param skipped_missing: Old ids that had no ``users`` row (nothing
        to migrate), e.g. ``["ghost"]``.
    :param refused: ``old -> new`` pairs skipped because ``new`` already
        existed as a distinct user and ``force`` was not set, e.g.
        ``["alice -> bob@example.com"]``.
    :param committed: ``True`` if the transaction was committed,
        ``False`` for a dry run (changes rolled back).
    """

    mapping: dict[str, str]
    per_table: dict[str, int] = field(default_factory=dict)
    skipped_missing: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    committed: bool = False

    def _bump(self, table: str, n: int = 1) -> None:
        """Increment the per-table counter (internal helper).

        :param table: Table name key, e.g. ``"users"``.
        :param n: Amount to add (default 1).
        """
        if n:
            self.per_table[table] = self.per_table.get(table, 0) + n


def build_domain_mapping(engine: Engine, domain: str) -> dict[str, str]:
    """Build an ``old -> new`` map appending ``@domain`` to bare usernames.

    Reads every ``users`` row and maps each bare username (no ``@``) to
    ``username@domain``. Rows whose id already contains ``@`` (already
    an email) and the reserved sentinels (``"local"``, ``"__public__"``)
    are skipped — they need no remap.

    :param engine: SQLAlchemy engine bound to the target database.
    :param domain: Email domain to append, e.g. ``"example.com"``
        (a leading ``@`` is tolerated and stripped).
    :returns: The mapping, e.g. ``{"alice": "alice@example.com"}``.
    """
    domain = domain.lstrip("@").strip().lower()
    mapping: dict[str, str] = {}
    with Session(engine) as session:
        ids = (
            session.execute(
                select(SqlUser.id).where(SqlUser.workspace_id == current_workspace_id())
            )
            .scalars()
            .all()
        )
    for uid in ids:
        if "@" in uid or uid in _RESERVED_USERS:
            continue
        mapping[uid] = f"{uid}@{domain}"
    return mapping


def remap_identities(
    engine: Engine,
    mapping: dict[str, str],
    *,
    dry_run: bool = True,
    force: bool = False,
) -> RemapReport:
    """Rewrite user-identity strings across the database in one transaction.

    For each ``old -> new`` pair (``old == new`` is skipped):

    1. If no ``users`` row exists for ``old``, record it in
       ``skipped_missing`` and move on (nothing references a
       non-existent user — the FK guarantees it).
    2. Ensure the ``new`` ``users`` row exists. If ``new`` is absent, a
       row is created carrying ``old``'s ``is_admin`` / ``password_hash``
       / timestamps. If ``new`` already exists as a *distinct* user, the
       pair is **refused** unless ``force`` is set (avoids silently
       merging two people / privilege levels); with ``force`` the rows
       merge and ``is_admin`` becomes ``old OR new``.
    3. Repoint every user-id-bearing child column from ``old`` to ``new``
       (grants merge by max level on a conversation collision).
    4. Delete the ``old`` ``users`` row.

    :param engine: SQLAlchemy engine bound to the target database.
    :param mapping: ``old -> new`` identity map, e.g.
        ``{"alice": "alice@example.com"}``.
    :param dry_run: When ``True`` (default), all changes are rolled back
        and the report reflects what *would* happen. ``False`` commits.
    :param force: Allow merging onto an existing distinct ``new`` user.
    :returns: A :class:`RemapReport` describing the outcome.
    """
    report = RemapReport(mapping=dict(mapping))

    with Session(engine) as session:
        for old_id, new_id in mapping.items():
            if old_id == new_id:
                continue

            old_user = session.get(SqlUser, (current_workspace_id(), old_id))
            if old_user is None:
                report.skipped_missing.append(old_id)
                continue

            new_user = session.get(SqlUser, (current_workspace_id(), new_id))
            if new_user is not None:
                if not force:
                    report.refused.append(f"{old_id} -> {new_id}")
                    continue
                # Merge: the surviving (new) row gains admin if either had it.
                new_user.is_admin = new_user.is_admin or old_user.is_admin
                report._bump("users")  # merged
            else:
                session.add(
                    SqlUser(
                        id=new_id,
                        is_admin=old_user.is_admin,
                        password_hash=old_user.password_hash,
                        created_at=old_user.created_at,
                        last_login_at=old_user.last_login_at,
                    )
                )
                session.flush()  # new row must exist before children repoint
                report._bump("users")  # created

            # ── session_permissions: per-row so a (new, conv) collision
            # merges to the higher level instead of violating the PK.
            old_grants = (
                session.execute(
                    select(SqlSessionPermission).where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.user_id == old_id,
                    )
                )
                .scalars()
                .all()
            )
            for grant in old_grants:
                existing = session.get(
                    SqlSessionPermission, (current_workspace_id(), new_id, grant.conversation_id)
                )
                if existing is not None:
                    if grant.level > existing.level:
                        existing.level = grant.level
                    session.delete(grant)
                else:
                    grant.user_id = new_id
                report._bump("session_permissions")
            session.flush()

            # ── Non-PK reference columns: bulk UPDATE is safe (no
            # uniqueness on these columns).
            for model, column in (
                (SqlComment, SqlComment.created_by),
                (SqlPolicy, SqlPolicy.created_by),
            ):
                result = session.execute(
                    update(model)
                    .where(model.workspace_id == current_workspace_id(), column == old_id)
                    .values(created_by=new_id)
                )
                report._bump(model.__tablename__, result.rowcount or 0)

            # account_tokens has two id columns to repoint.
            for column_name in ("user_id", "created_by"):
                column = getattr(SqlAccountToken, column_name)
                result = session.execute(
                    update(SqlAccountToken)
                    .where(
                        SqlAccountToken.workspace_id == current_workspace_id(),
                        column == old_id,
                    )
                    .values(**{column_name: new_id})
                )
                report._bump(SqlAccountToken.__tablename__, result.rowcount or 0)

            # ── hosts.owner is a PK part (owner, name); a collision with
            # an existing (new, name) host would violate the PK, so guard
            # per-row. Rare in OSS (hosts are a Databricks-connect
            # feature), but correctness over assumption.
            old_hosts = (
                session.execute(
                    select(SqlHost).where(
                        SqlHost.workspace_id == current_workspace_id(),
                        SqlHost.owner == old_id,
                    )
                )
                .scalars()
                .all()
            )
            for host in old_hosts:
                # Check if the new owner already has a host with the same name
                # (collision on the uq_hosts_workspace_owner_name unique constraint).
                # PK is now (workspace_id, host_id) so we SELECT by the unique key.
                clash = session.execute(
                    select(SqlHost).where(
                        SqlHost.workspace_id == current_workspace_id(),
                        SqlHost.owner == new_id,
                        SqlHost.name == host.name,
                    )
                ).scalar_one_or_none()
                if clash is not None:
                    session.delete(host)  # new owner already has this host name
                else:
                    host.owner = new_id
                report._bump("hosts")
            session.flush()

            # ── Finally remove the old principal. Children are already
            # repointed, so the ON DELETE CASCADE on session_permissions
            # has nothing left to take down.
            session.delete(old_user)
            report._bump("users_deleted")
            session.flush()

        if dry_run:
            session.rollback()
            report.committed = False
        else:
            session.commit()
            report.committed = True

    return report
