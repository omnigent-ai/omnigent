"""
Tests for output-policy DENY enforcement at the runner relay's text flush.

Runner-relayed (scaffold) harnesses stream assistant text as id-less
``output_text.delta`` events; the relay buffers them and persists the
joined text at the terminal event. That flush is the single point where
the streamed text becomes a durable assistant message, so it is where an
output-policy DENY must substitute the ``[Denied by policy: ...]``
sentinel:

- A ``PHASE_LLM_RESPONSE`` DENY is computed on the server (the policy
  evaluate route) but only reaches the harness *after* the text already
  streamed — the route records the deny in
  ``_llm_response_denied_turns`` and the relay consumes it here.
- A ``Phase.RESPONSE`` policy is otherwise unreachable in the runner
  topology (nothing POSTs the assistant message back through
  ``POST .../events``), so the terminal flush evaluates it directly.

Production breakage these catch: the denied assistant text persisting
as a normal message (the "silently advisory output policies" bug) —
the policy returns DENY yet the user-visible transcript keeps the
denied content.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.entities import Conversation, ConversationItem
from omnigent.server.routes._sessions.common import _llm_response_denied_turns
from omnigent.server.routes._sessions.helpers import _flush_relay_text
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

pytestmark = pytest.mark.asyncio

_DENIED_TEXT = "TRIPWIRE-DENIED-ASSISTANT-OUTPUT"


# ── Fakes ────────────────────────────────────────────────────────────


@dataclass
class _FakeConversationStore:
    """Conversation store stub capturing appended items.

    :param agent_id: agent binding reported by ``get_conversation``.
    :param appended: items captured by ``append`` calls.
    """

    agent_id: str | None = "ag_test"
    appended: list[Any] = field(default_factory=list)

    def get_conversation(self, conversation_id: str) -> Conversation:
        return Conversation(
            id=conversation_id,
            created_at=1,
            updated_at=1,
            root_conversation_id=conversation_id,
            agent_id=self.agent_id,
        )

    def append(self, conversation_id: str, items: list[Any]) -> list[ConversationItem]:
        result = []
        for i, item in enumerate(items):
            self.appended.append(item)
            result.append(
                ConversationItem(
                    id=f"item_{i}",
                    type=item.type,
                    response_id=item.response_id,
                    data=item.data,
                    created_at=1,
                    status="completed",
                )
            )
        return result


def _persisted_texts(store: _FakeConversationStore) -> list[str]:
    """Flatten every appended message item's text blocks."""
    texts: list[str] = []
    for item in store.appended:
        data = item.data
        content = data.content if hasattr(data, "content") else []
        for block in content:
            text = getattr(block, "text", None) or (
                block.get("text") if isinstance(block, dict) else None
            )
            if isinstance(text, str):
                texts.append(text)
    return texts


# ── _flush_relay_text deny substitution ──────────────────────────────


async def test_flush_substitutes_sentinel_for_llm_response_deny() -> None:
    """
    A recorded LLM_RESPONSE DENY replaces the buffered text with the
    deny sentinel at persist time.

    While the bug is live, the denied text persists unmodified even
    though the policy returned DENY for the turn.
    """
    store = _FakeConversationStore()
    text_acc = [_DENIED_TEXT]

    await _flush_relay_text(
        store,  # type: ignore[arg-type]
        "conv_deny_1",
        text_acc,
        "resp_1",
        "test-agent",
        deny_reason="tripwire hit",
    )

    texts = _persisted_texts(store)
    assert texts == ["[Denied by policy: tripwire hit]"], (
        f"expected only the deny sentinel to persist, got {texts!r}"
    )
    assert not text_acc, "buffer must clear after a confirmed persist"


async def test_flush_evaluates_response_phase_at_terminal() -> None:
    """
    ``evaluate_response_phase=True`` gates the joined text through the
    spec's RESPONSE-phase output policies before persisting.

    This is the only place the runner topology can fire ``Phase.RESPONSE``
    (nothing POSTs the assistant message back through the events route),
    so reverting it re-opens the "response phase never fires" facet.
    """
    store = _FakeConversationStore()
    captured: dict[str, Any] = {}

    async def _fake_output_policy(
        session_id: str,
        conv: Conversation,
        body: Any,
        conversation_store: Any,
        agent_store: Any,
        runner_router: Any,
        *,
        actor: Any = None,
    ) -> dict[str, Any]:
        captured["text"] = body.data["content"][0]["text"]
        return {"verdict": "deny", "reason": "output gated", "_denied_body": None}

    with (
        patch(
            "omnigent.server.routes._sessions.helpers._evaluate_output_policy",
            _fake_output_policy,
        ),
        patch("omnigent.runtime._globals._agent_store", object()),
    ):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_deny_2",
            [_DENIED_TEXT],
            "resp_2",
            "test-agent",
            evaluate_response_phase=True,
        )

    assert captured["text"] == _DENIED_TEXT, "the policy must see the full joined text"
    texts = _persisted_texts(store)
    assert texts == ["[Denied by policy: output gated]"], (
        f"expected the deny sentinel, got {texts!r}"
    )


