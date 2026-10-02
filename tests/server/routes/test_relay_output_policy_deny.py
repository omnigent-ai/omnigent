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
import contextlib
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.entities import Conversation, ConversationItem
from omnigent.policies.types import PolicyAction, PolicyResult
from omnigent.server.routes._sessions.common import _llm_response_denied_turns
from omnigent.server.routes._sessions.helpers import _flush_relay_text
from omnigent.spec import AgentSpec
from omnigent.spec.types import GuardrailsSpec
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


class _FakeEngine:
    """Stand-in for a built ``PolicyEngine`` when the evaluator itself is faked."""


async def _prepared_engine(*args: Any, **kwargs: Any) -> _FakeEngine:
    del args, kwargs
    return _FakeEngine()


@contextlib.contextmanager
def _relay_policy_pipeline(evaluate: Any) -> Iterator[None]:
    """Fake the relay's RESPONSE-phase pipeline: a ready engine and a scripted evaluator."""
    with (
        patch("omnigent.runtime._globals._agent_store", object()),
        patch(
            "omnigent.server.routes._sessions.helpers._prepare_output_policy_engine",
            _prepared_engine,
        ),
        patch("omnigent.server.routes._sessions.helpers._evaluate_output_policy", evaluate),
    ):
        yield


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
        engine: Any = None,
    ) -> dict[str, Any]:
        captured["text"] = body.data["content"][0]["text"]
        return {"verdict": "deny", "reason": "output gated", "_denied_body": None}

    with _relay_policy_pipeline(_fake_output_policy):
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

    with _relay_policy_pipeline(_allow):
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
    calls = {"n": 0}

    async def _boom(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        raise RuntimeError("engine construction failed")

    with _relay_policy_pipeline(_boom):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_failopen_1",
            ["survives engine failure"],
            "resp_4",
            "test-agent",
            evaluate_response_phase=True,
        )

    assert _persisted_texts(store) == ["survives engine failure"]
    assert calls["n"] == 1, "a non-transient failure must not be retried"


# ── RESPONSE-phase evaluation under an upstream request-limit throttle ──────


class _ThrottledRpcError(Exception):
    """gRPC-shaped ``RESOURCE_EXHAUSTED`` error; grpcio is not a test dependency."""

    def code(self) -> Any:
        return SimpleNamespace(name="RESOURCE_EXHAUSTED")

    def __str__(self) -> str:
        return (
            "<_InactiveRpcError of RPC that terminated with:\n"
            "\tstatus = StatusCode.RESOURCE_EXHAUSTED\n"
            '\tdetails = "REQUEST_LIMIT_EXCEEDED: Workspace 1965859176160743 '
            'exceeded the concurrent limit of 60 requests."\n>'
        )


class _Http429Error(Exception):
    """HTTP-client-shaped rejection whose response carries a 429 status."""

    def __init__(self) -> None:
        super().__init__("Client error '429' for url 'https://gateway.example/evaluate'")
        self.response = SimpleNamespace(status_code=429)


_THROTTLE_ERRORS = [
    pytest.param(_ThrottledRpcError(), id="grpc-resource-exhausted"),
    pytest.param(_Http429Error(), id="http-429"),
    pytest.param(
        RuntimeError("REQUEST_LIMIT_EXCEEDED: workspace exceeded its concurrent limit"),
        id="request-limit-text",
    ),
]


@dataclass
class _ThrottledConversationStore(_FakeConversationStore):
    """Store whose first ``throttled_reads`` conversation lookups hit the request limit."""

    throttled_reads: int = 1
    reads: int = 0

    def get_conversation(self, conversation_id: str) -> Conversation:
        self.reads += 1
        if self.reads <= self.throttled_reads:
            raise _ThrottledRpcError()
        return super().get_conversation(conversation_id)


