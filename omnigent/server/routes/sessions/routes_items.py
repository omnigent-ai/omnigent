"""Items and child-session routes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import (
    APIRouter,
    Query,
    Request,
)

from omnigent.entities import Conversation
from omnigent.entities.conversation import ITEM_TYPE_TO_DATA_CLS
from omnigent.errors import StaleCursorError
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
    LEVEL_OWNER,
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    is_session_owner as _is_session_owner,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import (
    STALE_CURSOR_RESPONSE,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    _session_status_cache,
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import SessionLiveness
from omnigent.server.routes._sessions.orchestration import (
    _child_session_summaries_from_conversations,
    _settle_orphaned_running_rows,
)
from omnigent.server.schemas import (
    ChildSessionList,
    PaginatedList,
    SessionItemList,
    SideChatList,
    SideChatSummary,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import runner_seen_is_fresh
from omnigent.stores.permission_store import PermissionStore

# ``?descendants=true`` depth: the default matches the CLI tree and the web
# Agents rail; the cap bounds the in-memory walk.
_DEFAULT_DESCENDANT_DEPTH = 3
_MAX_DESCENDANT_DEPTH = 10
# ``?descendants=true`` reads at most this many sub-agents of one spawn tree;
# a bigger tree gets the one-level listing instead, which clients walk.
_SPAWN_TREE_READ_CAP = 2000
# The item types ``GET /sessions/{id}/items`` can filter by.
_ITEM_TYPE_PATTERN = f"^({'|'.join(ITEM_TYPE_TO_DATA_CLS)})$"


def _is_settle_candidate(conv: Conversation) -> bool:
    """Whether a read would consider *conv* for settling, before any owner check.

    Mirrors the cheap filters in ``_settle_orphaned_running_rows``, so a read
    with no candidate never needs the parent-chain OWNER check.
    """
    return (
        conv.agent_id is not None
        and conv.runner_id is not None
        and conv.live_status in ("running", "waiting")
        and _session_status_cache.get(conv.id) is None
        and not runner_seen_is_fresh(conv.runner_last_seen)
    )


def register_items_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
) -> None:
    """Register the items routes on router."""

    async def _may_settle_children(
        user_id: str | None, parent: Conversation, displayed_level: int | None
    ) -> bool:
        """Whether the caller may settle the orphaned children of *parent*.

        Only a top-level session's displayed level is authoritative. A
        sub-agent's direct grant can be narrower than what its parent chain
        gives, so it always resolves OWNER through the chain. Permissions off
        settles for everyone.

        :param user_id: The authenticated caller.
        :param parent: The session whose children are listed.
        :param displayed_level: The caller's level from the access check.
        :returns: ``True`` when the caller owns the parent (or is admin).
        """
        if permission_store is None:
            return True
        if (
            parent.parent_conversation_id is None
            and displayed_level is not None
            and displayed_level >= LEVEL_OWNER
        ):
            return True
        return await asyncio.to_thread(
            check_session_access,
            user_id,
            parent.id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )

    def _owner_check(
        user_id: str | None, parent: Conversation, displayed_level: int | None
    ) -> Callable[[], Awaitable[bool]]:
        """Return a memoised, lazy OWNER check for *parent*'s children.

        The parent-chain query runs at most once, and only when a read finds a
        settle candidate.
        """
        resolved: list[bool] = []

        async def may_settle() -> bool:
            if not resolved:
                resolved.append(await _may_settle_children(user_id, parent, displayed_level))
            return resolved[0]

        return may_settle

    async def _settle_children(
        request: Request, rows: list[Conversation], may_settle: Callable[[], Awaitable[bool]]
    ) -> None:
        """Settle orphaned ``running`` children as the session list does, so
        the tree and runners polling this list never see a dead turn as live.
        Only the parent's owner may settle; the owner check runs only when a
        row is a candidate. See :func:`_may_settle_children`."""
        if not any(_is_settle_candidate(conv) for conv in rows):
            return
        if not await may_settle():
            return
        await _settle_orphaned_running_rows(
            rows,
            conversation_store=conversation_store,
            liveness_lookup=liveness_lookup,
            host_store=getattr(request.app.state, "host_store", None),
            may_settle=lambda _conv: True,
        )

    @router.get(
        "/sessions/{session_id}/items",
        response_model=None,
        responses={200: {"model": SessionItemList}, **STALE_CURSOR_RESPONSE},
    )
    async def list_session_items(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
        type: str | None = Query(default=None, pattern=_ITEM_TYPE_PATTERN),
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
        :param type: Only items of this type, e.g. ``"message"``; ``None``
            lists every type. An unknown type is a 422.
        :returns: A page of flattened conversation items, documented as
            :class:`SessionItemList`.
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
            type=type,
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
            set to the row's own parent and ``depth`` set. *limit* and
            *after* page through that order. A spawn tree of more than
            2000 sub-agents gets the one-level listing instead (rows without
            ``depth``), as from a server without this flag. *before*,
            *order* and the title filters do not apply.
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
        may_settle = _owner_check(user_id, parent, access.level)
        if descendants:
            subtree = await _list_descendant_sessions(
                request, parent, may_settle, limit=limit, after=after, max_depth=max_depth
            )
            if subtree is not None:
                return subtree
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
        await _settle_children(request, page.data, may_settle)
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
        request: Request,
        parent: Conversation,
        may_settle: Callable[[], Awaitable[bool]],
        *,
        limit: int,
        after: str | None,
        max_depth: int,
    ) -> PaginatedList | None:
        """Return a page of *parent*'s sub-agent subtree, breadth-first.

        Every sub-agent records the root of its spawn tree, so one bounded
        read (:meth:`ConversationStore.list_spawn_tree`) returns the whole
        tree and the subtree is cut out in memory. READ on *parent* covers
        its descendants: a sub-agent's access is its parent chain's.

        :param parent: The session the caller was authorized on.
        :param limit: Maximum rows in the page.
        :param after: Id of the last row of the previous page, or ``None``.
        :param max_depth: Levels below *parent* to include.
        :returns: The page with ``depth`` set, or ``None`` when the spawn tree
            is too big for one read, in which case the caller answers with
            the one-level listing a server without ``descendants`` gives.
        :raises StaleCursorError: If *after* is not a row of the subtree, or
            the tree outgrew one read since the page it came from.
        """
        page = await asyncio.to_thread(
            conversation_store.list_spawn_tree,
            parent.root_conversation_id or parent.id,
            # The tree's root row plus up to the cap of sub-agents.
            limit=_SPAWN_TREE_READ_CAP + 1,
        )
        if page.has_more:
            if after is not None:
                # The tree outgrew one read since the first page: this cursor
                # cannot continue, so the client restarts.
                raise StaleCursorError(after)
            return None
        # Rows come in the store's one-level listing order, so grouping keeps
        # each parent's children in the order a per-level walk reads them.
        children_of: dict[str, list[Conversation]] = {}
        for row in page.data:
            # An archived sub-agent hides its subtree, as in a per-level walk.
            if row.parent_conversation_id and not row.archived:
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
        start = 0
        if after is not None:
            position = next((i for i, c in enumerate(ordered) if c.id == after), None)
            if position is None:
                raise StaleCursorError(after)
            start = position + 1
        window = ordered[start : start + limit]
        await _settle_children(request, window, may_settle)
        summaries = await _child_session_summaries_from_conversations(
            window, parent.id, conversation_store, parent_ids=parent_ids
        )
        data = [s.model_copy(update={"depth": depths[s.id]}) for s in summaries]
        return PaginatedList(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=start + limit < len(ordered),
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
        limit: int = Query(default=20, ge=1, le=20),
        after: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List the side chats available in a session's Workspace rail.

        Only the owner of the session's root starts side chats there, so only
        that owner gets any, in id order; anyone else, admins included, gets an
        empty page. Opening and closing tabs is local to each device.

        :param request: Inbound HTTP request; carries the caller identity.
        :param session_id: Parent session id, e.g. ``"conv_abc123"``.
        :param limit: Maximum side chats to return (1-20, default 20).
        :param after: Cursor: the previous page's ``last_id``.
        :returns: A :class:`PaginatedList` of :class:`SideChatSummary`.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation or await asyncio.to_thread(
            conversation_store.get_conversation, session_id
        )
        if parent is None:
            raise _session_not_found()
        # Only the owner of the parent's root starts its side chats, so they are all theirs.
        if not await asyncio.to_thread(
            _is_session_owner,
            user_id,
            parent,
            auth_provider=auth_provider,
            permission_store=permission_store,
        ):
            return PaginatedList(data=[], has_more=False)
        ids = await asyncio.to_thread(
            conversation_store.list_side_chat_ids, parent.id, after=after, limit=limit + 1
        )
        has_more = len(ids) > limit
        ids = ids[:limit]
        rows = await asyncio.to_thread(conversation_store.get_conversations, ids)
        return PaginatedList(
            data=[
                SideChatSummary(
                    id=side_id,
                    created_at=rows[side_id].created_at,
                    title=rows[side_id].title,
                )
                for side_id in ids
                if side_id in rows
            ],
            first_id=ids[0] if ids else None,
            last_id=ids[-1] if ids else None,
            has_more=has_more,
        )
