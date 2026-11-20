"""Recovery records for answers on a stopped session: pruning, races and the runner check."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_elicitations
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes._sessions import orchestration
from tests._helpers.polling import poll_until_async


def _record(started_at: float, *, delivering: bool) -> orchestration._StoppedSessionAnswer:
    record = orchestration._StoppedSessionAnswer(
        started_at=started_at, fingerprint="old", runner_id=None
    )
    if delivering:
        record.delivery = asyncio.get_running_loop().create_future()
    return record


async def test_the_prune_keeps_running_and_recent_records() -> None:
    old = time.time() - orchestration._CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S - 1
    records = {
        ("session", "old_done"): _record(old, delivering=False),
        ("session", "old_running"): _record(old, delivering=True),
        ("session", "recent"): _record(time.time(), delivering=False),
    }
    for key, record in records.items():
        orchestration._stopped_session_answers[key] = record
    try:
        orchestration._prune_stopped_session_answers()

        kept = {
            key for key in records if orchestration._stopped_session_answers.get(key) is not None
        }
        assert kept == {("session", "old_running"), ("session", "recent")}
    finally:
        for key in records:
            orchestration._stopped_session_answers.pop(key, None)


async def test_an_answer_prunes_old_records() -> None:
    old = time.time() - orchestration._CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S - 1
    key = ("session", "old_done")
    orchestration._stopped_session_answers[key] = _record(old, delivering=False)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    conv = Conversation(id="other", created_at=0, updated_at=0, root_conversation_id="other")

    async def _post_event(*args: object, **kwargs: object) -> dict[str, bool]:
        raise AssertionError("an out-of-scope answer must not be delivered")

    try:
        delivered = await orchestration._deliver_answer_to_stopped_session(
            request,
            "other",
            conv,
            {"elicitation_id": "elicit_x", "action": "decline"},
            None,
            _post_event,
            conversation_store=None,
            runner_exit_reports=None,
        )

        assert delivered is None
        assert orchestration._stopped_session_answers.get(key) is None
    finally:
        orchestration._stopped_session_answers.pop(key, None)


async def test_a_reused_id_does_not_drop_a_running_delivery() -> None:
    session_id, elicitation_id = "conv_reused", "elicit_claude_" + "d" * 32
    pending_elicitations.record_publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": elicitation_id,
            "params": {"mode": "form", "message": "a different question"},
        },
    )
    record = _record(time.time(), delivering=True)
    key = (session_id, elicitation_id)
    orchestration._stopped_session_answers[key] = record
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    conv = Conversation(id=session_id, created_at=0, updated_at=0, root_conversation_id=session_id)

    async def _post_event(*args: object, **kwargs: object) -> dict[str, bool]:
        raise AssertionError("a request that waits must not start its own delivery")

    try:
        answer = asyncio.create_task(
            orchestration._deliver_answer_to_stopped_session(
                request,
                session_id,
                conv,
                {"elicitation_id": elicitation_id, "action": "accept", "content": {}},
                None,
                _post_event,
                conversation_store=None,
                runner_exit_reports=None,
            )
        )
        await asyncio.sleep(0)
        assert orchestration._stopped_session_answers.get(key) is record
        record.delivery.set_result({"queued": True})
        assert await asyncio.wait_for(answer, 5.0) == {"queued": True}
    finally:
        orchestration._stopped_session_answers.pop(key, None)
        pending_elicitations.resolve(session_id, elicitation_id)


_QUESTION = "Which database?"


def _question_params() -> dict[str, object]:
    return {
        "mode": "form",
        "message": "Claude wants to call **AskUserQuestion**",
        "phase": "pre_tool_use",
        "policy_name": "claude_native_permission",
        "content_preview": "AskUserQuestion(...)",
        "ask_user_question": {"questions": [{"question": _QUESTION, "options": []}]},
    }


class _Question:
    """One parked Claude question on a hostless native session, answered at unit level."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, session_id: str) -> None:
        self.session_id = session_id
        self.elicitation_id = "elicit_claude_" + session_id[-8:].rjust(32, "0")
        self.conv = Conversation(
            id=session_id, created_at=0, updated_at=0, root_conversation_id=session_id
        )
        self.request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        self.posts: list[object] = []
        self.resolved: list[str] = []
        real_resolved = orchestration._publish_elicitation_resolved

        def _record_resolved(session: str, elicitation: str, **kwargs: Any) -> None:
            self.resolved.append(elicitation)
            real_resolved(session, elicitation, **kwargs)

        async def _no_runner(*args: object, **kwargs: object) -> None:
            del args, kwargs

        monkeypatch.setattr(orchestration, "_is_native_terminal_session", lambda conv: True)
        monkeypatch.setattr(orchestration, "_get_runner_client", _no_runner)
        monkeypatch.setattr(orchestration, "_publish_elicitation_resolved", _record_resolved)
        pending_elicitations.record_publish(
            session_id,
            {
                "type": "response.elicitation_request",
                "elicitation_id": self.elicitation_id,
                "params": _question_params(),
            },
        )

    @property
    def key(self) -> tuple[str, str]:
        return (self.session_id, self.elicitation_id)

    def pending(self) -> bool:
        return (
            pending_elicitations.lookup(self.elicitation_id, conversation_id=self.session_id)
            is not None
        )

    def answer(self, post_event: Callable[..., Awaitable[Any]]) -> asyncio.Task[Any]:
        return asyncio.create_task(
            orchestration._deliver_answer_to_stopped_session(
                self.request,
                self.session_id,
                self.conv,
                {
                    "elicitation_id": self.elicitation_id,
                    "action": "accept",
                    "content": {_QUESTION: "sqlite"},
                },
                None,
                post_event,
                conversation_store=None,
                runner_exit_reports=None,
            )
        )

    def cleanup(self) -> None:
        orchestration._stopped_session_answers.pop(self.key, None)
        pending_elicitations.resolve(self.session_id, self.elicitation_id)


@pytest.fixture
def question(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[_Question]:
    parked = _Question(monkeypatch, "conv_" + request.node.name[-24:])
    try:
        yield parked
    finally:
        parked.cleanup()


def _gated_post_event(
    question: _Question, results: list[object]
) -> tuple[Callable[..., Awaitable[Any]], asyncio.Event, asyncio.Event]:
    """A message path that records each call, waits for a release, then returns or raises."""
    reached, release = asyncio.Event(), asyncio.Event()

    async def _post_event(*args: object, **kwargs: object) -> object:
        del args, kwargs
        question.posts.append(object())
        reached.set()
        await release.wait()
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    return _post_event, reached, release


async def test_a_denied_answer_keeps_the_question_open(question: _Question) -> None:
    """A denial fails the request and keeps the card open; a later answer is delivered."""
    denial = {"queued": False, "denied": True, "reason": "blocked"}
    post_event, _reached, release = _gated_post_event(question, [denial, {"queued": True}])
    release.set()

    with pytest.raises(OmnigentError) as denied:
        await question.answer(post_event)
    assert denied.value.code == ErrorCode.FORBIDDEN
    assert "blocked" in str(denied.value)
    assert question.pending()
    assert question.resolved == []
    record = orchestration._stopped_session_answers.get(question.key)
    assert record is not None and record.acknowledgement is None

    assert await question.answer(post_event) == {"queued": True}
    assert len(question.posts) == 2
    assert not question.pending()
    assert question.resolved == [question.elicitation_id]


@pytest.mark.parametrize("case", ["delivered", "leader_fails"])
async def test_two_answers_at_once_deliver_one_message(question: _Question, case: str) -> None:
    """A second answer waits for the first and gets its result: one message, or the same error."""
    error = OmnigentError("host offline", code=ErrorCode.RUNNER_UNAVAILABLE)
    result: object = error if case == "leader_fails" else {"queued": True}
    post_event, reached, release = _gated_post_event(question, [result])
    first = question.answer(post_event)
    await asyncio.wait_for(reached.wait(), 5.0)
    second = question.answer(post_event)
    await asyncio.sleep(0)
    assert not second.done(), "the second answer must wait for the first one's result"
    release.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5.0)

    assert len(question.posts) == 1
    if case == "leader_fails":
        assert outcomes == [error, error]
        assert question.pending()
        return
    assert outcomes == [{"queued": True}, {"queued": True}]
    assert not question.pending()


