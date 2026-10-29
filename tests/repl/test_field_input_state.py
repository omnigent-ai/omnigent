"""Tests for ``_FieldInputState`` in ``omnigent.repl._repl``.

Covers the future-based field input collection used to prompt
schema fields interactively in the REPL.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from omnigent.repl._repl import _FieldInputState, _SessionsChatReplAdapter
from tests._helpers.polling import poll_until_async


def test_not_pending_initially() -> None:
    state = _FieldInputState()
    assert not state.pending
    assert state.field_name is None


def test_begin_creates_pending_future() -> None:
    state = _FieldInputState()
    loop = asyncio.new_event_loop()
    try:
        fut = loop.run_until_complete(_begin(state, "name"))
        assert state.pending
        assert state.field_name == "name"
        assert not fut.done()
    finally:
        loop.close()


def test_resolve_completes_future() -> None:
    state = _FieldInputState()

    async def _run() -> str:
        fut = state.begin("email")
        assert state.pending
        state.resolve("test@example.com")
        return await fut

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_run())
        assert result == "test@example.com"
        assert not state.pending
        assert state.field_name is None
    finally:
        loop.close()


def test_resolve_returns_false_when_no_pending() -> None:
    state = _FieldInputState()
    assert not state.resolve("value")


def test_cancel_resolves_with_empty_string() -> None:
    state = _FieldInputState()

    async def _run() -> str:
        fut = state.begin("field")
        state.cancel()
        return await fut

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_run())
        assert result == ""
        assert not state.pending
    finally:
        loop.close()


def test_begin_replaces_previous_future() -> None:
    state = _FieldInputState()

    async def _run() -> tuple[str, str]:
        fut1 = state.begin("first")
        fut2 = state.begin("second")
        assert fut1.done()
        assert await fut1 == ""
        state.resolve("val2")
        return await fut1, await fut2

    loop = asyncio.new_event_loop()
    try:
        r1, r2 = loop.run_until_complete(_run())
        assert r1 == ""
        assert r2 == "val2"
    finally:
        loop.close()


async def _begin(state: _FieldInputState, name: str) -> asyncio.Future[str]:
    return state.begin(name)


# --------------------------------------------------------------------------
# Abort contract: cancel() must be distinguishable from an empty submit.
# --------------------------------------------------------------------------


def test_aborted_false_initially() -> None:
    state = _FieldInputState()
    assert not state.aborted


def test_cancel_sets_aborted() -> None:
    state = _FieldInputState()

    async def _run() -> bool:
        state.begin("field")
        state.cancel()
        return state.aborted

    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(_run())
    finally:
        loop.close()


def test_begin_clears_aborted() -> None:
    """A fresh prompt starts un-aborted even after a prior cancel."""
    state = _FieldInputState()

    async def _run() -> bool:
        state.begin("a")
        state.cancel()
        assert state.aborted
        state.begin("b")
        return state.aborted

    loop = asyncio.new_event_loop()
    try:
        assert not loop.run_until_complete(_run())
    finally:
        loop.close()


# --------------------------------------------------------------------------
# _prompt_schema_fields: parsing, validation, re-prompting, abort, markup.
# --------------------------------------------------------------------------


class _FakeHost:
    """Records rendered output instead of writing to a terminal."""

    def __init__(self) -> None:
        self.outputs: list[Any] = []

    def output(self, renderable: Any) -> None:
        self.outputs.append(renderable)


class _FakeFmt:
    accent = "cyan"
    muted = "grey50"
    warning = "yellow"


def _outputs_text(host: _FakeHost) -> str:
    """Concatenate the ``.plain`` of every rendered Rich ``Text``."""
    return "\n".join(getattr(o, "plain", str(o)) for o in host.outputs)


async def _drive_prompt(
    schema: dict[str, Any],
    answers: list[str],
    *,
    abort_at: int | None = None,
    state: _FieldInputState | None = None,
) -> tuple[dict[str, Any] | None, _FakeHost]:
    """Run ``_prompt_schema_fields`` feeding scripted user answers.

    :param answers: Values fed in order each time a field is pending.
    :param abort_at: 1-based prompt index at which to ``cancel()``
        (simulating Esc) instead of resolving.
    :param state: Optional pre-built state (``None`` builds a fresh one;
        pass an adapter-detached run by leaving the adapter's state unset).
    """
    fis = state if state is not None else _FieldInputState()
    host = _FakeHost()
    adapter = _prompt_adapter(fis, host)
    result = await _feed_answers(fis, adapter._prompt_schema_fields(schema), answers, abort_at)
    return result, host


def _prompt_adapter(fis: _FieldInputState, host: _FakeHost) -> _SessionsChatReplAdapter:
    """Adapter with only the terminal prompt surface wired."""
    adapter = _SessionsChatReplAdapter.__new__(_SessionsChatReplAdapter)
    adapter._field_input_state = fis  # type: ignore[attr-defined]
    adapter._host = host  # type: ignore[attr-defined]
    adapter._fmt = _FakeFmt()  # type: ignore[attr-defined]
    return adapter


async def _feed_answers(
    fis: _FieldInputState,
    prompt: Any,
    answers: list[str],
    abort_at: int | None = None,
) -> Any:
    """Await *prompt*, resolving each pending field with the next scripted answer."""
    task = asyncio.create_task(prompt)
    answer_iter = iter(answers)
    prompts = 0
    guard = 0
    while not task.done():
        await asyncio.sleep(0)
        guard += 1
        assert guard < 200, "prompt loop did not converge"
        if fis.pending:
            prompts += 1
            if abort_at is not None and prompts == abort_at:
                fis.cancel()
                continue
            fis.resolve(next(answer_iter, ""))
    return await task


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_prompt_collects_string_and_integer() -> None:
    schema = {
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
    }
    result, _ = _run(_drive_prompt(schema, ["alice", "42"]))
    assert result == {"name": "alice", "age": 42}


def test_prompt_boolean_parsing() -> None:
    schema = {"properties": {"flag": {"type": "boolean"}}, "required": ["flag"]}
    assert _run(_drive_prompt(schema, ["yes"]))[0] == {"flag": True}
    assert _run(_drive_prompt(schema, ["nope"]))[0] == {"flag": False}


def test_prompt_uses_default_on_empty() -> None:
    schema = {"properties": {"verbose": {"type": "boolean", "default": False}}}
    result, _ = _run(_drive_prompt(schema, [""]))
    assert result == {"verbose": False}


def test_prompt_skips_optional_on_empty() -> None:
    schema = {"properties": {"note": {"type": "string"}}}
    result, _ = _run(_drive_prompt(schema, [""]))
    assert result == {}


def test_prompt_required_empty_reprompts_then_accepts() -> None:
    schema = {"properties": {"name": {"type": "string"}}, "required": ["name"]}
    result, host = _run(_drive_prompt(schema, ["", "bob"]))
    assert result == {"name": "bob"}
    assert "required" in _outputs_text(host)


def test_prompt_invalid_integer_reprompts() -> None:
    schema = {"properties": {"age": {"type": "integer"}}, "required": ["age"]}
    result, host = _run(_drive_prompt(schema, ["abc", "7"]))
    assert result == {"age": 7}
    assert "whole number" in _outputs_text(host)


def test_prompt_enum_rejects_then_accepts() -> None:
    schema = {
        "properties": {"choice": {"type": "string", "enum": ["yes", "no"]}},
        "required": ["choice"],
    }
    result, host = _run(_drive_prompt(schema, ["maybe", "yes"]))
    assert result == {"choice": "yes"}
    assert "choose one of" in _outputs_text(host)


def test_prompt_one_of_const_rejects_then_accepts() -> None:
    schema = {
        "properties": {"mode": {"oneOf": [{"const": "a"}, {"const": "b"}]}},
        "required": ["mode"],
    }
    result, _ = _run(_drive_prompt(schema, ["z", "b"]))
    assert result == {"mode": "b"}


def test_prompt_abort_returns_none() -> None:
    """Esc mid-prompt declines the whole form rather than advancing."""
    schema = {
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }
    result, _ = _run(_drive_prompt(schema, [], abort_at=1))
    assert result is None


def test_prompt_markup_in_schema_text_is_safe() -> None:
    """Bracketed server text must not be parsed as Rich markup.

    A description containing an unbalanced closing tag would raise
    ``MarkupError`` under ``Text.from_markup`` and crash the prompt;
    here it must render literally and collection must succeed.
    """
    schema = {
        "properties": {"x": {"type": "string", "description": "use [bold] or [/red]"}},
        "required": ["x"],
    }
    result, host = _run(_drive_prompt(schema, ["v"]))
    assert result == {"x": "v"}
    assert "[/red]" in _outputs_text(host)


def test_prompt_returns_none_when_state_unavailable() -> None:
    schema = {"properties": {"x": {"type": "string"}}}
    adapter = _SessionsChatReplAdapter.__new__(_SessionsChatReplAdapter)
    adapter._field_input_state = None  # type: ignore[attr-defined]
    adapter._host = None  # type: ignore[attr-defined]
    adapter._fmt = None  # type: ignore[attr-defined]
    assert _run(adapter._prompt_schema_fields(schema)) is None


# --------------------------------------------------------------------------
# AskUserQuestion picker: questions arrive as the ``ask_user_question`` extra
# and are answered with numbered options instead of a y/a/n approval.
# --------------------------------------------------------------------------


def _question(**overrides: Any) -> Any:
    from omnigent.repl._repl import _QuestionPrompt

    fields: dict[str, Any] = {
        "key": "Env?",
        "question": "Env?",
        "header": "",
        "options": (("dev", ""), ("staging", ""), ("prod", "")),
        "multi_select": False,
        "allow_custom": True,
    }
    fields.update(overrides)
    return _QuestionPrompt(**fields)


@pytest.mark.parametrize(
    ("typed", "overrides", "expected"),
    [
        ("2", {}, "staging"),
        (" STAGING ", {}, "staging"),
        ("1,3", {"multi_select": True}, ["dev", "prod"]),
        ("3 1 3", {"multi_select": True}, ["prod", "dev"]),
        ("dev", {"multi_select": True}, ["dev"]),
        ("1,2", {}, None),
        ("4", {"allow_custom": False}, None),
        ("8080", {}, "8080"),
        ("3", {"options": (("1", ""), ("3", ""), ("5", ""))}, "5"),
        ("3,1", {"options": (("1", ""), ("3", ""), ("5", "")), "multi_select": True}, ["5", "1"]),
        ('"3"', {"options": (("1", ""), ("3", ""), ("5", ""))}, "3"),
        ('"2"', {"allow_custom": False}, None),
        ("²", {"allow_custom": False}, None),
        ("", {}, None),
        ("qa", {}, "qa"),
        ("qa", {"multi_select": True}, ["qa"]),
        ("qa", {"allow_custom": False}, None),
        ("", {"options": (), "text_default": "draft"}, "draft"),
        ("  keep spaces ", {"options": (), "text_default": "draft"}, "  keep spaces "),
        ("   ", {"options": (), "text_default": "draft"}, "   "),
    ],
    ids=[
        "number",
        "label-any-case",
        "multi-numbers",
        "multi-dedupes-in-order",
        "multi-label",
        "single-rejects-two",
        "out-of-range",
        "out-of-range-is-text",
        "number-beats-numeric-label",
        "multi-number-beats-numeric-label",
        "quoted-number-is-text",
        "quoted-needs-custom",
        "superscript-is-not-a-number",
        "empty",
        "custom-text",
        "multi-custom-text",
        "custom-not-allowed",
        "editor-keeps-default",
        "editor-exact-text",
        "editor-whitespace-is-an-answer",
    ],
)
def test_parse_question_answer(typed: str, overrides: dict[str, Any], expected: Any) -> None:
    from omnigent.repl._repl import _parse_question_answer

    assert _parse_question_answer(typed, _question(**overrides)) == expected


def test_question_prompts_follow_web_form_rules() -> None:
    from omnigent.repl._repl import _question_prompts

    prompts = _question_prompts(
        {
            "questions": [
                {"question": "Env?", "options": [{"label": "dev", "description": "d"}, "prod"]},
                {"id": "0", "question": "Pick", "options": ["a"], "isOther": False},
                {"question": "Name?", "options": [], "isOther": False},
                {"question": "Broken?", "options": 1},
                {"question": "", "options": ["x"]},
                "not-a-question",
            ]
        }
    )
    assert [(p.key, p.options, p.allow_custom) for p in prompts] == [
        ("Env?", (("dev", "d"), ("prod", "")), True),
        ("0", (("a", ""),), False),
        # With no options a typed answer is the only way to reply.
        ("Name?", (), True),
    ]
    assert _question_prompts({"questions": "nope"}) == []
    assert _question_prompts(None) == []


def test_question_picker_collects_answers_keyed_like_the_web_form() -> None:
    from omnigent.repl._repl import _question_prompts

    questions = _question_prompts(
        {
            "questions": [
                {"question": "Env?", "header": "Env", "options": ["dev", "prod"]},
                {
                    "id": "regions",
                    "question": "Regions?",
                    "multiSelect": True,
                    "options": ["a", "b"],
                },
            ]
        }
    )
    fis = _FieldInputState()
    host = _FakeHost()
    adapter = _prompt_adapter(fis, host)
    # A bad answer re-prompts the same question instead of skipping it.
    result = _run(
        _feed_answers(fis, adapter._prompt_questions("elicit_q", questions), ["", "2", "1,2"])
    )
    assert result == {"Env?": "prod", "regions": ["a", "b"]}
    text = _outputs_text(host)
    assert "question 1 of 2 · Env" in text
    assert "     2. prod" in text
    assert "↳ enter a number, or type your own answer" in text


def test_question_picker_abort_returns_none() -> None:
    from omnigent.repl._repl import _question_prompts

    questions = _question_prompts({"questions": [{"question": "Env?", "options": ["dev"]}]})
    fis = _FieldInputState()
    adapter = _prompt_adapter(fis, _FakeHost())
    result = _run(_feed_answers(fis, adapter._prompt_questions("elicit_q", questions), [], 1))
    assert result is None


def test_question_picker_renders_markup_in_options_literally() -> None:
    from omnigent.repl._repl import _question_prompts

    questions = _question_prompts(
        {"questions": [{"question": "Use [bold]?", "options": [{"label": "[/red]"}]}]}
    )
    fis = _FieldInputState()
    host = _FakeHost()
    adapter = _prompt_adapter(fis, host)
    assert _run(_feed_answers(fis, adapter._prompt_questions("e", questions), ["1"])) == {
        "Use [bold]?": "[/red]"
    }
    assert "1. [/red]" in _outputs_text(host)


def test_resolve_elsewhere_releases_only_its_own_prompt() -> None:
    state = _FieldInputState()

    async def _run_case() -> tuple[bool, bool, str]:
        fut = state.begin("answer", "elicit_mine")
        other = state.resolve_elsewhere("elicit_other")
        mine = state.resolve_elsewhere("elicit_mine")
        return other, mine, await fut

    other, mine, value = _run(_run_case())
    assert (other, mine, value) == (False, True, "")
    assert state.aborted and state.resolved_elsewhere_for("elicit_mine") and not state.pending
    assert not state.resolved_elsewhere_for("elicit_other")


def _question_adapter() -> tuple[_SessionsChatReplAdapter, _FieldInputState, Any, Any]:
    """Real sessions adapter with a mocked client and a y/a/n hook that must stay unused."""
    from unittest.mock import AsyncMock, MagicMock

    from omnigent_client import StreamHooks

    client = MagicMock()
    client.sessions.resolve_elicitation = AsyncMock(return_value=None)
    approval_hook = AsyncMock(return_value=True)
    fis = _FieldInputState()
    adapter = _SessionsChatReplAdapter(
        client=client,
        agent_name="test-agent",
        hooks=StreamHooks(on_elicitation_request=approval_hook),
        session_id="conv_abc",
        field_input_state=fis,
        host=_FakeHost(),  # type: ignore[arg-type]
        fmt=_FakeFmt(),  # type: ignore[arg-type]
    )
    return adapter, fis, client, approval_hook


def _question_event() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        elicitation_id="elicit_q",
        message="Claude wants to use **AskUserQuestion**",
        requested_schema={},
        mode="form",
        phase="pre_tool_use",
        policy_name="claude_sdk_permission",
        content_preview="",
        url=None,
        ask_user_question={"questions": [{"question": "Env?", "options": ["dev", "prod"]}]},
    )


def test_question_elicitation_posts_answers_without_the_approval_prompt() -> None:
    adapter, fis, client, approval_hook = _question_adapter()
    _run(_feed_answers(fis, adapter._handle_elicitation("conv_abc", _question_event()), ["2"]))
    client.sessions.resolve_elicitation.assert_awaited_once_with(
        "conv_abc", "elicit_q", {"action": "accept", "content": {"Env?": "prod"}}
    )
    # y/a/n would accept with no answers, and "approve always" would cache it.
    approval_hook.assert_not_awaited()


def test_question_answered_elsewhere_posts_nothing() -> None:
    adapter, fis, client, _ = _question_adapter()

    async def _answer_in_web_ui() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        for _ in range(200):
            if fis.pending:
                break
            await asyncio.sleep(0)
        assert fis.resolve_elsewhere("elicit_q"), "the question picker never opened"
        await task

    _run(_answer_in_web_ui())
    client.sessions.resolve_elicitation.assert_not_awaited()


def test_settle_resolved_elsewhere_releases_an_open_question_prompt() -> None:
    from omnigent.repl._repl import _ApprovalState, _settle_resolved_elsewhere

    state = _FieldInputState()
    approvals = _ApprovalState()

    async def _run_case() -> tuple[str | None, str | None]:
        state.begin("answer", "elicit_q")
        unrelated = _settle_resolved_elsewhere(approvals, state, "elicit_x", "accept", None)
        mine = _settle_resolved_elsewhere(approvals, state, "elicit_q", "accept", None)
        return unrelated, mine

    assert _run(_run_case()) == (None, "approved elsewhere")
    assert state.resolved_elsewhere_for("elicit_q")


def test_replay_feeds_pending_prompts_through_the_live_path() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omnigent.server.schemas import ElicitationRequestEvent

    adapter, _, client, _ = _question_adapter()
    raw = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_q",
        "method": "elicitation/create",
        "params": {"mode": "form", "message": "Pick one", "ask_user_question": {"questions": []}},
    }
    client.sessions.get = AsyncMock(
        return_value=SimpleNamespace(pending_elicitations=[raw, {"type": "bogus"}])
    )
    seen: list[Any] = []
    adapter._on_event = seen.append

    _run(adapter._replay_pending_elicitations("conv_abc"))

    # Unknown events are skipped; the prompt arrives as the same typed event
    # the live stream would deliver.
    assert [type(event) for event in seen] == [ElicitationRequestEvent]
    assert seen[0].elicitation_id == "elicit_q"


def test_a_prompt_seen_live_and_in_replay_is_handled_once() -> None:
    adapter, fis, client, _ = _question_adapter()

    async def _deliver_twice() -> None:
        first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        for _ in range(200):
            if fis.pending:
                break
            await asyncio.sleep(0)
        duplicate = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        for _ in range(50):
            if duplicate.done():
                break
            await asyncio.sleep(0)
        assert duplicate.done(), "the duplicate opened a second picker"
        fis.resolve("1")
        await first

    _run(_deliver_twice())
    client.sessions.resolve_elicitation.assert_awaited_once_with(
        "conv_abc", "elicit_q", {"action": "accept", "content": {"Env?": "dev"}}
    )


async def _until(condition: Any, message: str) -> None:
    """Yield to the loop until *condition()* holds; fail with *message* rather than hang."""
    try:
        await poll_until_async(condition, timeout_s=2.0, interval_s=0)
    except AssertionError:
        raise AssertionError(message) from None


def test_concurrent_prompts_are_asked_one_at_a_time() -> None:
    # Concurrent prompts queue for the shared input line instead of taking it
    # from each other, and an editor question never submits its prefill unasked.
    adapter, fis, client, _ = _question_adapter()
    editor = _question_event()
    editor.elicitation_id = "elicit_editor"
    editor.ask_user_question = {
        "questions": [
            {"id": "0", "question": "Notes?", "options": [], "textInput": {"default": "draft"}}
        ]
    }

    async def _answer_both() -> None:
        first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        second = asyncio.create_task(adapter._handle_elicitation("conv_abc", editor))
        await _until(lambda: fis.pending, "the first question never opened")
        for _ in range(50):
            await asyncio.sleep(0)
        client.sessions.resolve_elicitation.assert_not_awaited()
        fis.resolve("2")
        await _until(lambda: fis.pending and first.done(), "the second question never opened")
        fis.resolve("my notes")
        await asyncio.gather(first, second)

    _run(_answer_both())
    assert [c.args for c in client.sessions.resolve_elicitation.await_args_list] == [
        ("conv_abc", "elicit_q", {"action": "accept", "content": {"Env?": "prod"}}),
        ("conv_abc", "elicit_editor", {"action": "accept", "content": {"0": "my notes"}}),
    ]


def test_prompt_resolved_elsewhere_before_its_turn_is_never_shown() -> None:
    # A stale snapshot replay, or a prompt still queued behind another one,
    # must not open after another client settled it.
    adapter, fis, client, _ = _question_adapter()
    queued = _question_event()
    queued.elicitation_id = "elicit_queued"

    async def _run_case() -> None:
        first = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: fis.pending, "the first question never opened")
        second = asyncio.create_task(adapter._handle_elicitation("conv_abc", queued))
        adapter.note_elicitation_resolved("elicit_queued")
        fis.resolve("1")
        await _until(lambda: second.done(), "the settled prompt opened anyway")
        await first
        # Replay of an already-settled id is a no-op too.
        replay = asyncio.create_task(adapter._handle_elicitation("conv_abc", queued))
        await _until(lambda: replay.done(), "the settled prompt reopened from replay")

    _run(_run_case())
    assert [c.args[1] for c in client.sessions.resolve_elicitation.await_args_list] == ["elicit_q"]


def test_undelivered_answer_can_be_retried_from_a_later_replay() -> None:
    from unittest.mock import AsyncMock

    import httpx

    adapter, fis, client, _ = _question_adapter()
    client.sessions.resolve_elicitation = AsyncMock(
        side_effect=[httpx.ConnectError("down")] * 3 + [None]
    )

    async def _run_case() -> None:
        for _ in range(2):  # live delivery, then the reconnect replay
            task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
            await _until(lambda: fis.pending, "the question did not reopen after a failed send")
            fis.resolve("1")
            await task

    _run(_run_case())
    assert client.sessions.resolve_elicitation.await_count == 4


def test_schema_prompt_resolved_elsewhere_stops_without_posting() -> None:
    adapter, fis, client, approval_hook = _question_adapter()
    event = _form_event(
        "elicit_form", {"properties": {"name": {"type": "string"}}, "required": ["name"]}
    )

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", event))
        await _until(lambda: fis.pending, "the schema field prompt never opened")
        assert fis.resolve_elsewhere("elicit_form")
        await task

    _run(_run_case())
    approval_hook.assert_awaited_once()
    client.sessions.resolve_elicitation.assert_not_awaited()


def test_stream_reconnect_replays_pending_prompts_once_per_subscription() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter, _, client, _ = _question_adapter()
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


def _form_event(elicitation_id: str, schema: dict[str, Any]) -> Any:
    event = _question_event()
    event.elicitation_id = elicitation_id
    event.requested_schema = schema
    event.phase = "tool_call"
    del event.ask_user_question
    return event


def test_a_release_elsewhere_does_not_swallow_the_next_verdict() -> None:
    # The released flag belongs to one elicitation; a later form that fails
    # before prompting must still post its decline, or the turn hangs.
    adapter, fis, client, _ = _question_adapter()

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: fis.pending, "the question never opened")
        assert fis.resolve_elsewhere("elicit_q")
        await task
        bad_schema = {"properties": {"x": "not-a-property"}}
        await adapter._handle_elicitation("conv_abc", _form_event("elicit_next", bad_schema))

    _run(_run_case())
    client.sessions.resolve_elicitation.assert_awaited_once_with(
        "conv_abc", "elicit_next", {"action": "decline"}
    )


def test_esc_on_a_question_posts_a_decline() -> None:
    adapter, fis, client, _ = _question_adapter()

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: fis.pending, "the question never opened")
        fis.cancel()
        await task

    _run(_run_case())
    client.sessions.resolve_elicitation.assert_awaited_once_with(
        "conv_abc", "elicit_q", {"action": "decline"}
    )


def test_malformed_questions_decline_without_the_approval_prompt() -> None:
    # Nothing usable to ask: decline rather than accept with no answers.
    adapter, _, client, approval_hook = _question_adapter()
    event = _question_event()
    event.ask_user_question = {"questions": [{"question": "Q?", "options": 1}]}

    _run(adapter._handle_elicitation("conv_abc", event))

    approval_hook.assert_not_awaited()
    client.sessions.resolve_elicitation.assert_awaited_once_with(
        "conv_abc", "elicit_q", {"action": "decline"}
    )


def test_replay_after_switching_sessions_is_dropped() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    adapter, _, client, _ = _question_adapter()
    release = asyncio.Event()
    pending = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_old",
        "method": "elicitation/create",
        "params": {"mode": "form", "message": "old session"},
    }

    async def _slow_get(session_id: str) -> Any:
        await release.wait()
        return SimpleNamespace(pending_elicitations=[pending])

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
    from types import SimpleNamespace

    adapter, _, client, _ = _question_adapter()
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


def test_secret_questions_are_left_for_another_client() -> None:
    # Terminal input is echoed and saved to history, so a producer-marked
    # secret is never collected here and the elicitation stays pending.
    adapter, fis, client, approval_hook = _question_adapter()
    event = _question_event()
    event.ask_user_question = {
        "questions": [{"question": "Token?", "options": [], "isSecret": True}]
    }

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", event))
        await _until(lambda: task.done(), "a picker opened for a secret question")

    _run(_run_case())
    assert not fis.pending
    approval_hook.assert_not_awaited()
    client.sessions.resolve_elicitation.assert_not_awaited()
    assert "Answer it in the web UI" in _outputs_text(adapter._host)  # type: ignore[arg-type]


def test_form_accepted_elsewhere_during_its_approval_opens_no_fields() -> None:
    # Another client submits the form while its y/a/n card is open: the
    # terminal must not open stale fields, which would hold the prompt lock.
    adapter, fis, client, approval_hook = _question_adapter()

    async def _accepted_elsewhere(ctx: Any) -> bool:
        adapter.note_elicitation_resolved(ctx.elicitation_id)  # as run_repl does
        return True

    approval_hook.side_effect = _accepted_elsewhere
    form = _form_event("elicit_form", {"properties": {"name": {"type": "string"}}})

    async def _run_case() -> None:
        task = asyncio.create_task(adapter._handle_elicitation("conv_abc", form))
        await _until(lambda: task.done(), "stale form fields opened")
        # The lock is free: the next prompt still opens and gets answered.
        nxt = asyncio.create_task(adapter._handle_elicitation("conv_abc", _question_event()))
        await _until(lambda: fis.pending, "the next prompt never opened")
        fis.resolve("1")
        await nxt

    _run(_run_case())
    assert [c.args[1] for c in client.sessions.resolve_elicitation.await_args_list] == ["elicit_q"]


def test_editor_question_shows_its_prefill() -> None:
    from omnigent.repl._repl import _question_prompts

    questions = _question_prompts(
        {
            "questions": [
                {"id": "0", "question": "Notes?", "options": [], "textInput": {"default": "draft"}}
            ]
        }
    )
    fis = _FieldInputState()
    host = _FakeHost()
    adapter = _prompt_adapter(fis, host)
    assert _run(_feed_answers(fis, adapter._prompt_questions("e", questions), [""])) == {
        "0": "draft"
    }
    assert "prefilled: draft" in _outputs_text(host)
