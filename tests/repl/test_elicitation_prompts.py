"""How the REPL's sessions adapter asks and settles elicitations in the terminal.

Questions (the ``ask_user_question`` extra) go to the host's inline selector;
other prompts use the y/a/n approval and schema-field prompts. One prompt is
shown at a time, and a request another client settles closes or never opens.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from omnigent_client import StreamHooks

from omnigent.repl._repl import (
    _ApprovalState,
    _ApprovalVerdict,
    _FieldInputState,
    _question_prompts,
    _SessionsChatReplAdapter,
    _settle_resolved_elsewhere,
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
        approval_state=_ApprovalState(),
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
            "questions": (
                [{"question": "Env?", "options": ["dev", "prod"]}]
                if questions is None
                else questions
            )
        },
    )


def _form_event(elicitation_id: str, schema: dict[str, Any]) -> Any:
    event = _question_event(elicitation_id)
    event.requested_schema = schema
    event.phase = "tool_call"
    del event.ask_user_question
    return event


def _approval_event(elicitation_id: str = "elicit_approve") -> Any:
    event = _form_event(elicitation_id, {})
    event.message = "approve the tool call?"
    return event


def _resolved_elsewhere(adapter: Any, fis: _FieldInputState, elicitation_id: str) -> None:
    """Deliver another client's verdict the way run_repl's resolved-event branch does."""
    _settle_resolved_elsewhere(
        adapter, adapter._approval_state, fis, elicitation_id, "accept", None
    )


def _gate_approvals_on_the_user(adapter: Any, approval_hook: Any) -> None:
    """Make the y/a/n hook wait on the adapter's approval gate, as run_repl's does."""

    async def _wait_for_the_user(ctx: Any) -> bool:
        gate = adapter._approval_state.begin(
            ctx.policy_name, ctx.phase, elicitation_id=ctx.elicitation_id
        )
        return await gate

    approval_hook.side_effect = _wait_for_the_user


def _posted(client: Any) -> list[tuple[Any, ...]]:
    return [c.args for c in client.sessions.resolve_elicitation.await_args_list]


async def _until(condition: Any, message: str) -> None:
    """Yield to the loop until *condition()* holds; fail with *message* rather than hang."""
    try:
        await poll_until_async(condition, timeout_s=2.0, interval_s=0)
    except AssertionError:
        raise AssertionError(message) from None


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
        {"question": "Q?", "options": {}},
        {"question": "Q?", "options": ["dev", 1]},
        {"question": "Q?", "options": [{"label": ""}], "isOther": False},
        {"question": "Q?", "options": [], "isOther": False},
        {"question": "", "options": ["x"]},
        "not-a-question",
    ):
        assert _question_prompts({"questions": [valid, unusable]}) == [], unusable
    assert _question_prompts({"questions": []}) == []
    assert _question_prompts({"questions": "nope"}) == []
    assert _question_prompts(None) == []


# ── Questions through the selector ────────────────────────────


async def test_question_posts_the_selector_answers_without_the_approval_prompt() -> None:
    adapter, host, _, client, approval_hook = _adapter()
    task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    await _until(lambda: host.selector_open, "the selector never opened")
    host.answer({"Env?": "prod"})
    await task

    assert [q.question for q in host.asked[0]] == ["Env?"]
    assert _posted(client) == [
        ("conv_abc", "elicit_q", {"action": "accept", "content": {"Env?": "prod"}})
    ]
    # y/a/n would accept with no answers, and "approve always" would cache it.
    approval_hook.assert_not_awaited()


async def test_dismissing_the_selector_posts_a_decline() -> None:
    adapter, host, _, client, _ = _adapter()
    task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    await _until(lambda: host.selector_open, "the selector never opened")
    host.dismiss_questions()  # what Esc does
    await task

    assert _posted(client) == [("conv_abc", "elicit_q", {"action": "decline"})]


async def test_question_answered_elsewhere_closes_the_selector_without_posting() -> None:
    adapter, host, _, client, _ = _adapter()
    task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    await _until(lambda: host.selector_open, "the selector never opened")
    adapter.note_elicitation_resolved("elicit_q")  # the resolved event's handler
    await _until(lambda: task.done(), "the selector stayed open")

    client.sessions.resolve_elicitation.assert_not_awaited()


async def test_secret_questions_are_left_for_another_client() -> None:
    # Typed terminal input is visible on screen, so a producer-marked secret is
    # never collected here and the elicitation stays pending.
    adapter, host, _, client, approval_hook = _adapter()
    event = _question_event(questions=[{"question": "Token?", "options": [], "isSecret": True}])
    task = asyncio.create_task(adapter._handle_elicitation("conv_abc", event))
    await _until(lambda: task.done(), "a selector opened for a secret question")

    assert host.asked == []
    approval_hook.assert_not_awaited()
    client.sessions.resolve_elicitation.assert_not_awaited()
    assert "Answer it in the web UI" in host.text()


@pytest.mark.parametrize(
    "questions",
    [
        [{"question": "Q?", "options": 1}],
        [
            {"question": "Env?", "options": ["dev"]},
            {"question": "Q?", "options": [], "isOther": False},
        ],
        [],
    ],
    ids=["malformed", "one-unanswerable", "no-questions"],
)
async def test_unusable_questions_decline_without_any_prompt(questions: Any) -> None:
    adapter, host, _, client, approval_hook = _adapter()
    task = asyncio.create_task(
        adapter._handle_elicitation("conv_abc", _question_event(questions=questions))
    )
    await _until(lambda: task.done(), "an unusable question opened a prompt")

    assert host.asked == []
    approval_hook.assert_not_awaited()
    assert _posted(client) == [("conv_abc", "elicit_q", {"action": "decline"})]


# ── One request at a time, never a settled one ────────────────


async def test_concurrent_prompts_are_asked_one_at_a_time() -> None:
    adapter, host, _, client, _ = _adapter()
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

    assert [args[1] for args in _posted(client)] == ["elicit_q", "elicit_second"]


async def test_prompt_kinds_take_the_terminal_one_at_a_time() -> None:
    # A question, a y/a/n approval and a schema form queued together open in
    # turn, each only once the previous one is answered.
    adapter, host, fis, client, approval_hook = _adapter()
    _gate_approvals_on_the_user(adapter, approval_hook)
    approvals = adapter._approval_state
    form = _form_event("elicit_form", {"properties": {"name": {"type": "string"}}})
    question = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    await _until(lambda: host.selector_open, "the question never opened")
    approval = asyncio.create_task(adapter._handle_elicitation("conv_abc", _approval_event()))
    schema_form = asyncio.create_task(adapter._handle_elicitation("conv_abc", form))
    for _ in range(50):
        await asyncio.sleep(0)
    assert not approvals.pending and not fis.pending, "a prompt opened over the selector"

    host.answer({"Env?": "dev"})
    await _until(lambda: approvals.pending, "the approval never opened")
    assert not host.selector_open and not fis.pending
    approvals.resolve_verdict(_ApprovalVerdict.APPROVE_ONCE)
    await approval
    await _until(lambda: approvals.pending, "the form's approval never opened")
    approvals.resolve_verdict(_ApprovalVerdict.APPROVE_ONCE)
    await _until(lambda: fis.pending, "the form's field never opened")
    fis.resolve("Ada")
    await question
    await schema_form

    assert [args[1] for args in _posted(client)] == ["elicit_q", "elicit_approve", "elicit_form"]


async def test_a_pre_approved_request_does_not_wait_for_an_open_question() -> None:
    # "Approve always" answers it without input, so it skips the prompt queue.
    adapter, host, _, client, approval_hook = _adapter()
    _gate_approvals_on_the_user(adapter, approval_hook)
    event = _approval_event()
    adapter._approval_state.remember_always(event.policy_name, event.phase)
    approval_hook.side_effect = lambda ctx: _pre_approved_hook(adapter, ctx)
    question = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    await _until(lambda: host.selector_open, "the question never opened")
    approval = asyncio.create_task(adapter._handle_elicitation("conv_abc", event))
    await _until(lambda: approval.done(), "the pre-approved request waited for the question")

    assert host.selector_open, "the selector closed"
    assert _posted(client) == [("conv_abc", "elicit_approve", {"action": "accept"})]
    host.answer({"Env?": "dev"})
    await question


async def _pre_approved_hook(adapter: Any, ctx: Any) -> bool:
    """run_repl's y/a/n hook for a cached "always": approve without prompting."""
    assert adapter._approval_state.is_pre_approved(ctx.policy_name, ctx.phase)
    return True