async def test_two_answers_in_the_same_moment_deliver_one_message(
    question: _Question, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two answers that both pass the runner check before either starts still send one message."""
    release = asyncio.Event()
    waiting = 0

    async def _gated_no_runner(*args: object, **kwargs: object) -> None:
        nonlocal waiting
        del args, kwargs
        waiting += 1
        await release.wait()

    monkeypatch.setattr(orchestration, "_get_runner_client", _gated_no_runner)
    post_event, _reached, post_release = _gated_post_event(question, [{"queued": True}])
    post_release.set()
    answers = [question.answer(post_event) for _ in range(2)]
    await poll_until_async(lambda: waiting == 2, timeout_s=5.0, interval_s=0.01)
    release.set()

    assert await asyncio.wait_for(asyncio.gather(*answers), 5.0) == [{"queued": True}] * 2
    assert len(question.posts) == 1


async def test_a_cancelled_answer_hands_the_waiting_answer_an_error(question: _Question) -> None:
    """A dropped first request hands a waiting answer an error, and a retry still delivers."""
    post_event, reached, release = _gated_post_event(question, [{"queued": True}])
    first = question.answer(post_event)
    await asyncio.wait_for(reached.wait(), 5.0)
    second = question.answer(post_event)
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    with pytest.raises(OmnigentError) as follower_error:
        await asyncio.wait_for(second, 5.0)
    assert follower_error.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert question.pending()

    release.set()
    assert await asyncio.wait_for(question.answer(post_event), 5.0) == {"queued": True}
    assert len(question.posts) == 2
    assert not question.pending()


async def test_a_card_close_error_does_not_strand_the_answer(
    question: _Question, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed card close after delivery sends no second message, and a retry closes the card."""
    real_close = orchestration._close_answered_card
    calls = 0

    def _close_fails_once(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OmnigentError("close failed", code=ErrorCode.INTERNAL_ERROR)
        real_close(*args, **kwargs)

    monkeypatch.setattr(orchestration, "_close_answered_card", _close_fails_once)
    post_event, reached, release = _gated_post_event(question, [{"queued": True}])
    first = question.answer(post_event)
    await asyncio.wait_for(reached.wait(), 5.0)
    second = question.answer(post_event)
    await asyncio.sleep(0)
    release.set()
    leader, follower = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), 5.0
    )

    assert isinstance(leader, OmnigentError)
    assert follower == {"queued": True}
    assert await question.answer(post_event) == follower
    assert len(question.posts) == 1
    assert not question.pending()


