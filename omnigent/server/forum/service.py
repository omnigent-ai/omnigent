"""Server-owned ordered discussion workers using the normal session event path."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from omnigent.db.db_models import current_workspace_id
from omnigent.runtime import session_stream
from omnigent.server.forum.state import ForumState
from omnigent.server.forum.store import ForumStore

_logger = logging.getLogger(__name__)
SessionEvent = Callable[[str, dict[str, Any]], Awaitable[Any]]
History = Callable[[str], Awaitable[list[dict[str, Any]]]]
Availability = Callable[[str], Awaitable[bool]]


def explicit_stop(text: str) -> bool:
    """Recognize standalone human stop requests; never inspect agent output."""
    normalized = re.sub(r"[.!?,;]+$", "", text.strip().lower()).replace("’", "'")
    return bool(
        re.fullmatch(
            r"(?:please\s+)?(?:stop(?:\s+(?:the\s+)?(?:discussion|forum|conversation|agents))?(?:\s+(?:now|please))?|"
            r"that(?:'s| is) enough(?:\s+(?:for now|thank you))?|thank(?:s| you) (?:all|everyone)|"
            r"we(?:'re| are) done|(?:end|finish) (?:the|this) (?:discussion|forum|conversation))",
            normalized,
        )
    )


class ForumService:
    def __init__(self, store: ForumStore) -> None:
        self.store = store
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._closing = False
        self._wake_again: set[tuple[int, str]] = set()

    def wake(
        self,
        forum_id: str,
        send: SessionEvent,
        history: History,
        available: Availability | None = None,
    ) -> None:
        if self._closing:
            return
        key = (current_workspace_id(), forum_id)
        task = self._tasks.get(key)
        if task is not None and not task.done():
            self._wake_again.add(key)
            return
        if task is None or task.done():
            worker = asyncio.create_task(self._run(forum_id, send, history, available))
            self._tasks[key] = worker

            def finished(task: asyncio.Task[None]) -> None:
                if self._tasks.get(key) is task:
                    self._tasks.pop(key, None)
                    if key in self._wake_again:
                        self._wake_again.discard(key)
                        self.wake(forum_id, send, history, available)

            worker.add_done_callback(finished)

    async def shutdown(self) -> None:
        self._closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._wake_again.clear()

    async def _run(
        self, forum_id: str, send: SessionEvent, history: History, available: Availability | None
    ) -> None:
        owner = uuid.uuid4().hex
        active: asyncio.Task[str] | None = None
        active_session: str | None = None
        token: str | None = None
        owns_lease = False
        try:
            while True:
                state = await asyncio.to_thread(
                    self.store.mutate, forum_id, lambda _s: None, owner=owner
                )
                owns_lease = state is not None
                if state is None:
                    return
                if active is not None:
                    if state.active_token != token or state.status not in {"running", "paused"}:
                        assert active_session is not None
                        with contextlib.suppress(Exception):
                            await send(active_session, {"type": "interrupt"})
                        active.cancel()
                        await asyncio.gather(active, return_exceptions=True)
                        active = None
                        active_session = None
                    elif active.done():
                        error = None
                        try:
                            text = active.result()
                        except Exception:
                            _logger.exception("Forum participant turn failed")
                            text, error = "", "Agent unavailable; retrying automatically"
                            if active_session:
                                with contextlib.suppress(Exception):
                                    await send(active_session, {"type": "interrupt"})
                        await asyncio.to_thread(
                            self.store.mutate,
                            forum_id,
                            lambda s, token=token, text=text, error=error: s.finish(
                                token or "", text, time.time(), error
                            ),
                            owner=owner,
                        )
                        active = None
                        active_session = None
                    else:
                        await asyncio.sleep(0.5)
                        continue
                if state.status in {"blank", "stopped", "limit", "deleting", "suspended"}:
                    latest = await asyncio.to_thread(self.store.get, forum_id)
                    if latest is not None and latest.status in {"running", "paused"}:
                        continue
                    return
                # A previous worker lost its lease while a turn was active.
                if active is None and state.active_token and state.active_token != token:
                    assert state.active_index is not None
                    stale_token = state.active_token
                    stale = state.participants[state.active_index].session_id
                    with contextlib.suppress(Exception):
                        await send(stale, {"type": "interrupt"})
                    await asyncio.to_thread(
                        self.store.mutate,
                        forum_id,
                        lambda s, stale_token=stale_token: s.finish(
                            stale_token,
                            "",
                            time.time(),
                            "Previous turn interrupted during recovery",
                        ),
                        owner=owner,
                    )
                    continue
                token = uuid.uuid4().hex

                def choose(s: ForumState, token: str = token) -> None:
                    index = s.select(time.time())
                    if index is not None:
                        s.begin(index, token)

                state = await asyncio.to_thread(self.store.mutate, forum_id, choose, owner=owner)
                owns_lease = state is not None
                if state is None:
                    return
                if state.active_token == token:
                    assert state.active_index is not None
                    participant = state.participants[state.active_index]
                    active_session = participant.session_id
                    from omnigent.runtime.prompt import forum_turn_prompt

                    prompt = forum_turn_prompt(participant.name, state.posts, turn_token=token)
                    active = asyncio.create_task(
                        asyncio.wait_for(
                            self._turn(
                                active_session,
                                prompt,
                                send,
                                history,
                                native=participant.harness.endswith("-native"),
                                available=available,
                            ),
                            timeout=1800,
                        )
                    )
                await asyncio.sleep(0.5 if active else 2)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Forum worker retrying after an unexpected error")
            self._wake_again.add((current_workspace_id(), forum_id))
            await asyncio.sleep(2)
        finally:
            if active is not None:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
                if active_session and owns_lease:
                    with contextlib.suppress(Exception):
                        assert active_session is not None
                        await send(active_session, {"type": "interrupt"})
            await asyncio.to_thread(self.store.release, forum_id, owner)

    async def _turn(
        self,
        session_id: str,
        prompt: str,
        send: SessionEvent,
        history: History,
        *,
        native: bool = False,
        available: Availability | None = None,
    ) -> str:
        previous = {item["id"] for item in await history(session_id)}
        ready = asyncio.Event()
        completed = asyncio.get_running_loop().create_future()

        async def subscribed() -> list[dict[str, Any]]:
            ready.set()
            return []

        async def listen() -> None:
            busy = False
            try:
                async for event in session_stream.subscribe(session_id, on_subscribed=subscribed):
                    kind = event.get("type")
                    if kind == "session.status" and event.get("status") == "running":
                        busy = True
                    if kind == "session.status" and event.get("status") == "failed":
                        raise RuntimeError("Agent session failed")
                    if kind == "forum.turn.finished" and (busy or not native):
                        if event.get("status") != "idle":
                            raise RuntimeError("Native agent turn did not complete")
                        if not completed.done():
                            completed.set_result(None)
                        return
                    if kind == "response.completed" and not native:
                        if not completed.done():
                            completed.set_result(None)
                        return
                    if kind in {
                        "session.interrupted",
                        "response.failed",
                        "response.cancelled",
                        "response.incomplete",
                        "response.policy_denied",
                    }:
                        raise RuntimeError("Agent turn did not complete")
                raise RuntimeError("Agent stream disconnected")
            except Exception as exc:  # noqa: BLE001 — deliver stream failures to the turn waiter
                if not completed.done():
                    completed.set_exception(exc)

        listener = asyncio.create_task(listen())
        try:
            await ready.wait()
            result = await send(
                session_id,
                {
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                },
            )
            if isinstance(result, dict) and result.get("denied"):
                raise RuntimeError("Turn was denied")
            while not completed.done():
                await asyncio.wait({completed}, timeout=5)
                if (
                    not completed.done()
                    and available is not None
                    and not await available(session_id)
                ):
                    raise RuntimeError("Agent host disconnected")
            await completed
            items = await history(session_id)
            texts = []
            for item in items:
                data = item.get("data", item)
                if item["id"] not in previous and data.get("role") == "assistant":
                    if data.get("interrupted"):
                        raise RuntimeError("Agent response was interrupted")
                    texts.extend(
                        block.get("text", "")
                        for block in data.get("content", [])
                        if isinstance(block, dict)
                    )
            output = "\n\n".join(texts).strip()
            if not output:
                raise RuntimeError("Agent completed without a discussion response")
            return output
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
            if completed.done() and not completed.cancelled():
                completed.exception()
