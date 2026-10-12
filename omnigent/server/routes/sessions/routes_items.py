"""Items, child-session and sub-agent subtree routes."""

from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter,
    Query,
    Request,
)

from omnigent.entities import Conversation
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
from omnigent.server.routes._sessions.orchestration import (
    _child_session_summaries_from_conversations,
    _descendant_session_summaries_from_conversations,
)
from omnigent.server.schemas import (
    ChildSessionList,
    DescendantSessionList,
    PaginatedList,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore


def _descendant_rows(
    page: list[Conversation],
    session_id: str,
    conversation_store: ConversationStore,
    *,
    max_depth: int | None,
) -> list[tuple[Conversation, str, int]]:
    """
    Keep the rows of one spawn-tree page that sit under *session_id*.

    The store lists a tree newest-first, so a page can hold rows from sibling
    branches (when *session_id* is not the tree root) or below the depth cap.
    A row's depth is the length of its parent chain up to *session_id*;
    chains resolve against the page first and then against the store, one
    batched read per tree level, so the work is bounded by the page size
    times the tree height rather than by the tree size.

    :param page: One store page of the tree's ``sub_agent`` conversations.
    :param session_id: Subtree root; never part of the result.
    :param conversation_store: Store for ancestors outside the page.
    :param max_depth: Deepest level to keep, or ``None`` for all.
    :returns: ``(conversation, parent_id, depth)`` triples in page order.
    """
    known: dict[str, Conversation] = {conv.id: conv for conv in page}
    # Depth below session_id; None marks a row outside its subtree.
    depth_of: dict[str, int | None] = {session_id: 0}
    unresolved = list(page)
    while unresolved:
        missing: set[str] = set()
        still_unresolved: list[Conversation] = []
        for conv in unresolved:
            chain: list[str] = []
            cursor: str | None = conv.id
            while cursor is not None and cursor not in depth_of:
                # A corrupt parent cycle, or a chain already longer than the
                # requested depth, puts the row outside the listed tree.
                if cursor in chain or (max_depth is not None and len(chain) > max_depth):
                    depth_of[conv.id] = None
                    break
                chain.append(cursor)
                ancestor = known.get(cursor)
                if ancestor is None:
                    missing.add(cursor)
                    break
                cursor = ancestor.parent_conversation_id
            else:
                # A chain ending at the tree's top never met session_id.
                base = None if cursor is None else depth_of[cursor]
                for steps, conv_id in enumerate(reversed(chain), start=1):
                    depth_of[conv_id] = None if base is None else base + steps
                continue
            if conv.id not in depth_of:
                still_unresolved.append(conv)
        if not missing:
            break
        fetched = conversation_store.get_conversations(sorted(missing))
        known.update(fetched)
        for conv_id in missing - fetched.keys():
            # Deleted mid-walk: nothing beneath it can be placed in the tree.
            depth_of[conv_id] = None
        unresolved = still_unresolved

    rows: list[tuple[Conversation, str, int]] = []
    for conv in page:
        depth = depth_of.get(conv.id)
        if depth is None or depth < 1 or (max_depth is not None and depth > max_depth):
            continue
        if conv.parent_conversation_id is not None:
            rows.append((conv, conv.parent_conversation_id, depth))
    return rows


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

    # ── GET /sessions/{session_id}/descendants ───────────────────

    @router.get(
        "/sessions/{session_id}/descendants",
        response_model=None,
        responses={200: {"model": DescendantSessionList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_descendant_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        max_depth: int | None = Query(default=None, ge=1),
    ) -> PaginatedList:
        """
        List every sub-agent session under a parent, at any depth.

        The subtree comes back as a flat, cursor-paginated listing of
        :class:`DescendantSessionSummary` rows, each naming its own parent
        and depth, so a client rebuilds the tree with one request per page
        instead of one ``GET …/child_sessions`` per node (the REPL
        navigator, the SDK ``child_sessions_tree`` / ``subtree_busy``
        helpers and the web Agents panel read this).

        Each page is one store page of the spawn tree in primary-key order,
        the order the ``root_conversation_id`` index serves as a single
        range scan, so the rows read per request stay bounded by ``limit``
        rather than by the tree size; clients order siblings newest-first
        from ``created_at``. Rows from sibling branches or below
        ``max_depth`` are dropped from the page but still advance the
        cursor: a page can be shorter than ``limit`` — even empty — while
        ``has_more`` is true, and ``last_id`` is always the cursor for the
        next page. Placing the page's rows costs one batched primary-key
        read per tree level above them that the page itself does not hold,
        so a request's total work is bounded by ``limit`` times the tree
        height, or ``max_depth`` when given.

        Archived descendants are listed too, flagged ``archived``, so live
        work under an archived session stays reachable; clients hide the
        settled archived rows they do not want to show.

        :param request: Inbound HTTP request; carries the caller identity
            used to authorize READ on ``session_id``.
        :param session_id: Session whose subtree to list, e.g.
            ``"conv_abc123"``. Not itself included in the result.
        :param limit: Maximum rows read per page (1-1000, default 100).
        :param after: Cursor — the ``last_id`` of the previous page, e.g.
            ``"conv_child123"``.
        :param max_depth: Deepest level to return (1 = direct children
            only). ``None`` returns the whole subtree.
        :returns: A :class:`PaginatedList` of
            :class:`DescendantSessionSummary` objects.
        :raises OmnigentError: 404 if no readable session exists at
            ``session_id``; 400 ``stale_cursor`` if the row ``after`` names
            was deleted.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation
        if parent is None:
            parent = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if parent is None:
            raise _session_not_found()
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            kind="sub_agent",
            root_conversation_id=parent.root_conversation_id,
            sort_by="id",
            include_archived=True,
        )
        rows = await asyncio.to_thread(
            _descendant_rows, page.data, session_id, conversation_store, max_depth=max_depth
        )
        data = await _descendant_session_summaries_from_conversations(rows, conversation_store)
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )
