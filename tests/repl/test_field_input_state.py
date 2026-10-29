"""Tests for ``_FieldInputState`` in ``omnigent.repl._repl``.

Covers the future-based field input collection used to prompt
schema fields interactively in the REPL.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.repl._repl import _FieldInputState, _SessionsChatReplAdapter


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
        ("4", {}, None),
        ("", {}, None),
        ("qa", {}, "qa"),
        ("qa", {"multi_select": True}, ["qa"]),
        ("qa", {"allow_custom": False}, None),
        ("", {"options": (), "text_default": "draft"}, "draft"),
        ("  keep spaces ", {"options": (), "text_default": "draft"}, "  keep spaces "),
    ],
    ids=[
        "number",
        "label-any-case",
        "multi-numbers",
        "multi-dedupes-in-order",
        "multi-label",
        "single-rejects-two",
        "out-of-range",
        "empty",
        "custom-text",
        "multi-custom-text",
        "custom-not-allowed",
        "editor-keeps-default",
        "editor-exact-text",
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
        _feed_answers(fis, adapter._prompt_questions("elicit_q", questions), ["9", "2", "1,2"])
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
    assert state.aborted and state.resolved_elsewhere and not state.pending


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
    assert state.resolved_elsewhere
