"""Items and child-session routes."""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import (
    APIRouter,
    Query,
    Request,
)
from fastapi.responses import StreamingResponse

from omnigent.entities import Conversation
from omnigent.export import EntryNumbering, TranscriptHeader, header_from_conversation
from omnigent.models.model_metadata import concrete_reported_model
from omnigent.runtime.agent_cache import AgentCache
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
from omnigent.server.routes._sessions.helpers import (
    _attachment_disposition,
    _resolve_harness,
    _resolve_llm_model,
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

#: Items per page when streaming an export; one store round trip per page.
_EXPORT_PAGE_SIZE = 500
#: Sub-agent sessions read per parent when bundling a spawn tree.
_EXPORT_TREE_PAGE_SIZE = 200
#: ``zlib`` with a gzip wrapper, so the bytes are a ``.gz`` file rather than raw deflate.
_GZIP_WBITS = 16 + zlib.MAX_WBITS


async def _gzipped(lines: AsyncIterator[str]) -> AsyncIterator[bytes]:
    """Compress a line stream as it is produced, so a long session never buffers."""
    compressor = zlib.compressobj(wbits=_GZIP_WBITS)
    async for line in lines:
        chunk = compressor.compress(line.encode("utf-8"))
        if chunk:
            yield chunk
    yield compressor.flush()


class _NdjsonResponse(StreamingResponse):
    """A streamed NDJSON body; declares the media type for the OpenAPI schema too."""

    media_type = "application/x-ndjson"


def register_items_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
) -> None:
    """Register the items routes on router."""

    @router.get(
        "/sessions/{session_id}/export",
        # response_model=None: returns a streamed body, not a model.
        response_model=None,
        response_class=_NdjsonResponse,
        responses={
            200: {
                "description": (
                    "The session as an ``omnigent.transcript/1`` file: one JSON "
                    "object per line, header first, then one entry per item."
                ),
                "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
            },
        },
    )
    async def export_session_transcript(
        request: Request,
        session_id: str,
        content: Literal["full", "none"] = Query(default="full"),
        compress: Literal["none", "gzip"] = Query(default="none"),
        include: Literal["session", "tree"] = Query(default="session"),
    ) -> StreamingResponse:
        """
        Stream the session as a portable, versioned transcript.

        Builds the same ``omnigent.transcript/1`` file as ``omnigent session
        export`` (see ``docs/session-export.md``) server-side, paging the
        items so a long session never has to fit in memory at once. The
        header's ``title`` and ``model`` are resolved the way the session
        snapshot (``GET /v1/sessions/{id}``) resolves them.

        :param request: Inbound HTTP request; carries the caller identity
            used to authorize READ on the session.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param content: ``"full"`` (default) writes the session as it is stored.
            ``"none"`` writes its shape without content: turns, tool names,
            call ids, timing and the ``sealed`` markers, with every
            content field dropped and ``redacted`` set in the header. Such a
            file is not importable.
        :param include: ``"tree"`` appends this session's sub-agent sessions,
            and theirs, each introduced by its own header line. The caller's
            READ on this session covers them, as it does for
            ``GET /v1/sessions/{id}/child_sessions``.
        :param compress: ``"gzip"`` returns the same transcript gzipped, as
            ``<session_id>.jsonl.gz``. Sent as ``application/gzip`` rather than
            a content encoding, so the download is the compressed file itself
            rather than something the browser silently expands.
        :returns: An ``application/x-ndjson`` download named
            ``<session_id>.jsonl``.
        :raises OmnigentError: 404 when no such session exists or the caller
            has no READ access to it (the two are not distinguished).
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

        def _header(conversation: Conversation) -> TranscriptHeader:
            """Resolve the agent-derived header fields the way the snapshot does."""
            agent = agent_store.get(conversation.agent_id) if conversation.agent_id else None
            return header_from_conversation(
                conversation,
                agent_name=agent.name if agent is not None else None,
                harness=_resolve_harness(
                    conversation, agent_store=agent_store, agent_cache=agent_cache
                ),
                # The harness's own report supersedes the spec-derived model.
                llm_model=concrete_reported_model(conversation.reported_model)
                or _resolve_llm_model(
                    conversation, agent_store=agent_store, agent_cache=agent_cache
                ),
            )

        def _descendants(root_id: str) -> list[Conversation]:
            """The sub-agent sessions under *root_id*, breadth first.

            A sub-agent can spawn its own, so this walks rather than lists one
            level. Ids already seen are skipped, so a cycle in stored data
            cannot make the download run forever.
            """
            out: list[Conversation] = []
            seen = {root_id}
            queue = [root_id]
            while queue:
                parent_id = queue.pop(0)
                children = conversation_store.list_conversations(
                    limit=_EXPORT_TREE_PAGE_SIZE,
                    kind="sub_agent",
                    parent_conversation_id=parent_id,
                    order="asc",
                    sort_by="created_at",
                ).data
                for child in children:
                    if child.id in seen:
                        continue
                    seen.add(child.id)
                    out.append(child)
                    queue.append(child.id)
            return out

        redact = content == "none"

        async def _session_lines(conversation: Conversation) -> AsyncIterator[str]:
            """Yield one session: its header, then its items a page at a time."""
            header = await asyncio.to_thread(_header, conversation)
            if redact:
                header = header.model_copy(update={"redacted": True})
            yield header.to_line() + "\n"
            numbering = EntryNumbering()
            after: str | None = None
            while True:
                page = await asyncio.to_thread(
                    conversation_store.list_items,
                    conversation.id,
                    limit=_EXPORT_PAGE_SIZE,
                    after=after,
                    order="asc",
                )
                for item in page.data:
                    entry = numbering.entry(item.to_api_dict())
                    yield (entry.redacted() if redact else entry).to_line() + "\n"
                if not page.has_more:
                    break
                if page.last_id is None:
                    # Cannot be followed; aborting the stream beats a file that
                    # silently ends early and reads as complete.
                    raise RuntimeError(
                        f"session {conversation.id} item page reported more results "
                        "without a cursor"
                    )
                after = page.last_id

        async def _lines() -> AsyncIterator[str]:
            """Yield the session, and its spawn tree when asked for."""
            async for line in _session_lines(conv):
                yield line
            if include != "tree":
                return
            for descendant in await asyncio.to_thread(_descendants, conv.id):
                async for line in _session_lines(descendant):
                    yield line

        if compress == "gzip":
            return StreamingResponse(
                _gzipped(_lines()),
                media_type="application/gzip",
                headers={"Content-Disposition": _attachment_disposition(f"{conv.id}.jsonl.gz")},
            )
        return _NdjsonResponse(
            _lines(),
            headers={"Content-Disposition": _attachment_disposition(f"{conv.id}.jsonl")},
        )

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
