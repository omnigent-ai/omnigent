"""Accept startup messages into conversation history and recover them on the host's replica."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from starlette.datastructures import Headers

from omnigent.db.db_models import current_workspace_id
from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    SlashCommandData,
    StartupInputOptions,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_inputs
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.scheduled.telemetry import (
    current_fire_stage,
    current_scheduled_run,
    log_delivery,
    log_prompt_sent,
    scheduled_run_scope,
    start_fire_stage,
)
from omnigent.server.schemas import SessionEventInput, SessionStartupInput
from omnigent.stores.conversation_store import (
    PENDING_INPUT_RESPONSE_ID,
    ConversationNotFoundError,
    ConversationStore,
)
from omnigent.stores.host_store import host_is_live

_logger = logging.getLogger(__name__)
STARTUP_LABEL = "omnigent.startup"
# Terminal startup state of a launch withdrawn before it ran (a refused or
# superseded move). Nothing queues behind it, and an input admitted just before
# it was set settles itself when it sees it.
STARTUP_WITHDRAWN = "withdrawn"
# The move claim a startup was withdrawn or failed under. A move settles while
# it still holds the session, so a message held for it can be saved after; this
# tells that message the startup is settled, while a later move's claim, held
# before that move marks its own startup pending, isn't taken for it.
STARTUP_SETTLED_BY_LABEL = "omnigent.startup.settled_by"
_WITHDRAWN_INPUT_NOTE = (
    "The message wasn't sent: the sandbox launch it was waiting for was withdrawn. Send it again."
)
_FAILED_INPUT_NOTE = (
    "The message could not be delivered: the sandbox it was waiting for failed to launch."
)
# How long a submitted native input waits for its transcript echo before it stops
# holding back the messages behind it. The CLI can take a message without ever
# recording it (e.g. a slash command Claude Code answers only in its TUI), and then
# no echo comes. Matches the in-memory pending-input TTL.
_RECEIPT_TIMEOUT_S = 600.0

AUTOMATIC_TTL_ENV = "OMNIGENT_AUTOMATIC_MESSAGE_TTL_SECONDS"
_DEFAULT_AUTOMATIC_TTL_S = 720.0
# "<oldest expired created_at>:<count>" while automatic turns expired unseen.
EXPIRED_AUTOMATIC_LABEL = "omnigent.expired_automatic"
CATCH_UP = "catch_up"


def _automatic_ttl_s() -> float:
    raw = os.environ.get(AUTOMATIC_TTL_ENV, "").strip()
    try:
        return max(0.0, float(raw)) if raw else _DEFAULT_AUTOMATIC_TTL_S
    except ValueError:
        return _DEFAULT_AUTOMATIC_TTL_S


def automatic_kind(item: ConversationItem, session_id: str) -> str | None:
    """
    What sent a saved input on its own, e.g. ``"ticket_update"``, or None.

    None for a person's message and for the input a session was created for,
    such as a scheduled run's prompt in its new session: those stay for retry.
    """
    if item.id == initial_input_id(session_id):
        return None
    options = (
        item.data.startup_options
        if isinstance(item.data, (MessageData, SlashCommandData))
        else None
    )
    if options is None:
        return None
    if options.automatic:
        return options.automatic
    return "scheduled_prompt" if options.scheduled_run is not None else None


def _expired_ledger(conv: Conversation | None) -> tuple[int, int] | None:
    raw = conv.labels.get(EXPIRED_AUTOMATIC_LABEL) if conv is not None else None
    try:
        since, count = (int(part) for part in (raw or "").split(":"))
    except ValueError:
        return None
    return since, count


def catch_up_message(*, since: int, count: int, now: float, on_board: bool) -> str:
    """The one turn that replaces automatic messages this session never received."""
    minutes = max(1, round((now - since) / 60))
    started = time.strftime("%H:%M UTC", time.gmtime(since))
    plural = "" if count == 1 else "s"
    return (
        f"Catch-up: this session couldn't receive messages for about {minutes} minute"
        f"{'' if minutes == 1 else 's'} (since {started}), so {count} automatic update"
        f"{plural} (ticket digests, ticket updates or scheduled prompts) expired instead of "
        "being delivered. Messages people typed and messages from other sessions were kept. "
        + (
            f"Re-read the board for tickets that changed since {started} before acting."
            if on_board
            else "Check anything those updates would have covered before acting."
        )
    )


_UNCONFIRMED_INPUT_NOTE = (
    "The agent never confirmed it received this message. Check the terminal; "
    "if the agent didn't act on it, send it again."
)


def _settled_startup(conv: Conversation) -> str | None:
    """
    The terminal state a message saved now must settle with, if any.

    A withdrawn startup, or one a move failed, unless a later move has taken the
    session since. A create's failed launch is left to its own handling.
    """
    from omnigent.server.managed_hosts import is_move_claim

    state = conv.labels.get(STARTUP_LABEL)
    settled_by = conv.labels.get(STARTUP_SETTLED_BY_LABEL)
    if state == STARTUP_WITHDRAWN or (state == "failed" and settled_by is not None):
        if not is_move_claim(conv.runner_id) or conv.runner_id == settled_by:
            return state
    return None


def initial_input_id(session_id: str) -> str:
    """Stable identity for a create request's first input."""
    return uuid5(NAMESPACE_URL, f"initial-input:{session_id}").hex


