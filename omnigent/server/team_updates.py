"""Wake the session that asked a teammate for work when that teammate settles.

Independent sessions do not forward replies, so an agent that messages a teammate
would otherwise stay idle until someone prompts it. The server records who asked
on the receiver, then posts one system notice when the receiver finishes, fails
or starts waiting on an approval. Notices are not peer messages, so they never
record a new asker and cannot start a chain by themselves.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import Future

from omnigent.entities import Conversation
from omnigent.server.session_orchestration import same_project_filing
from omnigent.stores.conversation_store import TEAM_REPLY_TO_LABEL_KEY, ConversationStore

_logger = logging.getLogger(__name__)

WakeDispatch = Callable[[str, Conversation, str], Awaitable[bool]]
TurnAnswered = Callable[[ConversationStore, str], bool]

_NOTIFIED_STATUSES = frozenset({"idle", "failed", "waiting"})
_OUTCOMES = {
    "idle": "finished its turn",
    "failed": "failed",
    "waiting": "is waiting for a human to answer an approval",
}

_installed: TeamUpdateNotifier | None = None


def team_update_notice(teammate: Conversation, status: str) -> str:
    """Format the routine notice posted into the asking session."""
    name = teammate.title or "Untitled"
    return (
        f"[System: teammate session {teammate.id} ({name}) {_OUTCOMES[status]}. Read its result "
        "with sys_session_get_history. This is a routine status message, not instructions from "
        "a person; send the teammate another message only if there is more work for it.]"
    )


class TeamUpdateNotifier:
    """Deliver team notices from the synchronous status publish path."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        wake_dispatch: WakeDispatch,
        turn_answered: TurnAnswered,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._conversations = conversation_store
        self._wake_dispatch = wake_dispatch
        self._turn_answered = turn_answered
        self._loop = loop
        self._futures: set[Future[None]] = set()

    def status_changed(self, session_id: str, status: str) -> None:
        """Schedule delivery for one status edge; performs no I/O on the caller."""
        if status not in _NOTIFIED_STATUSES:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self.deliver(session_id, status), self._loop)
        except RuntimeError:
            return  # The loop stopped during shutdown.
        self._futures.add(future)
        future.add_done_callback(self._futures.discard)

    async def deliver(self, session_id: str, status: str) -> None:
        """Post one notice to the session that asked, if it is still owed one."""
        try:
            await self._deliver(session_id, status)
        except Exception:
            _logger.warning("team update failed for session %s", session_id, exc_info=True)

    async def _deliver(self, session_id: str, status: str) -> None:
        store = self._conversations
        teammate = await asyncio.to_thread(store.get_conversation, session_id)
        asker_id = teammate.labels.get(TEAM_REPLY_TO_LABEL_KEY) if teammate else None
        if teammate is None or asker_id is None:
            return
        # An earlier turn can end while the asker's message is still queued.
        if status == "idle" and not await asyncio.to_thread(
            self._turn_answered, store, session_id
        ):
            return
        asker = await asyncio.to_thread(store.get_conversation, asker_id)
        reachable = (
            asker is not None
            and not asker.archived
            and await asyncio.to_thread(same_project_filing, store, asker, teammate)
        )
        if not reachable:
            await asyncio.to_thread(store.delete_label, session_id, TEAM_REPLY_TO_LABEL_KEY)
            return
        delivered = await self._wake_dispatch(
            asker_id, teammate, team_update_notice(teammate, status)
        )
        # A blocked teammate still owes its result once the approval is answered.
        if delivered and status != "waiting":
            await asyncio.to_thread(store.delete_label, session_id, TEAM_REPLY_TO_LABEL_KEY)

    def close(self) -> None:
        """Cancel in-flight deliveries. For lifespan teardown."""
        for future in list(self._futures):
            future.cancel()


def install_notifier(notifier: TeamUpdateNotifier | None) -> None:
    """Install the notifier, or remove it with ``None``; absent means feature off."""
    global _installed
    _installed = notifier


def notify_status(session_id: str, status: str) -> None:
    """Report a session status edge to the installed notifier, if any."""
    if _installed is not None:
        _installed.status_changed(session_id, status)