async def test_flush_response_phase_allow_persists_unmodified() -> None:
    """An ALLOW (no verdict) persists the text unchanged."""
    store = _FakeConversationStore()

    async def _allow(*args: Any, **kwargs: Any) -> None:
        return None

    with (
        patch(
            "omnigent.server.routes._sessions.helpers._evaluate_output_policy",
            _allow,
        ),
        patch("omnigent.runtime._globals._agent_store", object()),
    ):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_allow_1",
            ["plain assistant answer"],
            "resp_3",
            "test-agent",
            evaluate_response_phase=True,
        )

    assert _persisted_texts(store) == ["plain assistant answer"]


async def test_flush_response_phase_failure_fails_open() -> None:
    """
    A policy-engine crash during the RESPONSE evaluation must not destroy
    the narration — the text persists unmodified (output phases are
    advisory on evaluation error, matching the LLM phases' default).
    """
    store = _FakeConversationStore()

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("engine construction failed")

    with (
        patch(
            "omnigent.server.routes._sessions.helpers._evaluate_output_policy",
            _boom,
        ),
        patch("omnigent.runtime._globals._agent_store", object()),
    ):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_failopen_1",
            ["survives engine failure"],
            "resp_4",
            "test-agent",
            evaluate_response_phase=True,
        )

    assert _persisted_texts(store) == ["survives engine failure"]


# ── Full relay loop: deny marker consumed at the terminal flush ──────


class _ScriptedStreamResponse:
    """Async context manager yielding scripted SSE frames."""

    def __init__(self, release: asyncio.Event, events: list[dict[str, Any]]) -> None:
        self._release = release
        self._events = events

    async def __aenter__(self) -> _ScriptedStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> Any:
        import json as _json

        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._release.wait()
        for event in self._events:
            yield f"data: {_json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"


class _ScriptedRunnerClient:
    """Fake runner client replaying a scripted turn."""

    def __init__(self, release: asyncio.Event, events: list[dict[str, Any]]) -> None:
        self._release = release
        self._events = events

    def stream(self, method: str, path: str, *, timeout: Any) -> _ScriptedStreamResponse:
        del method, path, timeout
        return _ScriptedStreamResponse(self._release, self._events)


async def test_flush_persist_failure_leaves_buffer_for_retry() -> None:
    """
    An append failure during a denied flush keeps a retry buffer — and
    that buffer must already hold the SENTINEL, not the denied content,
    so no retry path can ever persist the original text.
    """

    @dataclass
    class _FailingStore(_FakeConversationStore):
        def append(self, conversation_id: str, items: list[Any]) -> list[ConversationItem]:
            raise RuntimeError("db write failed")

    store = _FailingStore()
    text_acc = [_DENIED_TEXT]

    with patch("omnigent.server.routes._sessions.helpers._publish_policy_deny"):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_retry_1",
            text_acc,
            "resp_retry",
            "test-agent",
            deny_reason="tripwire",
        )

    assert text_acc, "a failed persist must leave a buffer for retry"
    assert text_acc == ["[Denied by policy: tripwire]"], (
        "the retry buffer must carry the sentinel, never the denied text"
    )


async def test_response_phase_deny_survives_persist_failure_retry() -> None:
    """
    A RESPONSE-phase deny must not be re-evaluated from scratch on a
    persist-failure retry: a stateful policy whose labels moved on the
    first DENY can flip to ALLOW, which would leak the original denied
    text. The first flush commits the sentinel into the retry buffer, so
    the retry persists the sentinel even when the policy now allows.
    """
    call_count = 0

    async def _deny_once_then_allow(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"verdict": "deny", "reason": "stateful tripwire", "_denied_body": None}
        return None  # ALLOW on re-evaluation (labels moved on the first DENY)

    @dataclass
    class _FailOnceStore(_FakeConversationStore):
        fail_next: bool = True

        def append(self, conversation_id: str, items: list[Any]) -> list[ConversationItem]:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("db write failed")
            return super().append(conversation_id, items)

    store = _FailOnceStore()
    text_acc = [_DENIED_TEXT]

    with (
        patch(
            "omnigent.server.routes._sessions.helpers._evaluate_output_policy",
            _deny_once_then_allow,
        ),
        patch("omnigent.runtime._globals._agent_store", object()),
        patch("omnigent.server.routes._sessions.helpers._publish_policy_deny"),
    ):
        # First flush: DENY computed, persist fails — buffer must now
        # hold the sentinel.
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_stateful_retry",
            text_acc,
            "resp_retry_2",
            "test-agent",
            evaluate_response_phase=True,
        )
        assert text_acc == ["[Denied by policy: stateful tripwire]"]
        # Retry flush (same call shape the relay's later flush uses):
        # even though the policy would now ALLOW, the denied text is
        # gone — only the sentinel can persist.
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_stateful_retry",
            text_acc,
            "resp_retry_2",
            "test-agent",
            evaluate_response_phase=True,
        )

    texts = _persisted_texts(store)
    assert texts == ["[Denied by policy: stateful tripwire]"], (
        f"the original denied text must never persist on retry, got {texts!r}"
    )
    assert _DENIED_TEXT not in "".join(texts)


