"""How the REPL's sessions adapter asks and settles elicitations in the terminal.

Questions (the ``ask_user_question`` extra) go to the host's inline selector;
other prompts use the y/a/n approval and schema-field prompts. One prompt is
shown at a time, prompts settled by another client never (re)open, and prompts
raised before the stream subscribed are replayed from the session snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
from omnigent_client import StreamHooks

from omnigent.repl._repl import (
    _FieldInputState,
    _question_prompts,
    _SessionsChatReplAdapter,
)
from tests._helpers.polling import poll_until_async


class _FakeFmt:
    accent = "cyan"
    muted = "grey50"
    warning = "yellow"


class _SelectorHost:
    """Terminal host stand-in whose question selector the test answers."""

    def __init__(self) -> None:
        self.outputs: list[Any] = []
        self.asked: list[list[Any]] = []
        self._pending: asyncio.Future[Any] | None = None

    def output(self, renderable: Any) -> None:
        self.outputs.append(renderable)

    async def ask_questions(self, questions: list[Any]) -> Any:
        self.asked.append(questions)
        self._pending = asyncio.get_running_loop().create_future()
        try:
            return await self._pending
        finally:
            self._pending = None

    def dismiss_questions(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(None)

    @property
    def selector_open(self) -> bool:
        return self._pending is not None and not self._pending.done()

    def answer(self, answers: Any) -> None:
        assert self._pending is not None, "no question is open"
        self._pending.set_result(answers)

    def text(self) -> str:
        return "\n".join(getattr(o, "plain", str(o)) for o in self.outputs)


def _adapter() -> tuple[_SessionsChatReplAdapter, _SelectorHost, _FieldInputState, Any, Any]:
    """Real sessions adapter with a mocked client and a y/a/n hook that accepts."""
    client = MagicMock()
    client.sessions.resolve_elicitation = AsyncMock(return_value=None)
    approval_hook = AsyncMock(return_value=True)
    host = _SelectorHost()
    fis = _FieldInputState()
    adapter = _SessionsChatReplAdapter(
        client=client,
        agent_name="test-agent",
        hooks=StreamHooks(on_elicitation_request=approval_hook),
        session_id="conv_abc",
        field_input_state=fis,
        host=host,  # type: ignore[arg-type]
        fmt=_FakeFmt(),  # type: ignore[arg-type]
    )
    return adapter, host, fis, client, approval_hook


def _question_event(elicitation_id: str = "elicit_q", questions: Any = None) -> Any:
    return SimpleNamespace(
        elicitation_id=elicitation_id,
        message="Claude wants to use **AskUserQuestion**",
        requested_schema={},
        mode="form",
        phase="pre_tool_use",
        policy_name="claude_sdk_permission",
        content_preview="",
        url=None,
        ask_user_question={
            "questions": questions or [{"question": "Env?", "options": ["dev", "prod"]}]
        },
    )


def _form_event(elicitation_id: str, schema: dict[str, Any]) -> Any:
    event = _question_event(elicitation_id)
    event.requested_schema = schema
    event.phase = "tool_call"
    del event.ask_user_question
    return event


def _posted(client: Any) -> list[tuple[Any, ...]]:
    return [c.args for c in client.sessions.resolve_elicitation.await_args_list]


async def _until(condition: Any, message: str) -> None:
    """Yield to the loop until *condition()* holds; fail with *message* rather than hang."""
    try:
        await poll_until_async(condition, timeout_s=2.0, interval_s=0)
    except AssertionError:
        raise AssertionError(message) from None


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Question parsing ──────────────────────────────────────────


def test_question_prompts_follow_web_form_rules() -> None:
    questions = _question_prompts(
        {
            "questions": [
                {"question": "Env?", "options": [{"label": "dev", "description": "d"}, "prod"]},
                {"id": "0", "question": "Pick", "options": ["a"], "isOther": False},
                {"question": "Name?", "options": []},
                {"id": "1", "question": "Notes?", "options": [], "textInput": {"default": "x"}},
            ]
        }
    )
    assert [(q.key, q.options, q.allow_custom, q.text_default) for q in questions] == [
        ("Env?", (("dev", "d"), ("prod", "")), True, None),
        ("0", (("a", ""),), False, None),
        ("Name?", (), True, None),
        ("1", (), True, "x"),
    ]


def test_one_unanswerable_question_rejects_the_batch() -> None:
    # Answering the rest would post an accept with an answer missing.
    valid = {"question": "Env?", "options": ["dev"]}
    for unusable in (
        {"question": "Q?", "options": 1},
        {"question": "Q?", "options": [], "isOther": False},
        {"question": "", "options": ["x"]},
        "not-a-question",
    ):
        assert _question_prompts({"questions": [valid, unusable]}) == [], unusable
    assert _question_prompts({"questions": "nope"}) == []
    assert _question_prompts(None) == []


# ── Questions through the selector ────────────────────────────


def test_question_posts_the_selector_answers_without_the_approval_prompt() -> None:
    adapter, host, _, client, approval_hook = _adapter()

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: host.selector_open, "the selector never opened")
        host.answer({"Env?": "prod"})
        await task

    _run(_run_case())
    assert [q.question for q in host.asked[0]] == ["Env?"]
    assert _posted(client) == [
        ("conv_abc", "elicit_q", {"action": "accept", "content": {"Env?": "prod"}})
    ]
    # y/a/n would accept with no answers, and "approve always" would cache it.
    approval_hook.assert_not_awaited()


def test_dismissing_the_selector_posts_a_decline() -> None:
    adapter, host, _, client, _ = _adapter()

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: host.selector_open, "the selector never opened")
        host.dismiss_questions()  # what Esc does
        await task

    _run(_run_case())
    assert _posted(client) == [("conv_abc", "elicit_q", {"action": "decline"})]


def test_question_answered_elsewhere_closes_the_selector_without_posting() -> None:
    adapter, host, _, client, _ = _adapter()

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: host.selector_open, "the selector never opened")
        adapter.note_elicitation_resolved("elicit_q")  # the resolved event's handler
        await _until(lambda: task.done(), "the selector stayed open")

    _run(_run_case())
    client.sessions.resolve_elicitation.assert_not_awaited()


def test_secret_questions_are_left_for_another_client() -> None:
    # Typed terminal input is visible on screen, so a producer-marked secret is
    # never collected here and the elicitation stays pending.
    adapter, host, _, client, approval_hook = _adapter()
    event = _question_event(questions=[{"question": "Token?", "options": [], "isSecret": True}])

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", event))
        await _until(lambda: task.done(), "a selector opened for a secret question")

    _run(_run_case())
    assert host.asked == []
    approval_hook.assert_not_awaited()
    client.sessions.resolve_elicitation.assert_not_awaited()
    assert "Answer it in the web UI" in host.text()


def test_unusable_questions_decline_without_any_prompt() -> None:
    for questions in (
        [{"question": "Q?", "options": 1}],
        [
            {"question": "Env?", "options": ["dev"]},
            {"question": "Q?", "options": [], "isOther": False},
        ],
    ):
        adapter, host, _, client, approval_hook = _adapter()

        async def _run_case(adapter: Any = adapter, questions: Any = questions) -> None:
            task = asyncio.create_task(
                adapter._handle_elicitation("conv_abc", _question_event(questions=questions))
            )
            await _until(lambda: task.done(), "an unusable question opened a prompt")

        _run(_run_case())
        assert host.asked == []
        approval_hook.assert_not_awaited()
        assert _posted(client) == [("conv_abc", "elicit_q", {"action": "decline"})]


# ── One prompt at a time, never a settled one ─────────────────


def test_concurrent_prompts_are_asked_one_at_a_time() -> None:
    adapter, host, _, client, _ = _adapter()

    async def _run_case() -> None:
        first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        second = asyncio.create_task(
            adapter._handle_elicitation("conv_abc", _question_event("elicit_second"))
        )
        await _until(lambda: host.selector_open, "the first question never opened")
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(host.asked) == 1, "the second question opened over the first"
        host.answer({"Env?": "dev"})
        await _until(lambda: first.done() and host.selector_open, "the second never opened")
        host.answer({"Env?": "prod"})
        await second

    _run(_run_case())
    assert [args[1] for args in _posted(client)] == ["elicit_q", "elicit_second"]


def test_prompt_settled_before_its_turn_is_never_shown() -> None:
    # A prompt queued behind another one, or a stale snapshot replay, must not
    # open after another client settled it.
    adapter, host, _, client, _ = _adapter()
    queued = _question_event("elicit_queued")

    async def _run_case() -> None:
        first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: host.selector_open, "the first question never opened")
        second = asyncio.create_task(adapter._handle_elicitation("conv_abc", queued))
        adapter.note_elicitation_resolved("elicit_queued")
        host.answer({"Env?": "dev"})
        await _until(lambda: second.done(), "the settled prompt opened anyway")
        await first
        replay = asyncio.create_task(adapter._handle_elicitation("conv_abc", queued))
        await _until(lambda: replay.done(), "the settled prompt reopened from replay")

    _run(_run_case())
    assert len(host.asked) == 1
    assert [args[1] for args in _posted(client)] == ["elicit_q"]


def test_a_prompt_seen_live_and_in_replay_is_handled_once() -> None:
    adapter, host, _, client, _ = _adapter()

    async def _run_case() -> None:
        first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: host.selector_open, "the question never opened")
        duplicate = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: duplicate.done(), "the duplicate opened a second prompt")
        host.answer({"Env?": "dev"})
        await first

    _run(_run_case())
    assert len(host.asked) == 1
    assert len(_posted(client)) == 1


def test_undelivered_answer_can_be_retried_from_a_later_replay() -> None:
    adapter, host, _, client, _ = _adapter()
    client.sessions.resolve_elicitation = AsyncMock(
        side_effect=[httpx.ConnectError("down")] * 3 + [None]
    )

    async def _run_case() -> None:
        for _ in range(2):  # live delivery, then the reconnect replay
            task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
            await _until(lambda: host.selector_open, "the question did not reopen")
            host.answer({"Env?": "dev"})
            await task

    _run(_run_case())
    assert client.sessions.resolve_elicitation.await_count == 4


# ── Schema forms settled by another client ────────────────────


def test_schema_form_answered_elsewhere_stops_without_posting() -> None:
    adapter, _, fis, client, approval_hook = _adapter()
    form = _form_event("elicit_form", {"properties": {"name": {"type": "string"}}})

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", form))
        await _until(lambda: fis.pending, "the field prompt never opened")
        assert fis.resolve_elsewhere("elicit_form")
        await task

    _run(_run_case())
    approval_hook.assert_awaited_once()
    client.sessions.resolve_elicitation.assert_not_awaited()


def test_form_accepted_elsewhere_during_its_approval_opens_no_fields() -> None:
    # Fields opened for a settled form would hold the prompt lock and block
    # every later prompt.
    adapter, host, fis, client, approval_hook = _adapter()

    async def _accepted_elsewhere(ctx: Any) -> bool:
        adapter.note_elicitation_resolved(ctx.elicitation_id)  # as run_repl does
        return True

    approval_hook.side_effect = _accepted_elsewhere
    form = _form_event("elicit_form", {"properties": {"name": {"type": "string"}}})

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", form))
        await _until(lambda: task.done(), "stale form fields opened")
        assert not fis.pending
        nxt = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: host.selector_open, "the next prompt never opened")
        host.answer({"Env?": "dev"})
        await nxt

    _run(_run_case())
    assert [args[1] for args in _posted(client)] == ["elicit_q"]


def test_a_release_elsewhere_does_not_swallow_the_next_verdict() -> None:
    # The released flag belongs to one elicitation; a later form that fails
    # before prompting must still post its decline, or the turn hangs.
    adapter, _, fis, client, _ = _adapter()
    form = _form_event("elicit_form", {"properties": {"name": {"type": "string"}}})

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", form))
        await _until(lambda: fis.pending, "the field prompt never opened")
        assert fis.resolve_elsewhere("elicit_form")
        await task
        bad = _form_event("elicit_next", {"properties": {"x": "not-a-property"}})
        await adapter._handle_elicitation("conv_abc", bad)

    _run(_run_case())
    assert _posted(client) == [("conv_abc", "elicit_next", {"action": "decline"})]


# ── Snapshot replay ───────────────────────────────────────────


def _pending(elicitation_id: str) -> dict[str, Any]:
    return {
        "type": "response.elicitation_request",
        "elicitation_id": elicitation_id,
        "method": "elicitation/create",
        "params": {"mode": "form", "message": "Pick one"},
    }


def test_replay_feeds_pending_prompts_through_the_live_path() -> None:
    from omnigent.server.schemas import ElicitationRequestEvent

    adapter, _, _, client, _ = _adapter()
    client.sessions.get = AsyncMock(
        return_value=SimpleNamespace(
            pending_elicitations=[_pending("elicit_q"), {"type": "bogus"}]
        )
    )
    seen: list[Any] = []
    adapter._on_event = seen.append

    _run(adapter._replay_pending_elicitations("conv_abc"))

    # Unknown events are skipped; the prompt arrives as the same typed event
    # the live stream would deliver.
    assert [type(event) for event in seen] == [ElicitationRequestEvent]
    assert seen[0].elicitation_id == "elicit_q"


def test_replay_releases_an_open_prompt_settled_while_disconnected() -> None:
    # Its resolved event was missed, so without reconciling it would hold the
    # prompt lock until the user answered a prompt that is already settled.
    from omnigent.server.schemas import ElicitationResolvedEvent

    adapter, host, _, client, _ = _adapter()
    client.sessions.get = AsyncMock(
        return_value=SimpleNamespace(pending_elicitations=[_pending("elicit_b")])
    )
    seen: list[Any] = []

    def _on_event(event: Any) -> None:
        seen.append(event)
        if isinstance(event, ElicitationResolvedEvent):
            adapter.note_elicitation_resolved(event.elicitation_id)  # as run_repl does

    adapter._on_event = _on_event

    async def _run_case() -> None:
        open_a = asyncio.create_task(
            adapter._handle_elicitation("conv_abc", _question_event("elicit_a"))
        )
        await _until(lambda: host.selector_open, "prompt A never opened")
        await adapter._replay_pending_elicitations("conv_abc")
        await _until(lambda: open_a.done(), "settled prompt A still holds the lock")

    _run(_run_case())
    released = [e.elicitation_id for e in seen if isinstance(e, ElicitationResolvedEvent)]
    assert released == ["elicit_a"]
    client.sessions.resolve_elicitation.assert_not_awaited()
    assert [getattr(e, "elicitation_id", None) for e in seen][-1] == "elicit_b"


def test_replay_keeps_a_prompt_that_opened_during_the_snapshot_request() -> None:
    adapter, host, _, client, _ = _adapter()
    release = asyncio.Event()

    async def _slow_get(session_id: str) -> Any:
        await release.wait()
        return SimpleNamespace(pending_elicitations=[])

    client.sessions.get = AsyncMock(side_effect=_slow_get)
    seen: list[Any] = []
    adapter._on_event = seen.append

    async def _run_case() -> None:
        replay = asyncio.create_task(adapter._replay_pending_elicitations("conv_abc"))
        await _until(lambda: client.sessions.get.await_count == 1, "snapshot never requested")
        opened = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: host.selector_open, "the live prompt never opened")
        release.set()
        await replay
        assert host.selector_open, "a prompt newer than the snapshot was released"
        host.answer({"Env?": "dev"})
        await opened

    _run(_run_case())
    assert seen == []


def test_replay_after_switching_sessions_is_dropped() -> None:
    adapter, _, _, client, _ = _adapter()
    release = asyncio.Event()

    async def _slow_get(session_id: str) -> Any:
        await release.wait()
        return SimpleNamespace(pending_elicitations=[_pending("elicit_old")])

    client.sessions.get = AsyncMock(side_effect=_slow_get)
    seen: list[Any] = []
    adapter._on_event = seen.append

    async def _run_case() -> None:
        replay = asyncio.create_task(adapter._replay_pending_elicitations("conv_abc"))
        await _until(lambda: client.sessions.get.await_count == 1, "snapshot never requested")
        adapter._session_id = "conv_other"
        release.set()
        await replay

    _run(_run_case())
    assert seen == []


def test_stopping_the_pump_cancels_its_replay() -> None:
    adapter, _, _, client, _ = _adapter()
    blocked = asyncio.Event()

    async def _stream(session_id: str) -> Any:
        yield SimpleNamespace()
        await blocked.wait()

    async def _never_returns(session_id: str) -> Any:
        await asyncio.Event().wait()

    client.sessions.stream = _stream
    client.sessions.get = _never_returns
    adapter._on_event = lambda event: None

    async def _run_case() -> Any:
        pump = asyncio.create_task(adapter._stream_pump())
        await _until(lambda: adapter._replay_task is not None, "replay never started")
        replay = adapter._replay_task
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await _until(lambda: replay.done(), "the replay outlived the pump")
        return replay

    assert _run(_run_case()).cancelled()


def test_stream_reconnect_replays_pending_prompts_once_per_subscription() -> None:
    adapter, _, _, client, _ = _adapter()
    opened = 0

    async def _stream(session_id: str) -> Any:
        nonlocal opened
        if opened == 2:
            await asyncio.Event().wait()  # later subscriptions stay quiet
        opened += 1
        yield SimpleNamespace()
        yield SimpleNamespace()

    client.sessions.stream = _stream
    replay = AsyncMock()
    adapter._replay_pending_elicitations = replay  # type: ignore[method-assign]
    adapter._on_event = lambda event: None

    async def _run_case() -> None:
        pump = asyncio.create_task(adapter._stream_pump())
        # The pump backs off 0.5s before resubscribing.
        await poll_until_async(lambda: opened == 2 and replay.call_count >= 2, timeout_s=5.0)
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump

    _run(_run_case())
    # Two events per subscription, but one replay each.
    assert replay.call_count == 2