def input_content(item: ConversationItem) -> list[dict[str, Any]]:
    """Render a saved message or skill invocation as a pending bubble."""
    data = item.data.model_dump()
    if item.type == "slash_command":
        return [
            {"type": "input_text", "text": f"/{data['name']} {data.get('arguments', '')}".strip()}
        ]
    return data.get("content", [])


async def restore_pending_inputs(store: ConversationStore, session_id: str) -> None:
    """Restore native receipt matching after an AP restart, without submitting again."""
    for item in await asyncio.to_thread(store.list_pending_inputs, session_id):
        if (
            item.status == "incomplete"
            and pending_inputs.pending_id_for_stable_id(session_id, item.id) is None
        ):
            options = (
                item.data.startup_options
                if isinstance(item.data, (MessageData, SlashCommandData))
                else None
            )
            pending_id = pending_inputs.record(
                session_id,
                input_content(item),
                stable_id=item.id,
                created_by=item.created_by,
                background_titles_enabled=bool(options and options.background_titles_enabled),
            )
            conv = await asyncio.to_thread(store.get_conversation, session_id)
            if conv is not None and conv.labels.get(STARTUP_LABEL) == "cancelled":
                pending_inputs.mark_interrupted(session_id, [pending_id])


@dataclass
class StartupInputContext:
    """The accepted sender's identity, independent of the browser's lifetime."""

    app: Any
    headers: Headers
    user_id: str | None
    input_id: str
    submitted: bool = False

    async def is_disconnected(self) -> bool:
        return False

    async def claim(self, store: ConversationStore, session_id: str) -> bool:
        # Leave the row queued throughout launch, authorization and policy checks.
        # A submitted native input is never replayed without its transcript receipt.
        start_fire_stage("input_claim", session_id=session_id)
        self.submitted = await asyncio.to_thread(
            store.transition_pending_input,
            session_id,
            self.input_id,
            expected="in_progress",
            status="incomplete",
        )
        if self.submitted:
            start_fire_stage("prompt_send", session_id=session_id)
        return self.submitted


StartupDispatch = Callable[
    [StartupInputContext, str, SessionEventInput], Coroutine[Any, Any, dict[str, bool | str]]
]


_services: weakref.WeakSet[ManagedStartupInputs] = weakref.WeakSet()


def wake_startup_worker(host_id: str) -> None:
    """Prompt a running host worker to look for the next saved input now."""
    key = (current_workspace_id(), host_id)
    for service in _services:
        entry = service._workers.get(key)
        if entry is not None:
            entry[2].set()


