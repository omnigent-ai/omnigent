from __future__ import annotations

import asyncio

import pytest
from omnigent_bot_core.events import (
    ElicitationOption,
    ElicitationQuestion,
    ElicitationRequest,
)
from omnigent_discord.approvals import (
    MAX_FORM_QUESTIONS,
    RESOLVED_EXTERNALLY,
    ElicitationCoordinator,
    ElicitationOutcome,
    Selections,
    Verdict,
    is_renderable,
    pending_card,
    question_options,
    resolve_form_answers,
    resolved_card,
)
from omnigent_discord.text import MAX_SELECT_OPTIONS

BINARY = ElicitationRequest(
    elicitation_id="el_1",
    message="Approve running `rm -rf /tmp/x`?",
    session_id="conv_1",
    content_preview="rm -rf /tmp/x",
)


def _form(*questions: ElicitationQuestion) -> ElicitationRequest:
    return ElicitationRequest(
        elicitation_id="el_2",
        message="Pick one",
        session_id="conv_1",
        questions=list(questions),
    )


def _question(key: str = "q1", *, options: int = 2, multi: bool = False) -> ElicitationQuestion:
    return ElicitationQuestion(
        key=key,
        question=f"Question {key}?",
        options=[ElicitationOption(label=f"opt-{i}") for i in range(options)],
        multi_select=multi,
    )


# ── coordinator ───────────────────────────────────────────────────────────


async def test_registered_waiter_receives_its_verdict() -> None:
    coordinator = ElicitationCoordinator(timeout_seconds=5)
    coordinator.register("conv_1", "el_1")
    waiter = asyncio.ensure_future(coordinator.await_verdict("conv_1", "el_1"))
    await asyncio.sleep(0)
    assert coordinator.resolve("conv_1", "el_1", Verdict(accepted=True)) is True
    assert await waiter == Verdict(accepted=True)


async def test_verdict_cannot_wake_another_sessions_waiter() -> None:
    # Keying on the (session, elicitation) pair keeps the authorization boundary
    # self-contained even if the server ever reused an elicitation id.
    coordinator = ElicitationCoordinator(timeout_seconds=5)
    coordinator.register("conv_A", "el_1")
    assert coordinator.resolve("conv_B", "el_1", Verdict(accepted=True)) is False


async def test_awaiting_without_an_answer_times_out_as_none() -> None:
    coordinator = ElicitationCoordinator(timeout_seconds=0.01)
    assert await coordinator.await_verdict("conv_1", "el_1") is None


async def test_external_resolution_wakes_the_waiter_with_the_sentinel() -> None:
    coordinator = ElicitationCoordinator(timeout_seconds=5)
    coordinator.register("conv_1", "el_1")
    waiter = asyncio.ensure_future(coordinator.await_verdict("conv_1", "el_1"))
    await asyncio.sleep(0)
    assert coordinator.resolve_external("conv_1", "el_1") is True
    assert await waiter is RESOLVED_EXTERNALLY


async def test_register_does_not_clobber_a_live_waiter() -> None:
    # A stream reconnect can replay the same elicitation request; the worker
    # already blocked on the original future must not be orphaned.
    coordinator = ElicitationCoordinator(timeout_seconds=5)
    coordinator.register("conv_1", "el_1")
    waiter = asyncio.ensure_future(coordinator.await_verdict("conv_1", "el_1"))
    await asyncio.sleep(0)
    coordinator.register("conv_1", "el_1")
    assert coordinator.resolve("conv_1", "el_1", Verdict(accepted=False)) is True
    assert await waiter == Verdict(accepted=False)


async def test_second_verdict_finds_no_waiter() -> None:
    coordinator = ElicitationCoordinator(timeout_seconds=5)
    coordinator.register("conv_1", "el_1")
    waiter = asyncio.ensure_future(coordinator.await_verdict("conv_1", "el_1"))
    await asyncio.sleep(0)
    coordinator.resolve("conv_1", "el_1", Verdict(accepted=True))
    await waiter
    assert coordinator.resolve("conv_1", "el_1", Verdict(accepted=True)) is False


async def test_unregister_drops_an_orphaned_waiter() -> None:
    coordinator = ElicitationCoordinator(timeout_seconds=5)
    coordinator.register("conv_1", "el_1")
    coordinator.unregister("conv_1", "el_1")
    assert coordinator.resolve("conv_1", "el_1", Verdict(accepted=True)) is False


# ── what Discord can render ───────────────────────────────────────────────


def test_binary_approval_is_renderable() -> None:
    assert is_renderable(BINARY) is True


def test_free_form_typed_input_is_not_renderable() -> None:
    # No component collects arbitrary text mid-turn, so it goes to the web UI.
    request = ElicitationRequest(
        elicitation_id="el_3",
        message="Name the branch",
        session_id="conv_1",
        needs_typed_input=True,
    )
    assert is_renderable(request) is False


def test_form_within_discords_row_budget_is_renderable() -> None:
    assert is_renderable(_form(*(_question(f"q{i}") for i in range(MAX_FORM_QUESTIONS))))


def test_form_past_the_row_budget_goes_to_the_web_ui() -> None:
    # A view holds five rows and Submit/Cancel takes one; rendering a subset
    # would round-trip a wrong answer to the agent.
    request = _form(*(_question(f"q{i}") for i in range(MAX_FORM_QUESTIONS + 1)))
    assert is_renderable(request) is False