async def _gate_runner_check(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event]:
    reached, release = asyncio.Event(), asyncio.Event()

    async def _gated(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        reached.set()
        await release.wait()
        return False

    monkeypatch.setattr(orchestration, "_session_runner_reachable", _gated)
    return reached, release


@pytest.mark.parametrize("other", ["finished", "delivering"])
async def test_a_retry_that_waits_takes_the_other_delivery(
    question: _Question, monkeypatch: pytest.MonkeyPatch, other: str
) -> None:
    """A retry whose runner check outlasts another delivery gets that delivery's ack."""
    record = orchestration._StoppedSessionAnswer(
        started_at=time.time(),
        fingerprint=orchestration._harness_elicitation_fingerprint_or_none(_question_params()),
        runner_id=None,
    )
    orchestration._stopped_session_answers[question.key] = record
    reached, release = await _gate_runner_check(monkeypatch)
    post_event, _reached, _release = _gated_post_event(question, [])
    retry = question.answer(post_event)
    await asyncio.wait_for(reached.wait(), 5.0)
    ack = {"queued": True, "item_id": "item_other"}
    if other == "finished":
        record.acknowledgement = ack
        release.set()
    else:
        delivery = asyncio.get_running_loop().create_future()
        record.delivery = delivery
        release.set()
        await asyncio.sleep(0)
        assert not retry.done(), "the retry must wait for the delivery that started meanwhile"
        delivery.set_result(ack)

    assert await asyncio.wait_for(retry, 5.0) == ack
    assert question.posts == []


@pytest.mark.parametrize("change", ["resolved", "asked_again"])
async def test_an_answer_whose_question_changes_during_the_runner_check_is_not_sent(
    question: _Question, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A question resolved or asked again during the runner check is not sent as a message."""
    reached, release = await _gate_runner_check(monkeypatch)
    post_event, _reached, _release = _gated_post_event(question, [])
    answer = question.answer(post_event)
    await asyncio.wait_for(reached.wait(), 5.0)
    if change == "resolved":
        pending_elicitations.resolve(question.session_id, question.elicitation_id)
    else:
        pending_elicitations.record_publish(
            question.session_id,
            {
                "type": "response.elicitation_request",
                "elicitation_id": question.elicitation_id,
                "params": {**_question_params(), "message": "a different question"},
            },
        )
    release.set()

    assert await asyncio.wait_for(answer, 5.0) is None
    assert question.posts == []
    assert orchestration._stopped_session_answers.get(question.key) is None


class _Rows:
    """A conversation store that serves one row and counts the reads."""

    def __init__(self, conv: Conversation) -> None:
        self.row: Conversation | None = conv
        self.error: Exception | None = None
        self.reads = 0

    def get_conversation(self, session_id: str) -> Conversation | None:
        del session_id
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.row


def _bound_conv(**fields: Any) -> Conversation:
    values: dict[str, Any] = {"host_id": "host_bound", "runner_id": "runner_bound", **fields}
    return Conversation(
        id="conv_bound",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_bound",
        **values,
    )


def _check_request(
    monkeypatch: pytest.MonkeyPatch, wait: Callable[..., Awaitable[object]]
) -> Request:
    """Stage a runner check that misses here, has no host connection, and runs *wait*."""
    from omnigent.server.routes import sessions as sessions_module

    async def _no_runner(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def _not_offline(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(orchestration, "_get_runner_client", _no_runner)
    monkeypatch.setattr(orchestration, "_session_offline_host_id", _not_offline)
    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 30.0)
    monkeypatch.setattr(sessions_module, "_wait_for_runner_client", wait)
    app = SimpleNamespace(state=SimpleNamespace(host_registry=None, tunnel_registry=None))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app})


@pytest.mark.parametrize("own_stamp", [False, True])
async def test_a_runner_live_on_another_replica_skips_the_wait(
    monkeypatch: pytest.MonkeyPatch, own_stamp: bool
) -> None:
    """A stamp newer than this process wrote means a sibling holds the runner; its own does not."""
    from omnigent.server import session_live_state

    stamp = int(time.time())
    conv = _bound_conv(runner_last_seen=stamp)
    if own_stamp:
        monkeypatch.setitem(session_live_state._last_liveness_stamp, "runner_bound", stamp)
    waits: list[object] = []

    async def _wait(*args: object, **kwargs: object) -> None:
        del args, kwargs
        waits.append(True)

    request = _check_request(monkeypatch, _wait)
    rows = _Rows(_bound_conv())
    reachable = await orchestration._session_runner_reachable(
        request, conv.id, conv, None, cast("Any", rows), RunnerExitReports()
    )

    assert reachable is (not own_stamp)
    assert waits == ([True] if own_stamp else [])


@pytest.mark.parametrize("row", ["moved", "gone", "read_error"])
async def test_a_runner_that_moves_during_the_wait_takes_the_answer(
    monkeypatch: pytest.MonkeyPatch, row: str
) -> None:
    """The check after the wait sees a runner that re-tunnelled meanwhile; a lost row is a miss."""
    conv = _bound_conv()
    rows = _Rows(conv)

    async def _wait(*args: object, **kwargs: object) -> None:
        del args, kwargs
        if row == "moved":
            rows.row = _bound_conv(runner_last_seen=int(time.time()))
        elif row == "gone":
            rows.row = None
        else:
            rows.error = RuntimeError("store down")

    request = _check_request(monkeypatch, _wait)
    reachable = await orchestration._session_runner_reachable(
        request, conv.id, conv, None, cast("Any", rows), RunnerExitReports()
    )

    assert reachable is (row == "moved")
    assert rows.reads == 1


@pytest.mark.parametrize("case", ["still_bound", "rebound", "wrong_replica"])
async def test_an_admission_error_after_the_runner_connects_keeps_the_hook(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Admission fails only after the tunnel connected; other errors, or a rebound row, raise."""
    conv = _bound_conv()
    rows = _Rows(conv if case != "rebound" else _bound_conv(runner_id="runner_new"))
    code = (
        ErrorCode.WRONG_REPLICA
        if case == "wrong_replica"
        else ErrorCode.RUNNER_CAPABILITY_MISMATCH
    )
    error = OmnigentError("raised by the wait", code=code)

    async def _wait(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise error

    request = _check_request(monkeypatch, _wait)
    check = orchestration._session_runner_reachable(
        request, conv.id, conv, None, cast("Any", rows), RunnerExitReports()
    )

    if case == "still_bound":
        assert await check is True
    else:
        with pytest.raises(OmnigentError) as raised:
            await check
        assert raised.value is error


async def test_a_reported_exit_skips_the_offline_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reported runner exit skips the host lookup, as the message path does."""
    from omnigent.server.routes import sessions as sessions_module

    reports = RunnerExitReports()
    reports.record("runner_gone", "exited", None)
    conv = Conversation(
        id="conv_exit",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_exit",
        host_id="host_exit",
        runner_id="runner_gone",
    )
    app = SimpleNamespace(state=SimpleNamespace(host_registry=None, tunnel_registry=None))
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app})
    waits: list[object] = []

    async def _no_runner(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def _offline_lookup(*args: object, **kwargs: object) -> str:
        raise AssertionError("a reported exit must skip the offline lookup")

    async def _wait(*args: object, **kwargs: object) -> None:
        del args
        waits.append(kwargs.get("runner_exit_reports"))

    monkeypatch.setattr(orchestration, "_get_runner_client", _no_runner)
    monkeypatch.setattr(orchestration, "_session_offline_host_id", _offline_lookup)
    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 30.0)
    monkeypatch.setattr(sessions_module, "_wait_for_runner_client", _wait)

    rows = _Rows(conv)
    reachable = await orchestration._session_runner_reachable(
        request, conv.id, conv, None, cast("Any", rows), reports
    )

    assert reachable is False
    assert waits == [reports]
    assert rows.reads == 0


async def test_an_offline_host_skips_the_runner_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """An offline host cannot bring the runner back, so the check does not wait."""
    from omnigent.server.routes import sessions as sessions_module

    conv = Conversation(
        id="conv_offline",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_offline",
        host_id="host_offline",
        runner_id="runner_offline",
    )
    app = SimpleNamespace(state=SimpleNamespace(host_registry=None, tunnel_registry=None))
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app})

    async def _no_runner(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def _offline(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "host_offline"

    async def _wait(*args: object, **kwargs: object) -> None:
        raise AssertionError("an offline host must skip the runner wait")

    monkeypatch.setattr(orchestration, "_get_runner_client", _no_runner)
    monkeypatch.setattr(orchestration, "_session_offline_host_id", _offline)
    monkeypatch.setattr(sessions_module, "_HOST_BOUND_RUNNER_CONNECT_GRACE_S", 30.0)
    monkeypatch.setattr(sessions_module, "_wait_for_runner_client", _wait)

    rows = _Rows(conv)
    reachable = await orchestration._session_runner_reachable(
        request, conv.id, conv, None, cast("Any", rows), RunnerExitReports()
    )

    assert reachable is False
    assert rows.reads == 0
