"""Each user's saved embedded-browser tabs per session."""

from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import session_stream
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access_and_level, require_user
from omnigent.server.schemas import (
    SessionBrowserTab,
    SessionBrowserTabs,
    SessionBrowserTabsChangedEvent,
    SessionBrowserTabsUpdate,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_browser_tab_store import (
    BrowserTab,
    BrowserTabsConflictError,
    SessionBrowserTabStore,
    UnreadableBrowserTabsError,
)
from omnigent.stores.session_browser_tab_store import (
    SessionBrowserTabs as StoredBrowserTabs,
)

# Owner of the tabs when the server runs without authentication.
_SINGLE_USER_ID = "local"

# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, SessionBrowserTabStore] = {}
_stores_lock = threading.Lock()


def _storage_location(conversation_store: object) -> str | None:
    location = getattr(conversation_store, "storage_location", None)
    return location if isinstance(location, str) and location else None


def browser_tabs_supported(conversation_store: object) -> bool:
    """Whether the server can save browser tabs: sessions live in a SQL database."""
    return _storage_location(conversation_store) is not None


def _open_store(conversation_store: ConversationStore) -> SessionBrowserTabStore:
    """Return the tab store beside the session's database.

    :raises OmnigentError: The conversation store has no SQL database.
    """
    location = _storage_location(conversation_store)
    if location is None:
        raise OmnigentError(
            "Saved browser tabs need a SQL session database", code=ErrorCode.INTERNAL_ERROR
        )
    with _stores_lock:
        store = _stores.get(location)
        if store is None:
            store = SessionBrowserTabStore(location)
            _stores[location] = store
        return store


def _response(stored: StoredBrowserTabs) -> SessionBrowserTabs:
    return SessionBrowserTabs(
        revision=stored.revision,
        tabs=[SessionBrowserTab(id=tab.id, url=tab.url, title=tab.title) for tab in stored.tabs],
    )


def register_browser_tab_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the saved browser tab routes on ``router``."""

    async def authorize(request: Request, session_id: str) -> str:
        """Check read access and return the tab owner's id."""
        user_id = require_user(request, auth_provider)
        await require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        return user_id or _SINGLE_USER_ID

    @router.get("/sessions/{session_id}/browser-tabs", response_model=SessionBrowserTabs)
    async def get_browser_tabs(request: Request, session_id: str) -> SessionBrowserTabs:
        """Return the caller's saved browser tabs in the session.

        Requires session read access. A session the caller never saved tabs in
        returns no tabs at revision 0.
        """
        user_id = await authorize(request, session_id)
        store = _open_store(conversation_store)
        try:
            stored = await asyncio.to_thread(store.read, session_id, user_id)
        except UnreadableBrowserTabsError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INTERNAL_ERROR) from exc
        return _response(stored)

    @router.put("/sessions/{session_id}/browser-tabs", response_model=SessionBrowserTabs)
    async def put_browser_tabs(
        request: Request, session_id: str, body: SessionBrowserTabsUpdate
    ) -> SessionBrowserTabs:
        """Replace the caller's saved browser tabs in the session.

        Requires session read access, since the tabs belong to the caller rather
        than the session. Returns 409 when ``base_revision`` is not the stored
        revision; the client should fetch the tabs and adopt them. A save that
        changes the tabs notifies the session's viewers to refetch theirs.
        """
        user_id = await authorize(request, session_id)
        store = _open_store(conversation_store)
        tabs = tuple(BrowserTab(id=tab.id, url=tab.url, title=tab.title) for tab in body.tabs)
        try:
            stored = await asyncio.to_thread(
                store.replace, session_id, user_id, tabs, base_revision=body.base_revision
            )
        except BrowserTabsConflictError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        except UnreadableBrowserTabsError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INTERNAL_ERROR) from exc
        if stored.revision != body.base_revision:
            event = SessionBrowserTabsChangedEvent(
                type="session.browser_tabs.changed", session_id=session_id
            )
            session_stream.publish(session_id, event.model_dump())
        return _response(stored)
