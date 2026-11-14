"""Context-window breakdown route: estimate what fills a session's window."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Iterator

from fastapi import APIRouter, Request

from omnigent.entities import Conversation, ConversationItem
from omnigent.entities.file import StoredFile
from omnigent.errors import StaleCursorError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.context_breakdown import MAX_SCANNED_ITEMS, build_context_breakdown
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import _logger
from omnigent.server.routes._sessions.helpers import (
    _load_agent_spec_for_session,
    _spec_harness,
)
from omnigent.server.schemas import SessionContextBreakdown
from omnigent.spec.types import AgentSpec
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.permission_store import PermissionStore

# Items read per store page while scanning the history newest first.
_SCAN_PAGE_SIZE = 500


def register_context_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    file_store: FileStore | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
) -> None:
    """Register the context-breakdown route on *router*."""

    def _load_spec(conv: Conversation) -> AgentSpec | None:
        """Resolve the session's spec, or ``None`` when it cannot load."""
        try:
            spec = _load_agent_spec_for_session(conv, agent_store, agent_cache=agent_cache)
        except Exception:
            # Same tolerance as the session snapshot: report history without a spec.
            _logger.debug("Context breakdown could not load spec for %s", conv.id, exc_info=True)
            return None
        if spec is not None and conv.sub_agent_name:
            from omnigent.runtime.workflow import _find_spec_by_name

            # Same fallback as the session snapshot: an unresolved name keeps the parent.
            return _find_spec_by_name(spec, conv.sub_agent_name) or spec
        return spec

    def _items_newest_first(session_id: str) -> Iterator[ConversationItem]:
        """Yield the session's items newest first, a page at a time."""
        cursor: str | None = None
        scanned = 0
        while scanned <= MAX_SCANNED_ITEMS:
            try:
                page = conversation_store.list_items(
                    session_id, limit=_SCAN_PAGE_SIZE, after=cursor, order="desc"
                )
            except StaleCursorError:
                # The cursor item was deleted mid-scan; report what was read.
                return
            yield from page.data
            scanned += len(page.data)
            if not page.has_more or page.last_id is None:
                return
            cursor = page.last_id

    def _file_lookup(session_id: str) -> Callable[[str], StoredFile | None]:
        """Look up each file once, accepting legacy unscoped rows as the resolver does."""

        @functools.cache
        def lookup(file_id: str) -> StoredFile | None:
            if file_store is None:
                return None
            stored = file_store.get(file_id)
            if stored is None or stored.session_id not in (None, session_id):
                return None
            return stored

        return lookup

    def _build(conv: Conversation) -> SessionContextBreakdown:
        spec = _load_spec(conv)
        harness = conv.harness_override or (_spec_harness(spec) if spec is not None else None)
        return build_context_breakdown(
            conv.id,
            spec=spec,
            harness=harness,
            labels=conv.labels,
            items_newest_first=_items_newest_first(conv.id),
            lookup_file=_file_lookup(conv.id),
        )

    @router.get(
        "/sessions/{session_id}/context-breakdown",
        response_model=SessionContextBreakdown,
    )
    async def get_session_context_breakdown(
        request: Request,
        session_id: str,
    ) -> SessionContextBreakdown:
        """
        Estimate what fills the session's context window.

        Splits the window into the agent's system prompt, skill listing,
        declared tool sources, attachments and conversation history. Every
        token figure is a size-based estimate; the measured total stays on
        the session snapshot.

        :param request: The incoming FastAPI request.
        :param session_id: Session identifier, e.g. ``"conv_abc123"``.
        :returns: The estimated :class:`SessionContextBreakdown`.
        :raises OmnigentError: 404 if the session does not exist or the
            caller cannot read it.
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
        return await asyncio.to_thread(_build, conv)
