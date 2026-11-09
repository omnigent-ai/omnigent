"""Owner-private sibling creation and sidebar family projections."""

from __future__ import annotations

import asyncio
import logging
from itertools import batched

from fastapi import Request

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.feature_flags import Feature
from omnigent.server.schemas import SessionFamily
from omnigent.server.session_orchestration import require_owned_session
from omnigent.server.sharing_settings import SiblingSessionDestinations
from omnigent.stores.conversation_store import (
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
    ConversationStore,
    family_pinned_label_key,
)
from omnigent.stores.permission_store import PermissionStore

_FAMILY_GRANT_BATCH_SIZE = 1000
_logger = logging.getLogger(__name__)


def authorize_sibling_source(
    request: Request,
    source_id: str,
    user_id: str | None,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    *,
    parent_id: str | None,
    host_id: str | None,
    host_type: str,
) -> Conversation:
    if not request.app.state.feature_flags.enabled(Feature.SIBLING_SESSIONS):
        raise OmnigentError("Nested agents are disabled", code=ErrorCode.FORBIDDEN)
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
    require_sibling_destination(request, source.host_id, host_id=host_id, host_type=host_type)
    if host_id is not None:
        host_store = getattr(request.app.state, "host_store", None)
        host = host_store.get_host(host_id) if host_store else None
        if host is not None and host.sandbox_provider and source.host_id != host.host_id:
            raise OmnigentError(
                "Only the source session's sandbox can be reused", code=ErrorCode.FORBIDDEN
            )
    return source


def require_sibling_destination(
    request: Request,
    source_host_id: str | None,
    *,
    host_id: str | None,
    host_type: str,
) -> None:
    """Apply the same destination policy to creation and subsequent host launches."""
    destinations = SiblingSessionDestinations.coerce(
        getattr(
            request.app.state,
            "sibling_session_destinations",
            lambda: SiblingSessionDestinations.ANY_HOST,
        )()
    )
    if host_type == "managed":
        if destinations is SiblingSessionDestinations.CURRENT_HOST:
            raise OmnigentError(
                "Add nested agent is limited to the current host by server policy",
                code=ErrorCode.FORBIDDEN,
            )
    elif destinations is not SiblingSessionDestinations.ANY_HOST and (
        host_id is None or host_id != source_host_id
    ):
        allowed = (
            "the current host"
            if destinations is SiblingSessionDestinations.CURRENT_HOST
            else "the current host or a new managed sandbox"
        )
        raise OmnigentError(
            f"Add nested agent is limited to {allowed} by server policy",
            code=ErrorCode.FORBIDDEN,
        )


async def complete_sibling_creation(
    conversations: ConversationStore, source_id: str, session_id: str, *, user_id: str | None
) -> None:
    try:
        await asyncio.to_thread(
            conversations.add_new_sibling, source_id, session_id, user_id=user_id
        )
    except Exception:
        # No runner or fresh sandbox has been launched yet.
        try:
            await conversations.delete_conversation(session_id)
        except Exception:
            _logger.exception("Failed to discard rejected sibling %s", session_id)
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
    """Annotate rows; only explicitly opted-in sidebar lists expand their families."""
    candidates = [item for item in sessions if item.family_id is not None]
    if permissions is not None and user_id is None:
        return sessions, {}

    def owned_ids(session_ids: list[str]) -> set[str]:
        if permissions is None:
            return set(session_ids)
        return {
            grant.conversation_id
            for batch in batched(session_ids, _FAMILY_GRANT_BATCH_SIZE)
            for grants in permissions.list_for_sessions(list(batch), user_id=user_id).values()
            for grant in grants
            if grant.level == LEVEL_OWNER and grant.user_id == user_id
        }

    candidate_owners = owned_ids([item.id for item in candidates])
    family_ids = list(
        dict.fromkeys(
            item.family_id
            for item in candidates
            if item.family_id is not None and item.id in candidate_owners
        )
    )
    members_by_family = conversations.get_session_families(family_ids) if family_ids else {}
    member_owners = owned_ids(
        [member.id for members in members_by_family.values() for member in members]
    )
    result = {item.id: item for item in sessions}
    families: dict[str, SessionFamily] = {}
    seen: set[str] = set()
    pin_key = family_pinned_label_key(user_id)
    for item in candidates:
        family_id = item.family_id
        assert family_id is not None
        if family_id in seen or item.id not in candidate_owners:
            continue
        seen.add(family_id)
        members = members_by_family.get(family_id, [])
        owned = [member for member in members if member.id in member_owners]
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
