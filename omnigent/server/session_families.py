"""Owner-private sibling creation and sidebar family projections."""

from __future__ import annotations

import asyncio

from fastapi import Request

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.schemas import SessionFamily
from omnigent.server.session_orchestration import require_owned_session
from omnigent.stores.conversation_store import (
    FAMILY_PIN_LABEL_PREFIX,
    PINNED_LABEL_KEY,
    SESSION_FAMILY_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
    ConversationStore,
    pinned_label_key,
)
from omnigent.stores.permission_store import PermissionStore


def authorize_sibling_source(
    request: Request,
    source_id: str,
    user_id: str | None,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    *,
    parent_id: str | None,
    host_id: str | None,
) -> Conversation:
    if parent_id is not None or permissions is None or user_id is None:
        raise OmnigentError(
            "Siblings require an owned source and no execution parent",
            code=ErrorCode.INVALID_INPUT,
        )
    source = require_owned_session(source_id, user_id, conversations, permissions, private=True)
    while (
        source.parent_conversation_id is not None or source.labels.get(SIDE_CHAT_LABEL_KEY) == "1"
    ):
        source = require_owned_session(
            source.parent_conversation_id or source.labels[SIDE_CHAT_SOURCE_LABEL_KEY],
            user_id,
            conversations,
            permissions,
            private=True,
        )
    if host_id is not None:
        host_store = getattr(request.app.state, "host_store", None)
        host = host_store.get_host(host_id) if host_store else None
        if host is not None and host.sandbox_provider and source.host_id != host.host_id:
            raise OmnigentError(
                "Only the source session's sandbox can be reused", code=ErrorCode.FORBIDDEN
            )
    return source


async def complete_sibling_creation(
    conversations: ConversationStore, source_id: str, session_id: str, *, user_id: str | None
) -> None:
    try:
        await asyncio.to_thread(
            conversations.add_new_sibling, source_id, session_id, user_id=user_id
        )
    except Exception:
        # No runner or fresh sandbox has been launched yet.
        await conversations.delete_conversation(session_id)
        raise


def project_families(
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    sessions: list[Conversation],
    user_id: str | None,
    *,
    expand: bool = False,
    include_archived: bool = False,
    archived_only: bool = False,
) -> tuple[list[Conversation], dict[str, SessionFamily]]:
    """Hydrate complete owner-visible families without changing pagination cursors."""
    candidates = [item for item in sessions if SESSION_FAMILY_LABEL_KEY in item.labels]
    grants = permissions.list_for_sessions([item.id for item in candidates]) if permissions else {}
    result = {item.id: item for item in sessions}
    families: dict[str, SessionFamily] = {}
    seen: set[str] = set()
    pin_key = FAMILY_PIN_LABEL_PREFIX + pinned_label_key(user_id).removeprefix(
        PINNED_LABEL_KEY + "."
    )
    for item in candidates:
        family_id = item.labels[SESSION_FAMILY_LABEL_KEY]
        if family_id in seen or (
            permissions is not None
            and not any(
                grant.user_id == user_id and grant.level == LEVEL_OWNER
                for grant in grants.get(item.id, [])
            )
        ):
            continue
        seen.add(family_id)
        members = conversations.get_session_family(item.id)
        member_grants = (
            permissions.list_for_sessions([member.id for member in members]) if permissions else {}
        )
        owned = [
            member
            for member in members
            if permissions is None
            or any(
                grant.user_id == user_id and grant.level == LEVEL_OWNER
                for grant in member_grants.get(member.id, [])
            )
        ]
        active = [member for member in owned if not member.archived]
        info = SessionFamily(
            id=family_id,
            representative_id=(active or owned)[0].id if owned else None,
            pinned=pin_key in item.labels,
            pinned_at=item.labels.get(pin_key),
        )
        for member in owned:
            families[member.id] = info
            if expand and (
                member.archived if archived_only else (include_archived or not member.archived)
            ):
                result[member.id] = member
    return list(result.values()), families
