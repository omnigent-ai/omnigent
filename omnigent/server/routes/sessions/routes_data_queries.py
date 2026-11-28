"""``GET /v1/sessions/{id}/data_queries``: the session's tool calls that ran data queries.

Classification is read-only display metadata over items the caller can
already read with ``GET /v1/sessions/{id}/items``; it never changes how a
call was authorized, sandboxed or run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, Query, Request

from omnigent.data_queries import classify_tool_call
from omnigent.entities import ConversationItem
from omnigent.entities.conversation import FunctionCallData, FunctionCallOutputData
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import STALE_CURSOR_RESPONSE
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.helpers import _load_agent_spec_for_session
from omnigent.server.schemas import SessionDataQuery, SessionDataQueryList
from omnigent.spec.types import DataQueriesSpec
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore

_PAGE_SIZE = 500
# Items one request classifies at most; a longer session continues from ``next_after``.
_MAX_SCANNED_ITEMS = 20_000
# Once a page stops taking calls, how many more items it reads for their outputs.
_OUTPUT_SEARCH_ITEMS = 2_000


@dataclass
class _Match:
    call: ConversationItem
    data: FunctionCallData
    # The item read just before the call, so a later page can start at the call.
    resume_after: str | None
    output: ConversationItem | None = None


def collect_data_queries(
    conversation_store: ConversationStore,
    session_id: str,
    *,
    spec: DataQueriesSpec | None,
    after: str | None,
    limit: int,
    max_scanned_items: int = _MAX_SCANNED_ITEMS,
    page_size: int = _PAGE_SIZE,
) -> SessionDataQueryList:
    """
    Classify a session's tool calls, oldest first, pairing each with its output.

    A page takes calls until it holds *limit* queries or has read
    *max_scanned_items* items, then reads on (a bounded distance) only for the
    outputs of the calls it took.

    :param conversation_store: The store holding the session's items.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param spec: The agent's ``data_queries:`` block, or ``None`` for built-ins only.
    :param after: Item ID to continue after, from a previous ``next_after``.
    :param limit: Most data queries to return.
    :param max_scanned_items: Most items to take calls from in one page.
    :param page_size: Items read from the store at a time.
    :returns: The page; ``next_after`` resumes the scan when ``has_more``.
    :raises StaleCursorError: If *after* is not an item of the session.
    """
    matches: list[_Match] = []
    awaiting_output: dict[str, _Match] = {}
    cursor = accepted_through = after
    taking_calls = True
    scanned = read_for_outputs = 0
    exhausted = skipped_match = False
    while True:
        page = conversation_store.list_items(
            session_id, limit=page_size, after=cursor, order="asc"
        )
        stop = False
        for position, item in enumerate(page.data):
            previous, cursor = cursor, item.id
            if isinstance(item.data, FunctionCallData):
                if classify_tool_call(item.data.name, item.data.arguments, spec=spec) is not None:
                    if taking_calls:
                        match = _Match(call=item, data=item.data, resume_after=previous)
                        matches.append(match)
                        awaiting_output[item.data.call_id] = match
                    else:
                        skipped_match = True
            elif isinstance(item.data, FunctionCallOutputData):
                found = awaiting_output.pop(item.data.call_id, None)
                if found is not None:
                    found.output = item
            if taking_calls:
                scanned += 1
                accepted_through = item.id
                taking_calls = len(matches) < limit and scanned < max_scanned_items
            else:
                read_for_outputs += 1
            if not taking_calls and (
                not awaiting_output or read_for_outputs >= _OUTPUT_SEARCH_ITEMS
            ):
                stop = True
                exhausted = not page.has_more and position == len(page.data) - 1
                break
        if stop:
            break
        if not page.has_more:
            exhausted = True
            break

    has_more = skipped_match or not exhausted
    next_after = None
    if has_more:
        next_after = accepted_through
        unanswered = next((i for i, m in enumerate(matches) if m.output is None), None)
        if not exhausted and unanswered:
            # Outputs past the search distance: the next page starts at that call.
            next_after = matches[unanswered].resume_after
            matches = matches[:unanswered]
    data = [entry for match in matches if (entry := _entry(match, spec)) is not None]
    return SessionDataQueryList(
        data=data,
        first_id=data[0].id if data else None,
        last_id=data[-1].id if data else None,
        has_more=has_more,
        next_after=next_after,
    )


def _entry(match: _Match, spec: DataQueriesSpec | None) -> SessionDataQuery | None:
    output = match.output
    result = (
        output.data.output if output and isinstance(output.data, FunctionCallOutputData) else None
    )
    query = classify_tool_call(match.data.name, match.data.arguments, result, spec=spec)
    if query is None:
        return None
    return SessionDataQuery(
        id=match.call.id,
        call_id=match.data.call_id,
        response_id=match.call.response_id,
        output_item_id=output.id if output is not None else None,
        tool_name=match.data.name,
        kind=query.kind,
        matcher=query.matcher,
        sql=query.sql,
        source=query.source,
        row_count=query.row_count,
        rule=query.rule,
        origin=query.origin,
    )


def register_data_queries_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
) -> None:
    """Register ``GET /sessions/{session_id}/data_queries`` on *router*."""

    @router.get(
        "/sessions/{session_id}/data_queries",
        response_model=SessionDataQueryList,
        responses={**STALE_CURSOR_RESPONSE},
    )
    async def list_session_data_queries(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        after: str | None = Query(default=None),
    ) -> SessionDataQueryList:
        """
        List the session's tool calls that ran data queries or read data sources.

        Each call is classified by the agent spec's ``data_queries:`` rules,
        then the built-in SQL CLI patterns (``psql -c``, ``bq query`` and
        others) unless the spec turns them off. A turn's Sources are the
        entries whose ``response_id`` is that turn's.

        :param session_id: Session identifier, e.g. ``"conv_abc123"``.
        :param limit: Most entries to return (1-500, default 100).
        :param after: ``next_after`` from the previous page.
        :returns: A page of :class:`SessionDataQuery`, oldest first.
        :raises OmnigentError: 403 without read access; 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        agent_spec = await asyncio.to_thread(
            _load_agent_spec_for_session, conv, agent_store, agent_cache=agent_cache
        )
        return await asyncio.to_thread(
            collect_data_queries,
            conversation_store,
            session_id,
            spec=agent_spec.data_queries if agent_spec is not None else None,
            after=after,
            limit=limit,
        )