def _no_retry_pause() -> Any:
    """Drop the real retry pauses so a throttled evaluation retries immediately."""
    return patch(
        "omnigent.server.routes._sessions.helpers._RESPONSE_POLICY_RETRY_DELAYS_S",
        (0.0, 0.0),
        create=True,
    )


# Seams of the preparation stage, patched on the facade the helpers proxy through.
_LOADER_PATCH = "omnigent.server.routes.sessions._load_agent_spec_for_session"
_BUILDER_PATCH = "omnigent.server.routes.sessions._build_policy_engine_from_spec"
_GOVERNED_SPEC = AgentSpec(spec_version=1, name="test-agent", guardrails=GuardrailsSpec())


class _ScriptedEngine:
    """``PolicyEngine`` stand-in returning one scripted verdict."""

    def __init__(self, result: PolicyResult) -> None:
        self.result = result
        self.evaluations = 0

    async def evaluate(self, ctx: Any) -> PolicyResult:
        del ctx
        self.evaluations += 1
        return self.result

    def apply_label_writes(self, labels: dict[str, str]) -> None:
        del labels


def _deny_engine(reason: str) -> _ScriptedEngine:
    return _ScriptedEngine(PolicyResult(action=PolicyAction.DENY, reason=reason))


def _failing_then(
    exc: BaseException, value: Any, *, failures: int = 1
) -> tuple[Callable[..., Any], dict[str, int]]:
    """Return a callable raising *exc* for its first *failures* calls, then returning *value*."""
    calls = {"n": 0}

    def _call(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc
        return value

    return _call, calls


@contextlib.contextmanager
def _governed_session(
    build_engine: Callable[..., Any],
    load_spec: Callable[..., Any] | None = None,
) -> Iterator[None]:
    """
    Run the real RESPONSE-phase pipeline against a governed spec.

    Only the preparation seams are scripted: *load_spec* replaces the spec
    lookup and *build_engine* the engine build. The evaluation runs the real
    ``_evaluate_output_policy`` over the engine *build_engine* returns.
    """
    with (
        patch("omnigent.runtime._globals._agent_store", object()),
        patch(_LOADER_PATCH, load_spec or (lambda conv, agent_store: _GOVERNED_SPEC)),
        patch(_BUILDER_PATCH, build_engine),
        patch("omnigent.server.routes._sessions.helpers._publish_policy_deny"),
        _no_retry_pause(),
    ):
        yield


def _fail_open_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.ERROR
        and "persisting the text unmodified" in record.getMessage()
    ]


async def test_flush_response_phase_retries_throttled_conversation_read() -> None:
    """A throttled conversation lookup is retried, so the retry's DENY still gates the text."""
    store = _ThrottledConversationStore(throttled_reads=1)
    engine = _deny_engine("gated after throttle")

    with _governed_session(build_engine=lambda *args, **kwargs: engine):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_throttled_read_1",
            [_DENIED_TEXT],
            "resp_7",
            "test-agent",
            evaluate_response_phase=True,
        )

    texts = _persisted_texts(store)
    assert texts == ["[Denied by policy: gated after throttle]"], (
        f"a throttled lookup must not bypass the output policy, got {texts!r}"
    )
    assert store.reads == 2, "the throttled lookup must be retried"
    assert engine.evaluations == 1


@pytest.mark.parametrize("throttle", _THROTTLE_ERRORS)
async def test_flush_response_phase_retries_throttled_spec_load(throttle: Exception) -> None:
    """Every throttle shape on the spec lookup is retried; the retried DENY gates the text."""
    store = _FakeConversationStore()
    engine = _deny_engine("gated after throttle")
    load_spec, loads = _failing_then(throttle, _GOVERNED_SPEC)

    with _governed_session(build_engine=lambda *args, **kwargs: engine, load_spec=load_spec):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_throttled_spec_1",
            [_DENIED_TEXT],
            "resp_8",
            "test-agent",
            evaluate_response_phase=True,
        )

    texts = _persisted_texts(store)
    assert texts == ["[Denied by policy: gated after throttle]"], (
        f"a throttled spec lookup must not bypass the output policy, got {texts!r}"
    )
    assert loads["n"] == 2, "the throttled spec lookup must be retried"
    assert engine.evaluations == 1


