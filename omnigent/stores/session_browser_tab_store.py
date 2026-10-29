"""Persistent store of each user's open embedded-browser tabs per session.

The desktop app keeps a session's tabs locally and mirrors them here, so the
same tabs reopen on the user's other devices and logins. Each user in a shared
session has their own row. Every write bumps a revision, and a write must name
the revision it was based on, so a client holding stale tabs cannot overwrite
newer ones from another device.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlSessionBrowserTabs, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)

MAX_BROWSER_TABS = 20
MAX_TAB_ID_LENGTH = 64
MAX_TAB_URL_LENGTH = 8192
MAX_TAB_TITLE_LENGTH = 512


class BrowserTabsConflictError(Exception):
    """A write named a revision other than the stored one.

    :ivar current: The stored tabs the client should adopt before writing again.
    """

    def __init__(self, current: SessionBrowserTabs) -> None:
        super().__init__(
            f"Browser tabs are at revision {current.revision}, not the revision the write named"
        )
        self.current = current


class UnreadableBrowserTabsError(Exception):
    """The stored tab document cannot be read."""


@dataclass(frozen=True)
class BrowserTab:
    """One open tab.

    :ivar id: The client's tab id, e.g. ``"agent-browser"`` or a UUID.
    :ivar url: The tab's last http(s) page, or ``None`` for a tab with no page yet.
    :ivar title: That page's title, or ``None``.
    """

    id: str
    url: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class SessionBrowserTabs:
    """A user's tabs in one session.

    :ivar revision: ``0`` until the first write, then incremented by each write.
    :ivar tabs: The open tabs in display order.
    """

    revision: int
    tabs: tuple[BrowserTab, ...]


_TABS = TypeAdapter(tuple[BrowserTab, ...])
_EMPTY = SessionBrowserTabs(revision=0, tabs=())


def validate_tabs(tabs: tuple[BrowserTab, ...]) -> None:
    """Check a tab list against the stored limits.

    :raises ValueError: Too many tabs, a duplicate or oversized id, or an
        oversized or non-http(s) URL or title.
    """
    if len(tabs) > MAX_BROWSER_TABS:
        raise ValueError(f"At most {MAX_BROWSER_TABS} browser tabs are saved per session")
    seen: set[str] = set()
    for tab in tabs:
        if not tab.id or len(tab.id) > MAX_TAB_ID_LENGTH:
            raise ValueError(f"Tab ids must be 1 to {MAX_TAB_ID_LENGTH} characters")
        if tab.id in seen:
            raise ValueError(f"Duplicate tab id {tab.id!r}")
        seen.add(tab.id)
        if tab.url is not None:
            if len(tab.url) > MAX_TAB_URL_LENGTH:
                raise ValueError(f"Tab URLs must be at most {MAX_TAB_URL_LENGTH} characters")
            if not tab.url.startswith(("http://", "https://")):
                raise ValueError("Tab URLs must be http or https")
        if tab.title is not None and len(tab.title) > MAX_TAB_TITLE_LENGTH:
            raise ValueError(f"Tab titles must be at most {MAX_TAB_TITLE_LENGTH} characters")


def _decode(revision: int, text: str) -> SessionBrowserTabs:
    try:
        tabs = _TABS.validate_python(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise UnreadableBrowserTabsError("Unreadable session browser tab document") from exc
    return SessionBrowserTabs(revision=revision, tabs=tabs)


def _encode(tabs: tuple[BrowserTab, ...]) -> str:
    return json.dumps([asdict(tab) for tab in tabs], separators=(",", ":"))


class SessionBrowserTabStore:
    """SQLAlchemy-backed store of each user's browser tabs per session."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy URI of the database holding
            ``session_browser_tabs``, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_browser_tab_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_browser_tab_store", immediate=True
        )

    def _load(
        self, session: Session, conversation_id: str, user_id: str, *, lock: bool
    ) -> SessionBrowserTabs | None:
        stmt = select(SqlSessionBrowserTabs.revision, SqlSessionBrowserTabs.tabs).where(
            SqlSessionBrowserTabs.workspace_id == current_workspace_id(),
            SqlSessionBrowserTabs.conversation_id == conversation_id,
            SqlSessionBrowserTabs.user_id == user_id,
        )
        row = session.execute(stmt.with_for_update() if lock else stmt).one_or_none()
        return None if row is None else _decode(row.revision, row.tabs)

    def read(self, conversation_id: str, user_id: str) -> SessionBrowserTabs:
        """Return the user's tabs in the session, empty at revision 0 when none were saved.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param user_id: The tab owner, e.g. ``"alice@example.com"``.
        :raises UnreadableBrowserTabsError: The stored document cannot be read.
        """
        with self._session("get_session_browser_tabs") as session:
            return self._load(session, conversation_id, user_id, lock=False) or _EMPTY

    def replace(
        self,
        conversation_id: str,
        user_id: str,
        tabs: tuple[BrowserTab, ...],
        *,
        base_revision: int,
    ) -> SessionBrowserTabs:
        """Replace the user's tabs if they are still at ``base_revision``.

        A write that changes nothing keeps the current revision.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param user_id: The tab owner, e.g. ``"alice@example.com"``.
        :param tabs: The new tabs in display order.
        :param base_revision: The revision the client last read or wrote.
        :returns: The stored tabs after the write.
        :raises ValueError: ``tabs`` exceeds a limit (see :func:`validate_tabs`).
        :raises BrowserTabsConflictError: The stored revision is not ``base_revision``.
        :raises UnreadableBrowserTabsError: The stored document cannot be read.
        """
        validate_tabs(tabs)

        def write(session: Session) -> SessionBrowserTabs:
            current = self._load(session, conversation_id, user_id, lock=True)
            stored = current or _EMPTY
            if stored.revision != base_revision:
                raise BrowserTabsConflictError(stored)
            if current is not None and current.tabs == tabs:
                return current
            updated = SessionBrowserTabs(revision=base_revision + 1, tabs=tabs)
            if current is None:
                session.add(
                    SqlSessionBrowserTabs(
                        conversation_id=conversation_id,
                        user_id=user_id,
                        revision=updated.revision,
                        tabs=_encode(tabs),
                        updated_at=now_epoch_us(),
                    )
                )
            else:
                session.execute(
                    update(SqlSessionBrowserTabs)
                    .where(
                        SqlSessionBrowserTabs.workspace_id == current_workspace_id(),
                        SqlSessionBrowserTabs.conversation_id == conversation_id,
                        SqlSessionBrowserTabs.user_id == user_id,
                    )
                    .values(
                        revision=updated.revision,
                        tabs=_encode(tabs),
                        updated_at=now_epoch_us(),
                    )
                )
            return updated

        try:
            return run_write_transaction(
                self._session_immediate, "save_session_browser_tabs", write
            )
        except IntegrityError as exc:
            # Another device created the row first; its revision supersedes this write.
            raise BrowserTabsConflictError(self.read(conversation_id, user_id)) from exc
