"""Project membership: the owner plus users the project is shared with.

A share is a ``member`` or a ``manager``; a Manager may also invite and remove
ordinary members. Everything else owner-only stays with the owner.

Shared by the project, ticket, conversation and permission stores so every
gate agrees on who may open a project and the sessions filed in it.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import Select, or_, select, union
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlProject,
    SqlProjectShare,
    SqlSessionPermission,
    current_workspace_id,
)

# Session owner level; mirrors ``omnigent.server.auth.LEVEL_OWNER`` without importing the server.
LEVEL_OWNER = 4

ProjectRole = Literal["owner", "manager", "member"]
ShareRole = Literal["manager", "member"]
SHARE_ROLES: tuple[ShareRole, ...] = ("member", "manager")


def project_role(
    session: Session, row: SqlProject | None, user_id: str | None
) -> ProjectRole | None:
    """Return the caller's role on a loaded project row, or ``None`` without access."""
    if row is None:
        return None
    if row.user_id == user_id:
        return "owner"
    if user_id is None:
        return None
    share = session.get(SqlProjectShare, (row.workspace_id, row.id, user_id))
    if share is None:
        return None
    return "manager" if share.role == "manager" else "member"


def accessible_project_ids(user_id: str | None) -> Select[tuple[str]]:
    """SELECT the ids of projects *user_id* owns or was shared."""
    workspace = current_workspace_id()
    owned = select(SqlProject.id).where(
        SqlProject.workspace_id == workspace,
        SqlProject.user_id.is_(None) if user_id is None else SqlProject.user_id == user_id,
    )
    if user_id is None:
        return owned
    shared = select(SqlProjectShare.project_id).where(
        SqlProjectShare.workspace_id == workspace,
        SqlProjectShare.user_id == user_id,
    )
    return select(union(owned, shared).subquery().c[0])


def project_session_owners(user_id: str) -> Select[tuple[str, str | None]]:
    """SELECT ``(session id, project owner)`` for sessions *user_id* reaches via a project.

    Only sessions owned by the project's owner or a current member count, so a
    session filed in the project by anyone else grants nothing through it.
    """
    participant_owned = (
        select(SqlSessionPermission.conversation_id)
        .where(
            SqlSessionPermission.workspace_id == SqlConversationMetadata.workspace_id,
            SqlSessionPermission.conversation_id == SqlConversationMetadata.id,
            SqlSessionPermission.level >= LEVEL_OWNER,
            or_(
                SqlSessionPermission.user_id == SqlProject.user_id,
                SqlSessionPermission.user_id.in_(
                    select(SqlProjectShare.user_id).where(
                        SqlProjectShare.workspace_id == SqlProject.workspace_id,
                        SqlProjectShare.project_id == SqlProject.id,
                    )
                ),
            ),
        )
        .exists()
    )
    return (
        select(SqlConversationMetadata.id, SqlProject.user_id)
        .join(
            SqlProject,
            (SqlProject.workspace_id == SqlConversationMetadata.workspace_id)
            & (SqlProject.id == SqlConversationMetadata.project_id),
        )
        .where(
            SqlConversationMetadata.workspace_id == current_workspace_id(),
            SqlProject.id.in_(accessible_project_ids(user_id)),
            participant_owned,
        )
    )


def project_conversation_ids(user_id: str) -> Select[tuple[str]]:
    """SELECT ids of sessions *user_id* reaches through project membership."""
    return select(project_session_owners(user_id).subquery().c[0])
