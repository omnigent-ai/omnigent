"""Items and child-session routes."""

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
)
from omnigent.server.schemas import (
    ChildSessionList,
    PaginatedList,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore

# ``?descendants=true`` depth: the default matches the CLI tree and the web
# Agents rail; the cap bounds the in-memory walk.
_DEFAULT_DESCENDANT_DEPTH = 3
_MAX_DESCENDANT_DEPTH = 10
# A spawn tree is read in pages of this size, up to the cap; a bigger tree
# answers ``has_more`` so the client falls back to walking one level at a time.
_SPAWN_TREE_PAGE_SIZE = 500
_SPAWN_TREE_SCAN_CAP = 5000


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
        descendants: bool = Query(default=False),
        max_depth: int = Query(default=_DEFAULT_DESCENDANT_DEPTH, ge=1, le=_MAX_DESCENDANT_DEPTH),
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
        :param descendants: Return the whole subtree down to *max_depth*
            in one response instead of one level, so a client drawing the
            sub-agent tree makes one request rather than one per node.
            Rows come breadth-first, each parent's children newest-first
            (the order a per-level walk sees), with ``parent_session_id``
            set to the row's own parent and ``depth`` set. *limit* caps
            the total rows; ``has_more`` means the subtree was cut off.
            Cursors and the title filters do not apply.
        :param max_depth: Levels to return with *descendants*, ``1`` for
            direct children only.
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
        if descendants:
            return await _list_descendant_sessions(parent, limit=limit, max_depth=max_depth)
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

    async def _list_descendant_sessions(
        parent: Conversation, *, limit: int, max_depth: int
    ) -> PaginatedList:
        """Return *parent*'s sub-agent subtree, breadth-first, from one tree scan.

        Every sub-agent records the root of its spawn tree, so one query
        reads the whole tree and the subtree is cut out in memory. READ on
        *parent* covers its descendants: a sub-agent's access is its
        parent chain's.

        :param parent: The session the caller was authorized on.
        :param limit: Maximum rows to return.
        :param max_depth: Levels below *parent* to include.
        :returns: The subtree's summaries with ``depth`` set.
        """
        rows, truncated = await asyncio.to_thread(
            _scan_spawn_tree, parent.root_conversation_id or parent.id
        )
        children_of: dict[str, list[Conversation]] = {}
        for row in rows:
            if row.parent_conversation_id:
                children_of.setdefault(row.parent_conversation_id, []).append(row)
        ordered: list[Conversation] = []
        depths: dict[str, int] = {}
        parent_ids: dict[str, str] = {}
        frontier = [parent.id]
        seen = {parent.id}
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            next_frontier: list[str] = []
            for node_id in frontier:
                for child in children_of.get(node_id, ()):
                    if child.id in seen:
                        continue
                    seen.add(child.id)
                    ordered.append(child)
                    depths[child.id] = depth
                    parent_ids[child.id] = node_id
                    next_frontier.append(child.id)
            frontier = next_frontier
        has_more = truncated or len(ordered) > limit
        ordered = ordered[:limit]
        summaries = await _child_session_summaries_from_conversations(
            ordered, parent.id, conversation_store, parent_ids=parent_ids
        )
        data = [s.model_copy(update={"depth": depths[s.id]}) for s in summaries]
        return PaginatedList(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=has_more,
        )

    def _scan_spawn_tree(root_id: str) -> tuple[list[Conversation], bool]:
        """Read every sub-agent of one spawn tree, newest first, up to a cap.

        :param root_id: Root conversation of the spawn tree.
        :returns: The rows, and whether the cap cut the scan short.
        """
        rows: list[Conversation] = []
        after: str | None = None
        while True:
            page = conversation_store.list_conversations(
                limit=_SPAWN_TREE_PAGE_SIZE,
                after=after,
                kind="sub_agent",
                root_conversation_id=root_id,
                order="desc",
                sort_by="created_at",
            )
            rows.extend(page.data)
            if not page.has_more or not page.data:
                return rows, False
            if len(rows) >= _SPAWN_TREE_SCAN_CAP:
                return rows, True
            after = page.last_id
