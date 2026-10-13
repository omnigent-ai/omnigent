"""Accept startup messages into conversation history and recover them on the host's replica."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable, Coroutine
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
from omnigent.server.schemas import SessionEventInput, SessionStartupInput
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID, ConversationStore

_logger = logging.getLogger(__name__)
STARTUP_LABEL = "omnigent.startup"


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
        self.submitted = await asyncio.to_thread(
            store.transition_pending_input,
            session_id,
            self.input_id,
            expected="in_progress",
            status="incomplete",
        )
        return self.submitted


StartupDispatch = Callable[
    [StartupInputContext, str, SessionEventInput], Coroutine[Any, Any, dict[str, bool | str]]
]


class ManagedStartupInputs:
    """Recover pending conversation items on the replica connected to their host."""

    def __init__(self, conversations: ConversationStore, hosts: HostRegistry) -> None:
        self.conversations = conversations
        self.hosts = hosts
        self.dispatch: StartupDispatch | None = None
        self._workers: dict[
            tuple[int, str], tuple[HostConnection, asyncio.Task[None], asyncio.Event]
        ] = {}
        self._deliveries: dict[tuple[int, str], asyncio.Task[Any]] = {}

    async def enqueue(
        self,
        session_id: str,
        event: SessionEventInput,
        *,
        user_id: str | None,
        headers: Headers,
        first: bool = True,
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
        return saved[0]

    async def accepts(self, conv: Conversation) -> bool:
        if conv.labels.get(STARTUP_LABEL) not in ("pending", "ready"):
            return False
        if conv.labels.get(STARTUP_LABEL) == "pending":
            return True
        return any(
            item.status in ("in_progress", "incomplete")
            for item in await asyncio.to_thread(self.conversations.list_pending_inputs, conv.id)
        )

    async def pending_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.labels.get(STARTUP_LABEL) == "cancelled":
            return []
        return [
            {
                "pending_id": pending_inputs.make_pending_id(session_id, item.id),
                "content": input_content(item),
                **({"created_by": item.created_by} if item.created_by else {}),
            }
            for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
            if item.status != "failed"
        ]

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
        if connection is None or not connection.registered_with_managed_token:
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

    async def _recover_session(
        self, app: Any, session_id: str, connection: HostConnection
    ) -> bool:
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or conv.host_id != connection.host_id:
            return False
        if conv.labels.get(STARTUP_LABEL) == "cancelled":
            return False
        items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        if items:
            first = items[0]
            key = (current_workspace_id(), session_id)
            if first.status == "in_progress" and key not in self._deliveries:
                task = asyncio.create_task(
                    self._deliver(app, session_id, first), name=f"startup-input-{session_id}"
                )
                self._deliveries[key] = task
            return first.status == "in_progress"
        if conv.labels.get(STARTUP_LABEL) != "pending":
            return False
        if conv.runner_id is None:
            from omnigent.server.routes.sessions import _launch_runner_on_host

            await _launch_runner_on_host(conv, self.conversations, self.hosts, connection)
            return True
        return False

    async def _deliver(self, app: Any, session_id: str, item: ConversationItem) -> None:
        if self.dispatch is None:
            return
        key = (current_workspace_id(), session_id)
        task = asyncio.current_task()
        assert task is not None
        if key in self._deliveries and self._deliveries[key] is not task:
            return
        self._deliveries[key] = task
        context: StartupInputContext | None = None
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
            response = await self.dispatch(context, session_id, event)
            if response.get("denied"):
                raise ValueError(str(response.get("reason", "Input denied by policy")))
            if context.submitted:
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
                await asyncio.to_thread(
                    self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception("Unable to deliver saved message for session %s", session_id)
            if context is None or not context.submitted:
                await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="in_progress",
                    status="failed",
                )
            await self._report_failure(
                session_id, item, str(exc), uncertain=bool(context and context.submitted)
            )
        finally:
            self._deliveries.pop(key, None)

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
        self, session_id: str, item: ConversationItem, detail: str, *, uncertain: bool
    ) -> None:
        from omnigent.server.routes.sessions import _publish_sandbox_status, _publish_status

        if await asyncio.to_thread(self.conversations.get_conversation, session_id) is None:
            return

        message = (
            "Delivery could not be confirmed; check the terminal before resending. "
            if uncertain
            else "The message is saved but could not be delivered. "
        ) + detail[:1000]
        await asyncio.to_thread(
            self.conversations.append,
            session_id,
            [
                NewConversationItem(
                    type="error",
                    response_id=f"startup_{item.id}",
                    stable_id=uuid5(NAMESPACE_URL, f"startup-failure:{session_id}:{item.id}").hex,
                    data=ErrorData(
                        source="execution", code="startup_input_failed", message=message
                    ),
                )
            ],
        )
        _publish_sandbox_status(session_id, "failed", message)
        _publish_status(session_id, "failed")

    async def shutdown(self) -> None:
        tasks = [task for _, task, _ in self._workers.values()] + list(self._deliveries.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
