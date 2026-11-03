"""Items and child-session routes."""

from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter,
    Query,
    Request,
)

from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import (
    STALE_CURSOR_RESPONSE,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import _permission_level_from_grants
from omnigent.server.routes._sessions.orchestration import (
    _child_session_summaries_from_conversations,
)
from omnigent.server.schemas import (
    ChildSessionList,
    PaginatedList,
    SideChatList,
    SideChatSummary,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import SIDE_CHAT_PARENT_LABEL_KEY
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.session_lifecycle import is_session_closed


def register_items_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the items routes on router."""

    @router.get(
        "/sessions/{session_id}/items",
        response_model=None,
        responses={200: {"model": PaginatedList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_session_items(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> PaginatedList:
        """
        List items in a session with cursor-based pagination.

        Delegates to the conversation items store — session_id is
        the conversation_id. Same pagination contract as
        ``GET /v1/conversations/{id}/items``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return
            (1-1000, default 100).
        :param after: Cursor — return items after this item ID,
            e.g. ``"msg_abc123"``.
        :param before: Cursor — return items before this item ID.
        :param order: Sort order, ``"asc"`` (chronological,
            default) or ``"desc"``.
        :returns: A :class:`PaginatedList` of conversation items.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [m.to_api_dict() for m in page.data]
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/child_sessions ────────────────

    @router.get(
        "/sessions/{session_id}/child_sessions",
        response_model=None,
        responses={200: {"model": ChildSessionList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_child_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        tool: str | None = Query(default=None),
        session_name: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List sub-agent (child) sessions under a parent session.

        Returns a page of :class:`ChildSessionSummary` objects
        derived from child conversations (``kind="sub_agent"``,
        ``parent_conversation_id=session_id``) plus each child's
        latest task. Powers the web / REPL debug surfaces' "child
        sessions" panel without parsing parent
        ``function_call_output`` JSON handles. Pagination contract
        matches :func:`list_session_items` so existing client code
        can reuse the same cursor logic.

        :param request: Inbound HTTP request; carries the caller
            identity used to authorize READ on the parent session.
        :param session_id: Parent session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of children to return
            (1-1000, default 20 — sub-agent fan-out is typically
            sparse compared to conversation items).
        :param after: Cursor — return children whose id appears
            after this one in sort order,
            e.g. ``"conv_child123"``.
        :param before: Cursor — return children before this one.
        :param order: Sort direction, ``"desc"`` (newest-first,
            default) or ``"asc"``. Sort column is ``created_at``.
        :param tool: When set, only return children whose title
            starts with this agent type (the segment before the
            ``":"``). Combined with ``session_name`` to form the
            exact title ``"{tool}:{session_name}"`` for server-side
            filtering.
        :param session_name: When set alongside ``tool``, only
            return children whose title matches
            ``"{tool}:{session_name}"`` exactly.
        :returns: A :class:`PaginatedList` of
            :class:`ChildSessionSummary` objects.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        # Require READ on the parent before listing its children (no cross-user enumeration).
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation
        if parent is None:
            parent = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if parent is None:
            raise _session_not_found()
        title_filter: str | None = None
        if tool and session_name:
            title_filter = f"{tool}:{session_name}"
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            kind="sub_agent",
            parent_conversation_id=session_id,
            order=order,
            sort_by="created_at",
            title=title_filter,
        )
        data = await _child_session_summaries_from_conversations(
            page.data,
            session_id,
            conversation_store,
        )
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/side_chats ────────────────────

    @router.get(
        "/sessions/{session_id}/side_chats",
        response_model=None,
        responses={200: {"model": SideChatList}},
    )
    async def list_side_chats(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List the side chats shown as tabs in a session's Workspace rail.

        Pages in side-chat id order over the labels lookup index, so a page
        reads at most ``limit`` side chats. Only side chats the caller can read
        are returned, so a page may be short while ``has_more`` is true;
        continue from ``last_id``. Closed side chats are included, so other
        devices can close their tabs.

        :param request: Inbound HTTP request; carries the caller identity.
        :param session_id: Parent session id, e.g. ``"conv_abc123"``.
        :param limit: Maximum side chats to examine (1-1000, default 100).
        :param after: Cursor: the previous page's ``last_id``.
        :returns: A :class:`PaginatedList` of :class:`SideChatSummary`.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None and (
            await asyncio.to_thread(conversation_store.get_conversation, session_id) is None
        ):
            raise _session_not_found()
        ids = await asyncio.to_thread(
            conversation_store.list_conversation_ids_with_label,
            SIDE_CHAT_PARENT_LABEL_KEY,
            session_id,
            after=after,
            limit=limit + 1,
        )
        has_more = len(ids) > limit
        ids = ids[:limit]
        by_id = await asyncio.to_thread(conversation_store.get_conversations, ids)
        rows = [by_id[side_id] for side_id in ids if side_id in by_id]
        # A generic side chat is a private top-level fork with its own grants; a
        # Codex side chat is a child whose access follows this session's.
        private = [row.id for row in rows if row.parent_conversation_id != session_id]
        if permission_store is not None and auth_provider is not None and private:
            grants = await asyncio.to_thread(permission_store.list_for_sessions, private)
            is_admin = user_id is not None and await asyncio.to_thread(
                permission_store.is_admin, user_id
            )
            hidden = {
                side_id
                for side_id in private
                if (_permission_level_from_grants(user_id, grants.get(side_id, []), is_admin) or 0)
                < LEVEL_READ
            }
            rows = [row for row in rows if row.id not in hidden]
        return PaginatedList(
            data=[
                SideChatSummary(
                    id=row.id,
                    created_at=row.created_at,
                    closed=is_session_closed(row.labels, row.title),
                )
                for row in rows
            ],
            first_id=ids[0] if ids else None,
            last_id=ids[-1] if ids else None,
            has_more=has_more,
        )