async def test_prompt_settled_before_its_turn_is_never_shown() -> None:
    # The stream can deliver a resolution right after its request, before the
    # prompt task runs; here it also waits behind another prompt.
    adapter, host, _, client, _ = _adapter()
    first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    await _until(lambda: host.selector_open, "the first question never opened")
    queued = adapter.queue_elicitation("conv_abc", _question_event("elicit_queued"))
    adapter.note_elicitation_resolved("elicit_queued")
    host.answer({"Env?": "dev"})
    await first
    assert queued is not None
    await queued

    assert len(host.asked) == 1
    assert [args[1] for args in _posted(client)] == ["elicit_q"]


async def test_a_repeated_request_is_asked_once() -> None:
    # The server republishes a pending request (e.g. a newer delivery state).
    adapter, host, _, client, _ = _adapter()
    first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    await _until(lambda: host.selector_open, "the question never opened")
    assert adapter.queue_elicitation("conv_abc", _question_event()) is None
    host.answer({"Env?": "dev"})
    await first

    assert len(host.asked) == 1
    assert len(_posted(client)) == 1


@pytest.mark.parametrize(
    "answered_here", [True, False], ids=["answered-here", "answered-elsewhere"]
)
@pytest.mark.parametrize(
    "later_question",
    [
        [{"question": "Env?", "options": ["dev", "prod"]}],
        [{"question": "Region?", "options": ["us", "eu"]}],
    ],
    ids=["same-question", "different-question"],
)
async def test_a_later_request_reusing_a_settled_id_opens(
    answered_here: bool, later_question: list[dict[str, Any]]
) -> None:
    # The server may reuse an id once a request is settled: a re-park of the
    # same question, or a different one.
    adapter, host, _, client, _ = _adapter()
    first = asyncio.create_task(
        adapter._handle_elicitation("conv_abc", _question_event("elicit_x"))
    )
    await _until(lambda: host.selector_open, "question A never opened")
    if answered_here:
        # No resolved echo afterwards: the stream can miss it.
        host.answer({"Env?": "dev"})
    else:
        adapter.note_elicitation_resolved("elicit_x")
    await first
    later = _question_event("elicit_x", later_question)
    second = asyncio.create_task(adapter._handle_elicitation("conv_abc", later))
    await _until(lambda: host.selector_open, "the later request under the reused id never opened")
    question_text = later_question[0]["question"]
    host.answer({question_text: "picked"})
    await second

    assert [q.question for q in host.asked[-1]] == [question_text]
    assert _posted(client)[-1] == (
        "conv_abc",
        "elicit_x",
        {"action": "accept", "content": {question_text: "picked"}},
    )