async def test_mid_turn_boundary_flush_gates_response_phase() -> None:
    """
    The tool-call-boundary flush must also gate the segment through the
    RESPONSE-phase policies: a model that emits the offending text BEFORE
    a tool call would otherwise persist it durably ahead of the terminal
    flush's evaluation (the policy-bypass path for multi-segment turns).
    """
    store = _FakeConversationStore()

    async def _deny(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"verdict": "deny", "reason": "gated segment", "_denied_body": None}

    with (
        patch(
            "omnigent.server.routes._sessions.helpers._evaluate_output_policy",
            _deny,
        ),
        patch("omnigent.runtime._globals._agent_store", object()),
        patch("omnigent.server.routes._sessions.helpers._publish_policy_deny"),
    ):
        # Same call shape the relay's function_call-boundary flush uses.
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_boundary_1",
            [_DENIED_TEXT],
            "resp_5",
            "test-agent",
            evaluate_response_phase=True,
        )

    assert _persisted_texts(store) == ["[Denied by policy: gated segment]"]


async def test_relay_consumes_deny_marker_and_persists_sentinel(db_uri: str) -> None:
    """
    End-to-end through the real relay loop: a session with a recorded
    LLM_RESPONSE DENY persists the deny sentinel — never the streamed
    denied text — and the marker is consumed so it cannot bleed into a
    later turn.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    sessions_module._runner_relay_tasks.clear()
    store = SqlAlchemyConversationStore(db_uri)
    # agent_id=None: the marker path needs no spec; the RESPONSE-phase
    # evaluation (which would need an agent row) short-circuits on it.
    conv = store.create_conversation()
    session_id = conv.id

    response_id = "resp_denied_turn"
    turn_events: list[dict[str, Any]] = [
        {"type": "response.in_progress", "response": {"id": response_id, "model": "debby"}},
        {"type": "response.output_text.delta", "delta": _DENIED_TEXT},
        {
            "type": "response.failed",
            "response": {
                "id": response_id,
                "model": "debby",
                "error": {
                    "code": "RuntimeError",
                    "message": "inner executor error: LLM response denied by policy: tripwire",
                },
            },
        },
    ]
    release = asyncio.Event()
    fake_runner = _ScriptedRunnerClient(release, turn_events)
    # The policy-evaluate route records the DENY before its verdict even
    # returns to the harness, so it always precedes the terminal event.
    _llm_response_denied_turns[session_id] = "tripwire"

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_deny_marker",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,
        )
        assert handle is not None
        release.set()
        await asyncio.wait_for(handle.task, timeout=5.0)

        items = store.list_items(session_id).data
        messages = [item for item in items if item.type == "message"]
        assert len(messages) == 1, f"expected one persisted message, got {items}"
        content = messages[0].to_api_dict()["content"]
        assert content == [{"type": "output_text", "text": "[Denied by policy: tripwire]"}], (
            f"denied text leaked into the durable transcript: {content!r}"
        )
        assert session_id not in _llm_response_denied_turns, (
            "the deny marker must be consumed at the terminal flush"
        )
    finally:
        release.set()
        _llm_response_denied_turns.pop(session_id, None)
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None:
            await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        session_stream.close(session_id)


async def test_relay_teardown_clears_stranded_deny_marker(db_uri: str) -> None:
    """
    A relay that dies before its terminal flush (runner drop, cancel)
    must not strand its deny marker: the marker dict is unbounded by
    design (an enforcement decision must never be silently evicted), so
    its leak-safety rides the relay's done-callback cleanup.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    sessions_module._runner_relay_tasks.clear()
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    session_id = conv.id

    release = asyncio.Event()
    fake_runner = _ScriptedRunnerClient(release, [])  # no terminal event needed

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_teardown",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,
        )
        assert handle is not None
        _llm_response_denied_turns[session_id] = "tripwire"
        handle.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handle.task, timeout=2.0)
        # Done-callbacks run soon after task completion.
        await asyncio.sleep(0)
        assert session_id not in _llm_response_denied_turns, (
            "a dead relay must not strand its deny marker"
        )
    finally:
        release.set()
        _llm_response_denied_turns.pop(session_id, None)
        sessions_module._runner_relay_tasks.clear()
        session_stream.close(session_id)
