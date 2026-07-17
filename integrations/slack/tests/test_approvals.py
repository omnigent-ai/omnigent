import asyncio
from typing import Any

from omnigent_slack.approvals import (
    ACTION_APPROVE,
    ACTION_DENY,
    ACTION_FORM_ANSWER,
    ACTION_FORM_SUBMIT,
    ElicitationCoordinator,
    Verdict,
    elicitation_card_blocks,
    parse_action_value,
    parse_form_answers,
    resolved_card_blocks,
    route_elicitation_click,
)
from omnigent_slack.omnigent import ElicitationOption, ElicitationQuestion, ElicitationRequest


class _RecordingSink:
    def __init__(self, delivered: bool = True) -> None:
        self.calls: list[tuple[str, Verdict]] = []
        self._delivered = delivered

    async def handle_elicitation_action(self, *, elicitation_id: str, verdict: Verdict) -> bool:
        self.calls.append((elicitation_id, verdict))
        return self._delivered


def _click_body(value: Any) -> dict[str, Any]:
    return {"actions": [{"value": value}]}


def _binary() -> ElicitationRequest:
    return ElicitationRequest(
        elicitation_id="elicit_1",
        message="Approve Edit()?",
        session_id="conv_1",
        policy_name="approve_edits",
        content_preview='{"name": "Edit"}',
    )


def _form() -> ElicitationRequest:
    return ElicitationRequest(
        elicitation_id="elicit_form",
        message="A couple of questions",
        session_id="conv_1",
        questions=[
            ElicitationQuestion(
                key="store",
                question="Where should it store data?",
                options=[ElicitationOption("Redis"), ElicitationOption("Memory")],
            ),
            ElicitationQuestion(
                key="langs",
                question="Which languages?",
                options=[ElicitationOption("Python"), ElicitationOption("Go")],
                multi_select=True,
            ),
        ],
    )


async def test_coordinator_delivers_verdict_to_waiter() -> None:
    coord = ElicitationCoordinator()
    approved = Verdict(accepted=True)

    async def click() -> None:
        for _ in range(50):
            if await coord.resolve("elicit_1", approved):
                return
            await asyncio.sleep(0.01)

    task = asyncio.create_task(click())
    verdict = await coord.await_verdict("elicit_1")
    await task
    assert verdict is approved


async def test_coordinator_times_out_to_none() -> None:
    coord = ElicitationCoordinator(timeout_seconds=0.05)
    assert await coord.await_verdict("elicit_1") is None


async def test_resolve_without_waiter_returns_false() -> None:
    coord = ElicitationCoordinator()
    assert await coord.resolve("nope", Verdict(accepted=True)) is False


async def test_resolve_is_single_shot() -> None:
    coord = ElicitationCoordinator()
    waiter = asyncio.create_task(coord.await_verdict("elicit_1"))
    await asyncio.sleep(0.02)
    assert await coord.resolve("elicit_1", Verdict(accepted=False)) is True
    # Second click finds the future already done → not delivered.
    assert await coord.resolve("elicit_1", Verdict(accepted=True)) is False
    assert (await waiter).accepted is False


def test_binary_card_has_buttons_carrying_ids() -> None:
    blocks = elicitation_card_blocks(_binary())
    actions = next(b for b in blocks if b["type"] == "actions")
    ids = {e["action_id"] for e in actions["elements"]}
    assert ids == {ACTION_APPROVE, ACTION_DENY}
    for element in actions["elements"]:
        assert element["value"] == "conv_1 elicit_1"
    assert any('{"name": "Edit"}' in str(b) for b in blocks)


def test_form_card_renders_inputs_per_question() -> None:
    blocks = elicitation_card_blocks(_form())
    # One input block per question, keyed so the submit handler can map answers.
    inputs = {
        b["block_id"]: b["accessory"]["type"]
        for b in blocks
        if isinstance(b.get("block_id"), str) and b["block_id"].startswith("omnigent_q::")
    }
    assert inputs == {"omnigent_q::store": "radio_buttons", "omnigent_q::langs": "checkboxes"}
    # A Submit carrying the resolve target.
    actions = next(b for b in blocks if b["type"] == "actions")
    submit = next(e for e in actions["elements"] if e["action_id"] == ACTION_FORM_SUBMIT)
    assert submit["value"] == "conv_1 elicit_form"


def test_parse_form_answers_single_and_multi() -> None:
    state_values = {
        "omnigent_q::store": {ACTION_FORM_ANSWER: {"selected_option": {"value": "Redis"}}},
        "omnigent_q::langs": {
            ACTION_FORM_ANSWER: {"selected_options": [{"value": "Python"}, {"value": "Go"}]}
        },
        # An unrelated block is ignored.
        "other": {"x": {}},
    }
    assert parse_form_answers(state_values) == {"store": "Redis", "langs": ["Python", "Go"]}


def test_parse_form_answers_omits_unanswered() -> None:
    state_values = {
        "omnigent_q::store": {ACTION_FORM_ANSWER: {"selected_option": None}},
        "omnigent_q::langs": {ACTION_FORM_ANSWER: {"selected_options": []}},
    }
    assert parse_form_answers(state_values) == {}


def test_resolved_card_drops_controls() -> None:
    blocks = resolved_card_blocks(_binary(), outcome="Approved")
    assert not any(b.get("type") == "actions" for b in blocks)
    assert "Approved" in blocks[0]["text"]["text"]


def test_parse_action_value_roundtrip() -> None:
    assert parse_action_value("conv_1 elicit_1") == ("conv_1", "elicit_1")
    assert parse_action_value("malformed") is None
    assert parse_action_value("") is None


async def test_route_binary_click_forwards_verdict() -> None:
    sink = _RecordingSink()
    await route_elicitation_click(sink, _click_body("conv_1 elicit_1"), accepted=True)
    assert len(sink.calls) == 1
    eid, verdict = sink.calls[0]
    assert eid == "elicit_1"
    assert verdict.accepted is True and verdict.content is None


async def test_route_form_submit_carries_answers() -> None:
    sink = _RecordingSink()
    body = {
        "actions": [{"value": "conv_1 elicit_form"}],
        "state": {
            "values": {
                "omnigent_q::store": {ACTION_FORM_ANSWER: {"selected_option": {"value": "Redis"}}},
            }
        },
    }
    await route_elicitation_click(sink, body, accepted=True, is_form_submit=True)
    eid, verdict = sink.calls[0]
    assert eid == "elicit_form"
    assert verdict.accepted is True
    assert verdict.content == {"store": "Redis"}


async def test_route_form_cancel_is_decline_without_content() -> None:
    sink = _RecordingSink()
    body = {"actions": [{"value": "conv_1 elicit_form"}], "state": {"values": {}}}
    await route_elicitation_click(sink, body, accepted=False, is_form_submit=True)
    _eid, verdict = sink.calls[0]
    assert verdict.accepted is False and verdict.content is None


async def test_route_click_ignores_malformed_body() -> None:
    sink = _RecordingSink()
    await route_elicitation_click(sink, {"actions": []}, accepted=True)
    await route_elicitation_click(sink, _click_body("no-space-value"), accepted=False)
    await route_elicitation_click(sink, _click_body(None), accepted=False)
    assert sink.calls == []


async def test_route_click_tolerates_stale_click() -> None:
    sink = _RecordingSink(delivered=False)
    await route_elicitation_click(sink, _click_body("conv_1 elicit_1"), accepted=True)
    assert len(sink.calls) == 1  # attempted; sink reported no waiter