@pytest.mark.parametrize("old_prompt", ["selector", "approval", "form"])
async def test_a_re_ask_under_an_open_id_replaces_the_old_prompt(old_prompt: str) -> None:
    # A sub-agent can re-ask under the same id with no resolved event between;
    # whatever the old prompt is, its answer must not resolve the new request.
    adapter, host, fis, client, approval_hook = _adapter()
    old_event = {
        "selector": _question_event("elicit_x"),
        "approval": _approval_event("elicit_x"),
        "form": _form_event("elicit_x", {"properties": {"name": {"type": "string"}}}),
    }[old_prompt]
    if old_prompt == "approval":
        _gate_approvals_on_the_user(adapter, approval_hook)
    old = asyncio.create_task(adapter._handle_elicitation("conv_abc", old_event))
    opened = {
        "selector": lambda: host.selector_open,
        "approval": lambda: adapter._approval_state.pending,
        "form": lambda: fis.pending,
    }[old_prompt]
    await _until(opened, "the first prompt never opened")
    question_b = _question_event("elicit_x", [{"question": "Region?", "options": ["us", "eu"]}])
    new = adapter.queue_elicitation("conv_abc", question_b)
    assert new is not None
    await _until(lambda: old.done(), "the replaced prompt kept holding the terminal")
    await _until(lambda: host.selector_open, "the new question never opened")
    host.answer({"Region?": "eu"})
    await new

    assert [q.question for q in host.asked[-1]] == ["Region?"]
    assert _posted(client) == [
        ("conv_abc", "elicit_x", {"action": "accept", "content": {"Region?": "eu"}})
    ]


async def test_a_retry_stops_once_its_request_is_settled() -> None:
    # A's first send drops; before its retry, the id is re-asked as B. The
    # retry must not post A's answer against B.
    adapter, host, _, client, _ = _adapter()
    sent: list[Any] = []

    async def _drop_the_first_send(*args: Any) -> None:
        sent.append(args)
        if len(sent) == 1:
            raise httpx.ConnectError("dropped")

    client.sessions.resolve_elicitation = AsyncMock(side_effect=_drop_the_first_send)
    first = asyncio.create_task(
        adapter._handle_elicitation("conv_abc", _question_event("elicit_x"))
    )
    await _until(lambda: host.selector_open, "question A never opened")
    host.answer({"Env?": "dev"})
    await _until(lambda: len(sent) == 1, "A's answer was never sent")
    question_b = _question_event("elicit_x", [{"question": "Region?", "options": ["us", "eu"]}])
    second = adapter.queue_elicitation("conv_abc", question_b)
    assert second is not None
    await first
    await _until(lambda: host.selector_open, "question B never opened")
    host.answer({"Region?": "eu"})
    await second

    assert [args[2] for args in sent] == [
        {"action": "accept", "content": {"Env?": "dev"}},
        {"action": "accept", "content": {"Region?": "eu"}},
    ]


