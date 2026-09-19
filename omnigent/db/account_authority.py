"""Account-generation checks shared by authentication and authority writes.

An authenticated request captures a generation once. Background tasks inherit
that context; durable jobs must restore their saved generation before running.
Writers lock the account before their resource rows, using an immediate
transaction on SQLite. Deletion uses the same ordering.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlSessionPermission, SqlUser, current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError


@dataclass(frozen=True)
class AccountAuthority:
    user_id: str
    generation: str | None
    workspace_id: int


_authority: ContextVar[AccountAuthority | None] = ContextVar("account_authority", default=None)
_targets: ContextVar[tuple[AccountAuthority, ...]] = ContextVar("account_targets", default=())


def bind_account_authority(user_id: str, generation: str) -> None:
    """Capture the identity authenticated by this request or durable job."""
    _authority.set(AccountAuthority(user_id, generation, current_workspace_id()))


def clear_account_authority() -> None:
    """Start authentication without borrowing an earlier identity."""
    _authority.set(None)
    _targets.set(())


def current_account_user() -> str | None:
    authority = _authority.get()
    if authority and authority.workspace_id == current_workspace_id():
        return authority.user_id
    return None


def account_generation(user_id: str) -> str | None:
    authority = _authority.get()
    if (
        authority
        and authority.workspace_id == current_workspace_id()
        and authority.user_id == user_id
    ):
        return authority.generation
    return None


@contextmanager
def account_authority_scope(user_id: str | None, generation: str | None) -> Iterator[None]:
    authority = (
        AccountAuthority(user_id, generation, current_workspace_id())
        if user_id is not None
        else None
    )
    token = _authority.set(authority)
    try:
        yield
    finally:
        _authority.reset(token)


@contextmanager
def target_account_scope(user_id: str, generation: str | None) -> Iterator[None]:
    """Pin a separately looked-up target without replacing the acting identity."""
    target = AccountAuthority(user_id, generation, current_workspace_id())
    token = _targets.set((*_targets.get(), target))
    try:
        yield
    finally:
        _targets.reset(token)


def lock_account(session: Session, user_id: str) -> SqlUser | None:
    """Lock before touching grants, hosts, or other owned resources."""
    return session.execute(
        select(SqlUser)
        .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def session_account_owner_query() -> Select[tuple[str, str | None]]:
    """Select live accounts registrations with an explicit session-owner grant."""
    from omnigent.server.auth import LEVEL_OWNER

    return (
        select(
            SqlUser.id.label("session_owner"),
            SqlUser.account_generation.label("session_generation"),
        )
        .select_from(SqlSessionPermission)
        .join(
            SqlUser,
            and_(
                SqlUser.workspace_id == SqlSessionPermission.workspace_id,
                SqlUser.id == SqlSessionPermission.user_id,
            ),
        )
        .where(
            SqlSessionPermission.workspace_id == current_workspace_id(),
            SqlSessionPermission.level == LEVEL_OWNER,
            SqlUser.deleted_at.is_(None),
            SqlUser.account_generation.is_not(None),
        )
        .order_by(SqlUser.id)
    )


class _GenerationDefault(Enum):
    CONTEXT = "context"


def require_active_account(
    session: Session,
    user_id: str | None,
    *,
    generation: str | None | _GenerationDefault = _GenerationDefault.CONTEXT,
    related_accounts: Mapping[str, str | None] | None = None,
) -> str | None:
    """Validate the captured generation under the writer's account lock.

    Identities without an accounts row keep the header/OIDC/machine behavior.
    An accounts request cannot create a missing row or cross a re-registration.
    Related resource owners join the same ordered lock set as actor and target.
    """
    authority = _authority.get()
    checks: dict[str, list[str | None]] = {}
    if authority is not None and authority.workspace_id == current_workspace_id():
        checks[authority.user_id] = [authority.generation]
    target_ids: set[str] = set()
    for target in _targets.get():
        if target.workspace_id == current_workspace_id():
            checks.setdefault(target.user_id, []).append(target.generation)
            target_ids.add(target.user_id)
    for related_id, related_generation in (related_accounts or {}).items():
        checks.setdefault(related_id, []).append(related_generation)
    if user_id is not None:
        checks.setdefault(user_id, [])
        if isinstance(generation, str) or generation is None:
            checks[user_id].append(generation)
    current_generation = None
    revoked: set[str] = set()
    for checked_id in sorted(checks):
        row = lock_account(session, checked_id)
        if (row is not None and row.deleted_at is not None) or any(
            (row.account_generation if row else None) != expected
            for expected in checks[checked_id]
        ):
            revoked.add(checked_id)
        if checked_id == user_id and row is not None:
            current_generation = row.account_generation
    if revoked:
        # A stale target is a retryable operation conflict; a revoked actor
        # still requires authentication even when a target also changed.
        if revoked <= target_ids and current_account_user() not in revoked:
            raise OmnigentError(
                "target account authority has been revoked", code=ErrorCode.CONFLICT
            )
        raise OmnigentError("account authority has been revoked", code=ErrorCode.UNAUTHORIZED)
    return current_generation
