import asyncio
from typing import Any

from omnigent_slack.approvals import (
    ACTION_APPROVE,
    ACTION_DENY,
    ACTION_FORM_ANSWER,
    ACTION_FORM_SUBMIT,
    ClickTarget,
    ElicitationCoordinator,
    Verdict,
    elicitation_card_blocks,
    parse_action_value,
    parse_form_answers,
    resolve_form_answers,
    resolved_card_blocks,
    route_elicitation_click,
)
from omnigent_slack.omnigent import ElicitationOption, ElicitationQuestion, ElicitationRequest

# Thread owner used across click tests; the value carried on every control is
# "<owner> <session_id> <elicitation_id>" so a non-owner click can be rejected.
_OWNER = "U_owner"


class _RecordingSink:
    def __init__(self, delivered: bool = True) -> None:
        self.calls: list[tuple[str, Verdict]] = []
        self.rejections: list[ClickTarget] = []
        self._delivered = delivered

    async def handle_elicitation_action(self, *, elicitation_id: str, verdict: Verdict) -> bool:
        self.calls.append((elicitation_id, verdict))
        return self._delivered

    async def reject_non_owner_click(
        self, client: Any, body: dict[str, Any], target: ClickTarget
    ) -> None:
        self.rejections.append(target)


def _click_body(value: Any, *, user_id: str = _OWNER) -> dict[str, Any]:
    return {"actions": [{"value": value}], "user": {"id": user_id}}


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
            if coord.resolve("elicit_1", approved):
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
    assert coord.resolve("nope", Verdict(accepted=True)) is False


async def test_register_then_resolve_before_await_is_not_lost() -> None:
    # A click can arrive between posting the card and the worker awaiting. As
    # long as the future was registered first, the verdict is captured and the
    # subsequent await returns it (no lost wakeup).
    coord = ElicitationCoordinator()
    coord.register("elicit_1")
    approved = Verdict(accepted=True)
    assert coord.resolve("elicit_1", approved) is True  # click before await
    assert await coord.await_verdict("elicit_1") is approved


async def test_resolve_is_single_shot() -> None:
    coord = ElicitationCoordinator()
    waiter = asyncio.create_task(coord.await_verdict("elicit_1"))
    await asyncio.sleep(0.02)
    assert coord.resolve("elicit_1", Verdict(accepted=False)) is True
    # Second click finds the future already done → not delivered.
    assert coord.resolve("elicit_1", Verdict(accepted=True)) is False
    assert (await waiter).accepted is False


def test_binary_card_has_buttons_carrying_ids() -> None:
    blocks = elicitation_card_blocks(_binary(), _OWNER)
    actions = next(b for b in blocks if b["type"] == "actions")
    ids = {e["action_id"] for e in actions["elements"]}
    assert ids == {ACTION_APPROVE, ACTION_DENY}
    for element in actions["elements"]:
        # "<owner> <session_id> <elicitation_id>" — owner carried for the auth gate.
        assert element["value"] == f"{_OWNER} conv_1 elicit_1"
    assert any('{"name": "Edit"}' in str(b) for b in blocks)


def test_form_card_renders_inputs_per_question() -> None:
    blocks = elicitation_card_blocks(_form(), _OWNER)
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
    assert submit["value"] == f"{_OWNER} conv_1 elicit_form"


def test_parse_form_answers_single_and_multi() -> None:
    # Option values are indices (the label can exceed Slack's 75-char cap); they
    # are mapped back to labels later by resolve_form_answers.
    state_values = {
        "omnigent_q::store": {ACTION_FORM_ANSWER: {"selected_option": {"value": "0"}}},
        "omnigent_q::langs": {
            ACTION_FORM_ANSWER: {"selected_options": [{"value": "0"}, {"value": "1"}]}
        },
        # An unrelated block is ignored.
        "other": {"x": {}},
    }
    assert parse_form_answers(state_values) == {"store": "0", "langs": ["0", "1"]}


def test_parse_form_answers_omits_unanswered() -> None:
    state_values = {
        "omnigent_q::store": {ACTION_FORM_ANSWER: {"selected_option": None}},
        "omnigent_q::langs": {ACTION_FORM_ANSWER: {"selected_options": []}},
    }
    assert parse_form_answers(state_values) == {}


def test_resolve_form_answers_maps_indices_to_full_labels() -> None:
    # A label longer than Slack's 75-char option-value cap must round-trip to the
    # agent intact — carried by index, resolved back to the untruncated label.
    long_label = "A very long option label " * 5  # > 75 chars
    request = ElicitationRequest(
        elicitation_id="e",
        message="pick",
        session_id="c",
        questions=[
            ElicitationQuestion(
                key="store",
                question="where",
                options=[ElicitationOption(long_label), ElicitationOption("Memory")],
            ),
            ElicitationQuestion(
                key="langs",
                question="which",
                options=[ElicitationOption("Python"), ElicitationOption("Go")],
                multi_select=True,
            ),
        ],
    )
    raw = {"store": "0", "langs": ["0", "1"]}
    assert resolve_form_answers(request, raw) == {
        "store": long_label,
        "langs": ["Python", "Go"],
    }