def test_question_past_the_option_cap_goes_to_the_web_ui() -> None:
    request = _form(_question(options=MAX_SELECT_OPTIONS + 1))
    assert is_renderable(request) is False


# ── card content ──────────────────────────────────────────────────────────


def test_binary_card_shows_the_pending_action_as_a_code_block() -> None:
    card = pending_card(BINARY)
    assert "Approval needed" in card.title
    assert card.fields and "rm -rf /tmp/x" in card.fields[0][1]
    assert card.fields[0][1].startswith("```")


def test_binary_card_without_a_preview_has_no_field() -> None:
    request = ElicitationRequest(elicitation_id="e", message="ok?", session_id="c")
    assert pending_card(request).fields == ()


def test_form_card_leads_with_the_question() -> None:
    card = pending_card(_form(_question()))
    assert card.description == "Pick one"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ElicitationOutcome.APPROVED, "Approved"),
        (ElicitationOutcome.DENIED, "Denied"),
        (ElicitationOutcome.ANSWERED, "Answered"),
        (ElicitationOutcome.ANSWERED_ELSEWHERE, "Answered elsewhere"),
    ],
)
def test_resolved_card_names_its_outcome(outcome: ElicitationOutcome, expected: str) -> None:
    assert expected in resolved_card(BINARY, outcome=outcome).title


@pytest.mark.parametrize(
    "outcome",
    [
        ElicitationOutcome.TIMED_OUT,
        ElicitationOutcome.DELIVERY_FAILED,
        ElicitationOutcome.ABANDONED,
    ],
)
def test_outcomes_needing_a_retry_say_so(outcome: ElicitationOutcome) -> None:
    # Each of these leaves the user's request unhandled, so the card must tell
    # them re-sending starts a fresh attempt.
    assert "again to retry" in resolved_card(BINARY, outcome=outcome).description


# ── option values and answer mapping ──────────────────────────────────────


def test_options_carry_the_index_not_the_label() -> None:
    # Discord caps an option value at 100 chars but the agent needs the full
    # label, so the index travels and the label is looked up at resolve time.
    long_label = "L" * 300
    request = _form(
        ElicitationQuestion(key="q1", question="Q?", options=[ElicitationOption(label=long_label)])
    )
    ((label, value, _description),) = question_options(request)[0]
    assert value == "0"
    assert len(label) <= 100


def test_option_descriptions_are_carried_through() -> None:
    request = _form(
        ElicitationQuestion(
            key="q1",
            question="Q?",
            options=[ElicitationOption(label="eu-west-1", description="Ireland")],
        )
    )
    assert question_options(request)[0][0][2] == "Ireland"


def test_resolve_form_answers_maps_an_index_back_to_the_full_label() -> None:
    long_label = "L" * 300
    request = _form(
        ElicitationQuestion(
            key="q1",
            question="Q?",
            options=[ElicitationOption(label="short"), ElicitationOption(label=long_label)],
        )
    )
    assert resolve_form_answers(request, {"q1": "1"}) == {"q1": long_label}


def test_resolve_form_answers_handles_a_multi_select() -> None:
    request = _form(_question(options=3, multi=True))
    assert resolve_form_answers(request, {"q1": ["0", "2"]}) == {"q1": ["opt-0", "opt-2"]}


def test_resolve_form_answers_drops_an_unknown_question() -> None:
    assert resolve_form_answers(_form(_question()), {"nope": "0"}) == {}


def test_resolve_form_answers_drops_an_out_of_range_index() -> None:
    assert resolve_form_answers(_form(_question(options=2)), {"q1": "9"}) == {}


def test_resolve_form_answers_ignores_a_non_numeric_value() -> None:
    assert resolve_form_answers(_form(_question()), {"q1": "opt-0"}) == {}


def test_resolve_form_answers_of_nothing_is_empty() -> None:
    assert resolve_form_answers(_form(_question()), None) == {}


# ── selection accumulation ────────────────────────────────────────────────


def test_selections_record_a_single_choice_as_a_scalar() -> None:
    selections = Selections()
    selections.record("q1", ["2"], multi=False)
    assert selections.values == {"q1": "2"}


def test_selections_record_a_multi_choice_as_a_list() -> None:
    selections = Selections()
    selections.record("q1", ["0", "1"], multi=True)
    assert selections.values == {"q1": ["0", "1"]}


def test_clearing_a_multi_select_forgets_the_question() -> None:
    selections = Selections()
    selections.record("q1", ["0"], multi=True)
    selections.record("q1", [], multi=True)
    assert selections.values == {}


def test_missing_names_the_unanswered_questions() -> None:
    request = _form(_question("q1"), _question("q2"))
    selections = Selections()
    selections.record("q1", ["0"], multi=False)
    assert selections.missing(request) == ["Question q2?"]


def test_fence_in_the_preview_cannot_escape_the_code_block() -> None:
    # The preview is the action the agent wants to run, so its text is
    # substantially prompt-controlled. A raw triple backtick would close the
    # fence early and let the remainder render as live markdown — and, with a
    # mention in it, ping the server.
    hostile = ElicitationRequest(
        elicitation_id="e",
        message="ok?",
        session_id="c",
        content_preview="safe\n```\n@everyone see this",
    )
    body = pending_card(hostile).fields[0][1]
    assert body.startswith("```") and body.endswith("```")
    assert "```" not in body[3:-3]
