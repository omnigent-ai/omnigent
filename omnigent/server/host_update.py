"""Whether a session's host needs an update to relay the project board tools.

A host built before it reported the ``project_tools`` capability may still
relay the tools (a source build from before the report, say). So a host that
doesn't report it only counts as outdated when the session hasn't written to
the board as itself recently: tools that work leave the session's id on its
ticket events, while writes made over REST on the owner's login don't.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from omnigent.db.db_models import SqlProjectTicketEvent, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.util.uuid import uuid_to_bytes

#: Board writes this recent, attributed to the session, show its tools work.
ATTRIBUTED_WRITE_WINDOW_S = 7 * 24 * 3600


@dataclass(frozen=True)
class HostUpdate:
    """The update notice for one session, as its viewer should see it.

    :param needed: Show the notice: the viewer owns the session's live host,
        which doesn't report the board tools, and the session hasn't written
        to the board as itself recently.
    :param host_id: The session's host, e.g. ``"2fd786c75c03cfbbec099a6820c08b62"``.
    :param host_version: The version that host reports, e.g. ``"0.24.0.dev0"``.
    """

    needed: bool
    host_id: str | None = None
    host_version: str | None = None


class AttributedBoardWrites:
    """Looks up whether a session wrote to its project's board as itself."""

    def __init__(self, ticket_location: str) -> None:
        self._db = make_named_managed_session_maker(
            get_or_create_engine(ticket_location), query_name_prefix="omnigent.host_update"
        )

    def recent(self, project_id: str, session_id: str, *, now: float | None = None) -> bool:
        """Whether *session_id* has a ticket event in *project_id* within the window."""
        since = int((time.time() if now is None else now) - ATTRIBUTED_WRITE_WINDOW_S)
        with self._db("find_session_board_write") as session:
            found = session.scalar(
                select(SqlProjectTicketEvent.id)
                .where(
                    SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                    SqlProjectTicketEvent.project_id == uuid_to_bytes(project_id).hex(),
                    SqlProjectTicketEvent.session_id == uuid_to_bytes(session_id).hex(),
                    SqlProjectTicketEvent.created_at >= since,
                )
                .limit(1)
            )
        return found is not None


def host_update(
    conversation: Any,
    host_connection: Any,
    viewer: str | None,
    writes: AttributedBoardWrites | None,
) -> HostUpdate:
    """Decide the update notice for one session's viewer.

    :param conversation: The session, or ``None`` when it doesn't exist.
    :param host_connection: The live connection of the session's host, or
        ``None`` when it's offline or the session has none.
    :param viewer: The signed-in user, or ``None`` in single-user mode.
    :param writes: Attributed-write lookup, or ``None`` when there's no board.
    """
    from omnigent.host.frames import CAP_PROJECT_TOOLS

    if conversation is None or host_connection is None:
        return HostUpdate(needed=False)
    hello = host_connection.hello
    found = HostUpdate(False, host_connection.host_id, hello.version)
    if CAP_PROJECT_TOOLS in hello.capabilities:
        return found
    # Only the person who runs that computer can update it. With sign-in on, a
    # host with no recorded owner shows nobody the notice rather than everyone.
    if viewer is not None and host_connection.owner != viewer:
        return found
    project_id = getattr(conversation, "project_id", None)
    if writes is not None and project_id and writes.recent(project_id, conversation.id):
        return found
    return HostUpdate(True, host_connection.host_id, hello.version)