async def test_flush_response_phase_retries_throttled_engine_build() -> None:
    """A throttled engine build is retried; the rebuilt engine's DENY gates the text."""
    store = _FakeConversationStore()
    engine = _deny_engine("gated after throttle")
    build_engine, builds = _failing_then(_ThrottledRpcError(), engine)

    with _governed_session(build_engine=build_engine):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_throttled_build_1",
            [_DENIED_TEXT],
            "resp_9",
            "test-agent",
            evaluate_response_phase=True,
        )

    assert _persisted_texts(store) == ["[Denied by policy: gated after throttle]"]
    assert builds["n"] == 2, "the throttled engine build must be retried"
    assert engine.evaluations == 1


async def test_flush_response_phase_persistent_throttle_fails_open_with_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A persistent throttle still fails open, and the fail-open log line names the cause."""
    store = _FakeConversationStore()
    load_spec, loads = _failing_then(_ThrottledRpcError(), _GOVERNED_SPEC, failures=3)

    caplog.set_level(logging.WARNING, logger="omnigent.server.routes.sessions")
    with _governed_session(
        build_engine=lambda *args, **kwargs: _deny_engine("unreached"), load_spec=load_spec
    ):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_throttled_persist_1",
            ["narration survives a long throttle"],
            "resp_10",
            "test-agent",
            evaluate_response_phase=True,
        )

    assert _persisted_texts(store) == ["narration survives a long throttle"]
    assert loads["n"] == 3, "one attempt per retry pause plus the initial attempt"
    failures = _fail_open_records(caplog)
    assert len(failures) == 1, f"expected one fail-open record, got {failures!r}"
    assert "after 3 attempt(s)" in failures[0], failures[0]
    assert "RESOURCE_EXHAUSTED" in failures[0] and "REQUEST_LIMIT_EXCEEDED" in failures[0], (
        f"the fail-open line must name the upstream throttle, got {failures[0]!r}"
    )


async def test_flush_response_phase_never_retries_a_partially_applied_evaluation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A throttle raised by the evaluation itself is not retried: the engine
    commits session-state updates before its label writes, so a repeat would
    double-apply an increment that already landed. The text still fails open
    and the log names the cause.
    """
    store = _FakeConversationStore()

    class _PartiallyCommittingEngine:
        """DENY commits a counter increment, then its trailing label write is throttled."""

        def __init__(self) -> None:
            self.committed_increments = 0

        async def evaluate(self, ctx: Any) -> PolicyResult:
            del ctx
            self.committed_increments += 1
            raise _ThrottledRpcError()

    engine = _PartiallyCommittingEngine()
    caplog.set_level(logging.WARNING, logger="omnigent.server.routes.sessions")
    with _governed_session(build_engine=lambda *args, **kwargs: engine):
        await _flush_relay_text(
            store,  # type: ignore[arg-type]
            "conv_partial_write_1",
            ["narration after a throttled label write"],
            "resp_11",
            "test-agent",
            evaluate_response_phase=True,
        )

    assert _persisted_texts(store) == ["narration after a throttled label write"]
    assert engine.committed_increments == 1, (
        "an evaluation that may already have written must not be replayed"
    )
    failures = _fail_open_records(caplog)
    assert len(failures) == 1, f"expected one fail-open record, got {failures!r}"
    assert "not retried" in failures[0] and "RESOURCE_EXHAUSTED" in failures[0], failures[0]


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

    def raise_for_status(self) -> None:
        """The scripted stream represents a successful HTTP response."""

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
        _relay_policy_pipeline(_deny_once_then_allow),
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
        _relay_policy_pipeline(_deny),
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