async def test_a_failing_selector_declines_and_frees_the_queue() -> None:
    adapter, host, _, client, _ = _adapter()
    asked: list[Any] = []

    async def _broken(questions: list[Any]) -> Any:
        asked.append(questions)
        if len(asked) == 1:
            raise RuntimeError("selector failed")
        return {"Env?": "prod"}

    host.ask_questions = _broken  # type: ignore[method-assign]
    first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    second = asyncio.create_task(
        adapter._handle_elicitation("conv_abc", _question_event("elicit_next"))
    )
    await first
    await second

    assert _posted(client) == [
        ("conv_abc", "elicit_q", {"action": "decline"}),
        ("conv_abc", "elicit_next", {"action": "accept", "content": {"Env?": "prod"}}),
    ]


# ── Schema forms and approvals settled by another client ──────


async def test_schema_form_answered_elsewhere_stops_without_posting() -> None:
    adapter, _, fis, client, approval_hook = _adapter()
    form = _form_event("elicit_form", {"properties": {"name": {"type": "string"}}})
    task = asyncio.create_task(adapter._handle_elicitation("conv_abc", form))
    await _until(lambda: fis.pending, "the field prompt never opened")
    _resolved_elsewhere(adapter, fis, "elicit_form")
    await task

    approval_hook.assert_awaited_once()
    client.sessions.resolve_elicitation.assert_not_awaited()


async def test_schema_form_settled_between_fields_asks_nothing_more() -> None:
    # The resolution lands after field one is submitted but before the form
    # resumes, when no field is pending to release.
    adapter, _, fis, client, _ = _adapter()
    schema = {"properties": {"name": {"type": "string"}, "team": {"type": "string"}}}
    task = asyncio.create_task(
        adapter._handle_elicitation("conv_abc", _form_event("elicit_form", schema))
    )
    await _until(lambda: fis.pending, "the first field never opened")
    fis.resolve("Ada")
    _resolved_elsewhere(adapter, fis, "elicit_form")
    await _until(lambda: task.done(), "the form kept asking after it was settled")

    assert not fis.pending
    client.sessions.resolve_elicitation.assert_not_awaited()


async def test_form_accepted_elsewhere_during_its_approval_opens_no_fields() -> None:
    # Fields opened for a settled form would hold the prompt lock and block
    # every later prompt.
    adapter, host, fis, client, approval_hook = _adapter()

    async def _accepted_elsewhere(ctx: Any) -> bool:
        adapter.note_elicitation_resolved(ctx.elicitation_id)  # as run_repl does
        return True

    approval_hook.side_effect = _accepted_elsewhere
    form = _form_event("elicit_form", {"properties": {"name": {"type": "string"}}})
    task = asyncio.create_task(adapter._handle_elicitation("conv_abc", form))
    await _until(lambda: task.done(), "stale form fields opened")
    assert not fis.pending
    nxt = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
    await _until(lambda: host.selector_open, "the next prompt never opened")
    host.answer({"Env?": "dev"})
    await nxt

    assert [args[1] for args in _posted(client)] == ["elicit_q"]


async def test_a_release_elsewhere_does_not_swallow_the_next_verdict() -> None:
    # The settled flag belongs to one request; a later form that fails before
    # prompting must still post its decline, or the turn hangs.
    adapter, _, fis, client, _ = _adapter()
    form = _form_event("elicit_form", {"properties": {"name": {"type": "string"}}})
    task = asyncio.create_task(adapter._handle_elicitation("conv_abc", form))
    await _until(lambda: fis.pending, "the field prompt never opened")
    _resolved_elsewhere(adapter, fis, "elicit_form")
    await task
    bad = _form_event("elicit_next", {"properties": {"x": "not-a-property"}})
    await adapter._handle_elicitation("conv_abc", bad)

    assert _posted(client) == [("conv_abc", "elicit_next", {"action": "decline"})]


async def test_approval_settled_elsewhere_posts_no_verdict() -> None:
    adapter, _, _, client, approval_hook = _adapter()

    async def _settled_elsewhere(ctx: Any) -> bool:
        adapter.note_elicitation_resolved(ctx.elicitation_id)  # as run_repl does
        return False

    approval_hook.side_effect = _settled_elsewhere
    await adapter._handle_elicitation("conv_abc", _approval_event())

    client.sessions.resolve_elicitation.assert_not_awaited()