def test_resolve_form_answers_drops_unknown_indices() -> None:
    request = ElicitationRequest(
        elicitation_id="e",
        message="pick",
        session_id="c",
        questions=[
            ElicitationQuestion(
                key="store",
                question="where",
                options=[ElicitationOption("Redis")],
            )
        ],
    )
    # Out-of-range / non-numeric indices are dropped; empty answer omits the key.
    assert resolve_form_answers(request, {"store": "9"}) == {}
    assert resolve_form_answers(request, {"store": ["9", "x"]}) == {}
    assert resolve_form_answers(request, None) == {}


def test_resolved_card_drops_controls() -> None:
    blocks = resolved_card_blocks(_binary(), outcome="Approved")
    assert not any(b.get("type") == "actions" for b in blocks)
    assert "Approved" in blocks[0]["text"]["text"]


def test_parse_action_value_roundtrip() -> None:
    assert parse_action_value(f"{_OWNER} conv_1 elicit_1") == ClickTarget(
        owner_user_id=_OWNER, session_id="conv_1", elicitation_id="elicit_1"
    )
    # An elicitation id may itself contain spaces — only the first two splits are
    # the owner and session; the remainder is the elicitation id.
    assert parse_action_value("U1 conv_1 elicit with spaces") == ClickTarget(
        owner_user_id="U1", session_id="conv_1", elicitation_id="elicit with spaces"
    )
    assert parse_action_value("conv_1 elicit_1") is None  # legacy 2-part value
    assert parse_action_value("malformed") is None
    assert parse_action_value("") is None


async def test_route_binary_click_forwards_verdict() -> None:
    sink = _RecordingSink()
    await route_elicitation_click(
        sink, None, _click_body(f"{_OWNER} conv_1 elicit_1"), accepted=True
    )
    assert len(sink.calls) == 1
    eid, verdict = sink.calls[0]
    assert eid == "elicit_1"
    assert verdict.accepted is True and verdict.content is None


async def test_route_form_submit_carries_answers() -> None:
    sink = _RecordingSink()
    body = {
        "actions": [{"value": f"{_OWNER} conv_1 elicit_form"}],
        "user": {"id": _OWNER},
        "state": {
            "values": {
                "omnigent_q::store": {ACTION_FORM_ANSWER: {"selected_option": {"value": "0"}}},
            }
        },
    }
    await route_elicitation_click(sink, None, body, accepted=True, is_form_submit=True)
    eid, verdict = sink.calls[0]
    assert eid == "elicit_form"
    assert verdict.accepted is True
    # Carried as an option index; resolved to the label later in the service.
    assert verdict.content == {"store": "0"}


async def test_route_form_cancel_is_decline_without_content() -> None:
    sink = _RecordingSink()
    body = {
        "actions": [{"value": f"{_OWNER} conv_1 elicit_form"}],
        "user": {"id": _OWNER},
        "state": {"values": {}},
    }
    await route_elicitation_click(sink, None, body, accepted=False, is_form_submit=True)
    _eid, verdict = sink.calls[0]
    assert verdict.accepted is False and verdict.content is None


async def test_route_click_ignores_malformed_body() -> None:
    sink = _RecordingSink()
    await route_elicitation_click(sink, None, {"actions": []}, accepted=True)
    await route_elicitation_click(sink, None, _click_body("no-space-value"), accepted=False)
    await route_elicitation_click(sink, None, _click_body(None), accepted=False)
    assert sink.calls == []
    assert sink.rejections == []


async def test_route_click_tolerates_stale_click() -> None:
    sink = _RecordingSink(delivered=False)
    await route_elicitation_click(
        sink, None, _click_body(f"{_OWNER} conv_1 elicit_1"), accepted=True
    )
    assert len(sink.calls) == 1  # attempted; sink reported no waiter


async def test_route_rejects_non_owner_click() -> None:
    # A click from anyone but the thread owner is rejected before any verdict is
    # delivered — the card is visible channel-wide but only the owner can act.
    sink = _RecordingSink()
    body = _click_body(f"{_OWNER} conv_1 elicit_1", user_id="U_intruder")
    await route_elicitation_click(sink, None, body, accepted=True)
    assert sink.calls == []
    assert sink.rejections == [
        ClickTarget(owner_user_id=_OWNER, session_id="conv_1", elicitation_id="elicit_1")
    ]


async def test_route_owner_click_is_accepted() -> None:
    sink = _RecordingSink()
    body = _click_body(f"{_OWNER} conv_1 elicit_1", user_id=_OWNER)
    await route_elicitation_click(sink, None, body, accepted=True)
    assert len(sink.calls) == 1
    assert sink.rejections == []