class ManagedStartupInputs:
    """Recover pending conversation items on the replica connected to their host."""

    def __init__(
        self,
        conversations: ConversationStore,
        hosts: HostRegistry,
        *,
        include_external_hosts: bool = False,
    ) -> None:
        self.conversations = conversations
        self.hosts = hosts
        self.include_external_hosts = include_external_hosts
        _services.add(self)
        self.dispatch: StartupDispatch | None = None
        self._workers: dict[
            tuple[int, str], tuple[HostConnection, asyncio.Task[None], asyncio.Event]
        ] = {}
        self._deliveries: dict[tuple[int, str], asyncio.Task[Any]] = {}
        self._runner_workers: dict[tuple[int, str], tuple[asyncio.Task[None], asyncio.Event]] = {}
        self._receipt_watches: set[asyncio.Task[None]] = set()

    async def enqueue(
        self,
        session_id: str,
        event: SessionEventInput,
        *,
        user_id: str | None,
        headers: Headers,
        first: bool = True,
        automatic: str | None = None,
    ) -> ConversationItem:
        from omnigent.server.background_session_titles import background_session_titles_enabled
        from omnigent.server.routes._sessions.helpers import (
            _build_new_item,
            _stable_id_reuse_is_exact_retry,
        )

        try:
            SessionStartupInput.model_validate(event.model_dump())
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        raw_id = event.data.get("stable_id")
        input_id = (
            initial_input_id(session_id)
            if first
            else (
                raw_id
                if isinstance(raw_id, str) and re.fullmatch(r"[0-9a-f]{32}", raw_id)
                else uuid4().hex
            )
        )
        if event.type == "slash_command":
            event = event.model_copy(
                update={"data": {"agent": "pending", "arguments": "", **event.data}}
            )
        item = _build_new_item(event, PENDING_INPUT_RESPONSE_ID, created_by=user_id).model_copy(
            update={"stable_id": input_id, "status": "in_progress"}
        )
        item.data = item.data.model_copy(
            update={
                "startup_options": StartupInputOptions(
                    model_override=event.model_override,
                    tools=event.tools,
                    background_titles_enabled=background_session_titles_enabled(headers),
                    scheduled_run=current_scheduled_run(),
                    automatic=automatic,
                )
            }
        )
        saved = await asyncio.to_thread(self.conversations.append, session_id, [item])
        if saved[0].deduplicated and not _stable_id_reuse_is_exact_retry(saved[0], item):
            raise OmnigentError(
                "This input id already belongs to a different message", code=ErrorCode.CONFLICT
            )
        if first:
            await asyncio.to_thread(
                self.conversations.set_labels, session_id, {STARTUP_LABEL: "pending"}
            )
        else:
            conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
            settled = _settled_startup(conv) if conv is not None else None
            if settled is not None:
                # Settled after this was admitted, possibly after the settlement
                # failed what it saw: settle this input the same way.
                if await self._fail_or_expire(session_id, saved[0], expected="in_progress"):
                    withdrawn = settled == STARTUP_WITHDRAWN
                    await self._report_failure(
                        session_id,
                        "launch_withdrawn" if withdrawn else "launch_failed",
                        _WITHDRAWN_INPUT_NOTE if withdrawn else _FAILED_INPUT_NOTE,
                        publish_status=False,
                    )
            elif conv is not None and STARTUP_LABEL not in conv.labels:
                # The startup finished as this was saved; keep later messages behind it.
                await asyncio.to_thread(
                    self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
                )
        return saved[0]

    async def record_launch_failure(
        self,
        session_id: str,
        reason: str | Callable[[], Awaitable[str]],
        *,
        move_claim: str | None = None,
    ) -> str:
        """
        Mark a failed launch on the session, for replicas without its tracker.

        No sandbox is left to deliver the saved inputs, so they are failed and
        the transcript says why. A deferred reason is resolved only after queued
        inputs are failed, so diagnostic I/O cannot race their delivery. Returns
        the resolved reason for callers recording the same failure elsewhere.

        :param move_claim: The claim of the move whose launch failed, which it
            still holds, or ``None`` for a create's launch.
        """
        undelivered = False
        labels = {STARTUP_LABEL: "failed"}
        if move_claim is not None:
            labels[STARTUP_SETTLED_BY_LABEL] = move_claim
        if await asyncio.to_thread(self.conversations.get_conversation, session_id) is not None:
            with contextlib.suppress(ConversationNotFoundError):
                await asyncio.to_thread(self.conversations.set_labels, session_id, labels)
            for item in await asyncio.to_thread(
                self.conversations.list_pending_inputs, session_id
            ):
                if item.status == "in_progress":
                    undelivered |= await self._fail_or_expire(
                        session_id, item, expected="in_progress"
                    )
        reason = await reason() if callable(reason) else reason
        if undelivered:
            await self._report_failure(
                session_id,
                "launch_failed",
                f"The message could not be delivered: {reason[:1000]}",
            )
        return reason

    async def release_held_inputs(
        self, session_id: str, message: str, *, move_claim: str | None
    ) -> None:
        """
        Settle a startup whose launch won't happen, without failing the session.

        The session keeps running where it is (its old host, or one that took
        it before the sandbox was ready). Messages held for the sandbox were
        never sent: they are failed with *message* so the user can send them
        again. The startup becomes :data:`STARTUP_WITHDRAWN` first, so later
        messages reach that host and one admitted meanwhile settles itself.

        :param session_id: The session the launch was for.
        :param message: Transcript note for the held messages, e.g. why they
            weren't sent.
        :param move_claim: The claim of the move whose launch this was, or
            ``None`` for a create's launch.
        """
        labels = {STARTUP_LABEL: STARTUP_WITHDRAWN}
        if move_claim is not None:
            labels[STARTUP_SETTLED_BY_LABEL] = move_claim
        with contextlib.suppress(ConversationNotFoundError):
            await asyncio.to_thread(self.conversations.set_labels, session_id, labels)
        undelivered = False
        for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            if item.status == "in_progress":
                undelivered |= await self._fail_or_expire(session_id, item, expected="in_progress")
        if undelivered:
            await self._report_failure(
                session_id, "launch_withdrawn", message, publish_status=False
            )

    async def accepts(self, conv: Conversation, app: Any) -> bool:
        """Whether a new message must wait behind the session's startup."""
        from omnigent.server.managed_hosts import is_move_claim

        if is_move_claim(conv.runner_id):
            # A move onto a new sandbox holds the session from its claim until
            # the sandbox's runner replaces it: hold the message for the sandbox,
            # on any replica. A move that gives up settles what it held, then
            # releases the claim.
            return True
        state = conv.labels.get(STARTUP_LABEL)
        if state not in ("pending", "ready"):
            return False
        # A failed launch can't deliver anything; the message path reports why.
        # The tracker knows on the launching replica, the label everywhere.
        tracker = getattr(app.state, "managed_launches", None)
        launch = tracker.get(conv.id) if tracker is not None else None
        if launch is not None and launch.error is not None:
            return False
        if await self.has_undelivered(conv.id):
            return True
        if state != "pending":
            return False
        # Nothing is saved yet: hold the message only while the launch can
        # still deliver it. A bound host that is gone needs the message path's
        # relaunch instead.
        if launch is not None or conv.host_id is None:
            return True
        host_store = getattr(app.state, "host_store", None)
        host = (
            await asyncio.to_thread(host_store.get_host, conv.host_id)
            if host_store is not None
            else None
        )
        return host is not None and host_is_live(host)

    async def has_undelivered(self, session_id: str) -> bool:
        """Whether a saved input is still to be delivered (or confirmed) here."""
        return any(
            item.status in ("in_progress", "incomplete")
            for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        )

    async def snapshot(self, session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Render the session's saved inputs for its snapshot.

        :param session_id: Session/conversation identifier.
        :returns: ``(queued, failed)``, each a list of
            ``{"pending_id", "content", "created_by"?}`` in submission order.
            ``failed`` holds inputs no launch or delivery could hand over, so
            clients can show them as undelivered instead of dropping them.
        """
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.labels.get(STARTUP_LABEL) == "cancelled":
            return [], []
        queued: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            entry = {
                "pending_id": pending_inputs.make_pending_id(session_id, item.id),
                "content": input_content(item),
                **({"created_by": item.created_by} if item.created_by else {}),
            }
            (failed if item.status == "failed" else queued).append(entry)
        return queued, failed

    def host_changed(self, app: Any, host_id: str) -> None:
        key = (current_workspace_id(), host_id)
        connection = self.hosts.get(host_id)
        existing = self._workers.get(key)
        if existing is not None:
            previous, worker, wake = existing
            if connection is previous and not worker.done():
                wake.set()
                return
            worker.cancel()
        if connection is None or (
            not connection.registered_with_managed_token and not self.include_external_hosts
        ):
            return
        wake = asyncio.Event()

        async def serve() -> None:
            after = None
            delay = 0.25
            while self.hosts.get(host_id) is connection:
                busy = False
                try:
                    ids = await asyncio.to_thread(
                        self.conversations.list_host_conversation_ids, host_id, after=after
                    )
                    for session_id in ids:
                        if self.hosts.get(host_id) is not connection:
                            return
                        busy |= await self._recover_session(app, session_id, connection)
                    after = ids[-1] if len(ids) == 32 else None
                    delay = 0.25 if busy or after else min(delay * 2, 30.0)
                except Exception:
                    _logger.exception("Unable to recover startup messages on host %s", host_id)
                    after = None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                wake.clear()

        task = asyncio.create_task(serve(), name=f"startup-inputs-{host_id}")
        self._workers[key] = (connection, task, wake)

        def remove_worker(done: asyncio.Task[None]) -> None:
            if self._workers.get(key, (None, None, None))[1] is done:
                self._workers.pop(key, None)

        task.add_done_callback(remove_worker)

    async def deliver_pending(self, app: Any, session_id: str) -> bool:
        """Schedule a saved input on an ordinary session; keep offline inputs queued."""
        key = (current_workspace_id(), session_id)
        if existing := self._runner_workers.get(key):
            existing[1].set()
            return True
        if key in self._deliveries:
            return True
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.archived or conv.agent_id is None:
            return False
        if conv.host_id is not None:
            connection = self.hosts.get(conv.host_id)
            if connection is None:
                return False
            self.host_changed(app, conv.host_id)
            return True
        if conv.runner_id is None or app.state.tunnel_registry.get(conv.runner_id) is None:
            return False
        wake = asyncio.Event()

        async def drain() -> None:
            try:
                while True:
                    wake.clear()
                    try:
                        current = await asyncio.to_thread(
                            self.conversations.get_conversation, session_id
                        )
                        if (
                            current is None
                            or current.archived
                            or current.host_id is not None
                            or current.labels.get(STARTUP_LABEL) == "cancelled"
                            or current.runner_id is None
                            or app.state.tunnel_registry.get(current.runner_id) is None
                        ):
                            return
                        items = await asyncio.to_thread(
                            self.conversations.list_pending_inputs, session_id
                        )
                        head = next((item for item in items if item.status != "failed"), None)
                        if head is None:
                            await self._finish(session_id)
                            # A writer can wake this task while it is finishing.
                            if wake.is_set():
                                continue
                            return
                        if head.status == "in_progress":
                            await self._deliver(app, session_id, head)
                    except Exception:
                        _logger.exception("Unable to recover saved inputs for %s", session_id)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), timeout=0.25)
            finally:
                self._runner_workers.pop(key, None)

        self._runner_workers[key] = (
            asyncio.create_task(drain(), name=f"session-inputs-{session_id}"),
            wake,
        )
        return True

    async def _recover_session(
        self, app: Any, session_id: str, connection: HostConnection
    ) -> bool:
        """Advance one session's startup here; return whether work is in flight."""
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.archived or conv.host_id != connection.host_id:
            return False
        from omnigent.server.managed_hosts import is_move_claim, move_claim_is_live

        if is_move_claim(conv.runner_id):
            # A move holds the session until its sandbox's runner replaces the
            # claim or the claim is handed over to this replica: nothing to deliver
            # or finish yet, and soon a runner to start.
            return move_claim_is_live(conv.runner_id)
        state = conv.labels.get(STARTUP_LABEL)
        if state == "failed":
            return False
        items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        if state == "cancelled":
            if not items:
                await self._finish(session_id)
            return False
        # A refused input waits for an explicit retry without holding back the
        # ones behind it. One submitted to a native harness and awaiting its
        # transcript receipt still does: it may already have run.
        head = next((item for item in items if item.status != "failed"), None)
        if head is not None:
            # Delivered even without the label: a message saved while the label
            # was being cleared still belongs to this session's startup.
            key = (current_workspace_id(), session_id)
            if head.status == "in_progress" and key not in self._deliveries:
                task = asyncio.create_task(
                    self._deliver(app, session_id, head), name=f"startup-input-{session_id}"
                )
                self._deliveries[key] = task
            return head.status == "in_progress"
        if state == "pending" and conv.runner_id is None:
            # The launch bound this host on a replica without its tunnel, so it
            # could not start the runner; start it here.
            from omnigent.server.routes.sessions import _launch_runner_on_host

            attempt = await _launch_runner_on_host(
                conv, self.conversations, self.hosts, connection
            )
            if attempt.error or attempt.error_code:
                reason = attempt.error or attempt.error_code or "the host refused the launch"
                await self._report_failure(
                    session_id,
                    f"launch_{attempt.runner_id}",
                    f"The agent could not start on the sandbox: {reason[:1000]}",
                )
            return True
        if state is not None and not items:
            # A runner is bound and nothing is left to deliver.
            await self._finish(session_id)
        return False

    async def _finish(self, session_id: str) -> None:
        """Clear a drained session's startup state so later reads skip it."""
        await asyncio.to_thread(self.conversations.delete_label, session_id, STARTUP_LABEL)
        # An input saved while the label was being cleared must still be delivered.
        if await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            await asyncio.to_thread(
                self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
            )

    async def _deliver(self, app: Any, session_id: str, item: ConversationItem) -> None:
        options = (
            item.data.startup_options
            if isinstance(item.data, (MessageData, SlashCommandData))
            else None
        )
        with scheduled_run_scope(
            options.scheduled_run if options else None, session_id=session_id
        ):
            start_fire_stage("input_validation", session_id=session_id)
            await self._deliver_scoped(app, session_id, item)

    async def _deliver_scoped(self, app: Any, session_id: str, item: ConversationItem) -> None:
        if self.dispatch is None:
            return
        key = (current_workspace_id(), session_id)
        task = asyncio.current_task()
        assert task is not None
        if key in self._deliveries and self._deliveries[key] is not task:
            return
        self._deliveries[key] = task
        context: StartupInputContext | None = None
        sent = False
        catch_up = False
        dispatch_id = uuid4().hex
        try:
            conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
            current = await asyncio.to_thread(self.conversations.get_item, session_id, item.id)
            if (
                conv is None
                or conv.labels.get(STARTUP_LABEL) == "cancelled"
                or current is None
                or current.response_id != PENDING_INPUT_RESPONSE_ID
                or current.status != "in_progress"
            ):
                return
            kind = automatic_kind(current, session_id)
            if kind not in (None, CATCH_UP) and time.time() - current.created_at > (
                _automatic_ttl_s()
            ):
                # Stale automatic turns are replaced by one catch-up note.
                catch_up = await self._expire_stale(session_id)
                return
            options = (
                item.data.startup_options
                if isinstance(item.data, (MessageData, SlashCommandData))
                else None
            )
            context = StartupInputContext(
                app,
                Headers(
                    {
                        "x-omnigent-background-session-titles": "on"
                        if options and options.background_titles_enabled
                        else "off",
                    }
                ),
                item.created_by,
                item.id,
            )
            event = SessionEventInput(
                type=item.type,
                data={**item.data.model_dump(exclude={"startup_options"}), "stable_id": item.id},
                model_override=options.model_override if options else None,
                tools=options.tools if options else None,
            )
            log_delivery(
                "scheduled_run_prompt_dispatch_started",
                session_id=session_id,
                input_id=item.id,
                dispatch_id=dispatch_id,
            )
            response = await self.dispatch(context, session_id, event)
            if response.get("denied"):
                raise ValueError(str(response.get("reason", "Input denied by policy")))
            if context.submitted:
                sent = True
                log_prompt_sent(session_id, input_id=item.id, dispatch_id=dispatch_id)
                start_fire_stage("post_delivery", session_id=session_id)
                if not response.get("pending_id") and not response.get("item_id"):
                    # Native overlays such as /btw have no transcript echo.
                    from omnigent.server.routes.sessions import (
                        _build_new_item,
                        _publish_input_consumed,
                    )

                    receipt = _build_new_item(
                        event, "startup_command", created_by=item.created_by
                    ).model_copy(update={"stable_id": item.id, "replaces_pending_id": item.id})
                    [saved] = await asyncio.to_thread(
                        self.conversations.append, session_id, [receipt]
                    )
                    _publish_input_consumed(
                        session_id,
                        saved,
                        cleared_pending_id=pending_inputs.make_pending_id(session_id, item.id),
                    )
                elif pending_id := response.get("pending_id"):
                    watch = asyncio.create_task(
                        self._expire_unconfirmed(session_id, item.id, str(pending_id)),
                        name=f"startup-input-receipt-{session_id}",
                    )
                    self._receipt_watches.add(watch)
                    watch.add_done_callback(self._receipt_watches.discard)
                await asyncio.to_thread(
                    self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
                )
                if kind == CATCH_UP:
                    await asyncio.to_thread(
                        self.conversations.delete_label, session_id, EXPIRED_AUTOMATIC_LABEL
                    )
                else:
                    catch_up = True
        except asyncio.CancelledError:
            log_delivery(
                "scheduled_run_prompt_dispatch_interrupted",
                session_id=session_id,
                failure_stage=current_fire_stage(),
                input_id=item.id,
                dispatch_id=dispatch_id,
                outcome="sent"
                if sent
                else "uncertain"
                if context and context.submitted
                else "not_sent",
            )
            raise
        except Exception as exc:
            if await self._waits_out_session_move(session_id, exc):
                return
            log_delivery(
                "scheduled_run_stage_failed",
                session_id=session_id,
                input_id=item.id,
                failure_stage="post_delivery" if sent else current_fire_stage(),
                dispatch_id=dispatch_id,
                outcome="sent"
                if sent
                else "uncertain"
                if context and context.submitted
                else "not_sent",
                exception_type=type(exc).__name__,
            )
            _logger.exception("Unable to deliver saved message for session %s", session_id)
            if (context is None or not context.submitted) and automatic_kind(
                item, session_id
            ) is not None:
                # Nobody typed it: it expires quietly instead of showing as undelivered.
                await self._expire_automatic(session_id, item, expected="in_progress")
                return
            if context is None or not context.submitted:
                await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="in_progress",
                    status="failed",
                )
            uncertain = bool(context and context.submitted)
            await self._report_failure(
                session_id,
                item.id,
                (
                    "Delivery could not be confirmed, so later messages wait behind it. "
                    "Check the terminal; if the agent never got it, press Stop, which "
                    "also clears the waiting messages, then send them again. "
                    if uncertain
                    else "The message is saved but could not be delivered. "
                )
                + str(exc)[:1000],
            )
        finally:
            self._deliveries.pop(key, None)
            if catch_up:
                try:
                    await self._catch_up(app, session_id, item.created_by)
                except Exception:
                    _logger.exception("Unable to send a catch-up note to session %s", session_id)

    async def _waits_out_session_move(self, session_id: str, exc: Exception) -> bool:
        """Whether *exc* refused delivery only while ``/clear`` moves the session.

        The input stays queued; the host worker delivers it once the move
        ends, as after any other transient wait.
        """
        from omnigent.server.routes._sessions.orchestration import _session_is_moving

        if not (
            isinstance(exc, OmnigentError)
            and exc.code == ErrorCode.CONFLICT
            and _session_is_moving(session_id)
        ):
            return False
        _logger.info("Saved message for session %s waits for /clear to finish", session_id)
        return True

    async def _expire_unconfirmed(self, session_id: str, item_id: str, pending_id: str) -> None:
        """Fail a submitted input whose transcript echo never came, freeing the ones behind it."""
        await asyncio.sleep(_RECEIPT_TIMEOUT_S)
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.labels.get(STARTUP_LABEL) == "cancelled":
            return
        current = await asyncio.to_thread(self.conversations.get_item, session_id, item_id)
        if current is not None and automatic_kind(current, session_id) is not None:
            if await self._expire_automatic(session_id, current, expected="incomplete"):
                pending_inputs.resolve(session_id, pending_id)
                if conv.host_id is not None:
                    wake_startup_worker(conv.host_id)
            return
        # The echo replaces the row, so only an input still awaiting it changes here.
        if not await asyncio.to_thread(
            self.conversations.transition_pending_input,
            session_id,
            item_id,
            expected="incomplete",
            status="failed",
        ):
            return
        # Its queued bubble now renders from the failed row instead.
        pending_inputs.resolve(session_id, pending_id)
        await self._report_failure(
            session_id, f"unconfirmed_{item_id}", _UNCONFIRMED_INPUT_NOTE, publish_status=False
        )
        if conv.host_id is not None:
            wake_startup_worker(conv.host_id)

    async def _fail_or_expire(
        self, session_id: str, item: ConversationItem, *, expected: str
    ) -> bool:
        """Fail an undeliverable input a person sent; an automatic one expires. Returns failed."""
        if automatic_kind(item, session_id) is not None:
            await self._expire_automatic(session_id, item, expected=expected)
            return False
        return await asyncio.to_thread(
            self.conversations.transition_pending_input,
            session_id,
            item.id,
            expected=expected,
            status="failed",
        )

    async def _expire_automatic(
        self, session_id: str, item: ConversationItem, *, expected: str
    ) -> bool:
        """Drop an automatic input and count it toward the session's catch-up note."""
        if not await asyncio.to_thread(
            self.conversations.transition_pending_input,
            session_id,
            item.id,
            expected=expected,
            status=None,
        ):
            return False
        pending_inputs.resolve(session_id, pending_inputs.make_pending_id(session_id, item.id))
        _logger.info(
            "Expired automatic %s message %s for session %s",
            automatic_kind(item, session_id),
            item.id,
            session_id,
        )
        if automatic_kind(item, session_id) == CATCH_UP:
            return True
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        since, count = _expired_ledger(conv) or (item.created_at, 0)
        with contextlib.suppress(ConversationNotFoundError):
            await asyncio.to_thread(
                self.conversations.set_labels,
                session_id,
                {EXPIRED_AUTOMATIC_LABEL: f"{min(since, item.created_at)}:{count + 1}"},
            )
        return True

    async def _expire_stale(self, session_id: str) -> bool:
        """Expire every queued automatic input older than the expiry window."""
        before = time.time() - _automatic_ttl_s()
        expired = False
        for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            kind = automatic_kind(item, session_id)
            if (
                item.status == "in_progress"
                and kind not in (None, CATCH_UP)
                and item.created_at < before
            ):
                expired |= await self._expire_automatic(session_id, item, expected="in_progress")
        return expired

    async def _catch_up(self, app: Any, session_id: str, user_id: str | None) -> None:
        """Queue one catch-up note once a session that missed automatic turns takes input."""
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        ledger = _expired_ledger(conv)
        if conv is None or conv.archived or ledger is None:
            return
        items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        if any(automatic_kind(item, session_id) == CATCH_UP for item in items):
            return
        since, count = ledger
        text = catch_up_message(
            since=since, count=count, now=time.time(), on_board=conv.project_id is not None
        )
        await self.enqueue(
            session_id,
            SessionEventInput(
                type="message",
                data={
                    "role": "user",
                    "stable_id": uuid5(
                        NAMESPACE_URL, f"catch-up:{session_id}:{since}:{count}"
                    ).hex,
                    "content": [{"type": "input_text", "text": text}],
                },
            ),
            user_id=user_id,
            headers=Headers(),
            first=False,
            automatic=CATCH_UP,
        )
        await self.deliver_pending(app, session_id)

    async def retry(self, session_id: str) -> bool:
        changed = False
        for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            if item.status == "failed":
                changed |= await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="failed",
                    status="in_progress",
                )
        return changed

    async def cancel(self, session_id: str) -> None:
        items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if not items and (conv is None or conv.labels.get(STARTUP_LABEL) != "pending"):
            return
        await asyncio.to_thread(
            self.conversations.set_labels, session_id, {STARTUP_LABEL: "cancelled"}
        )
        key = (current_workspace_id(), session_id)
        task = self._deliveries.get(key)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for item in items:
            if item.status == "incomplete":
                pending_inputs.mark_interrupted(
                    session_id, [pending_inputs.make_pending_id(session_id, item.id)]
                )
            else:
                await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected=item.status,
                    status=None,
                )

    async def _report_failure(
        self, session_id: str, failure_id: str, message: str, *, publish_status: bool = True
    ) -> None:
        """Record a startup failure in the transcript and, by default, the session's status."""
        from omnigent.server.routes.sessions import _publish_sandbox_status, _publish_status

        if await asyncio.to_thread(self.conversations.get_conversation, session_id) is None:
            return

        await asyncio.to_thread(
            self.conversations.append,
            session_id,
            [
                NewConversationItem(
                    type="error",
                    response_id=f"startup_{failure_id}",
                    stable_id=uuid5(
                        NAMESPACE_URL, f"startup-failure:{session_id}:{failure_id}"
                    ).hex,
                    data=ErrorData(
                        source="execution", code="startup_input_failed", message=message
                    ),
                )
            ],
        )
        if publish_status:
            _publish_sandbox_status(session_id, "failed", message)
            _publish_status(session_id, "failed", failure_origin="startup_input_failed")

    async def shutdown(self) -> None:
        tasks = [
            *(task for _, task, _ in self._workers.values()),
            *(task for task, _ in self._runner_workers.values()),
            *self._deliveries.values(),
            *self._receipt_watches,
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._runner_workers.clear()
