"""The pure-push elicitation lifecycle.

The turn loop never blocks on a card: it posts, spawns a resolver, and keeps
reading. Every outcome below is one way that dance can end, and each is
labelled distinctly on the card so the user knows whether their answer actually
reached the server.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fakes import FakeOmnigent, RecordingChannel
from omnigent_bot_core.events import (
    ElicitationOption,
    ElicitationQuestion,
    ElicitationRequest,
)
from omnigent_discord.approvals import (
    ElicitationCoordinator,
    ElicitationOutcome,
    Verdict,
)
from omnigent_discord.elicitation import ElicitationController, ElicitationTurnState
from omnigent_discord.models import ChannelKey, DiscordTurn

SERVER = "https://omnigent.example.com"
KEY = ChannelKey(channel_id="500", guild_id="900")
LOGGER = logging.getLogger("test")

BINARY = ElicitationRequest(
    elicitation_id="el_1",
    message="Approve running `rm -rf /tmp/x`?",
    session_id="conv_1",
    content_preview="rm -rf /tmp/x",
)
FORM = ElicitationRequest(
    elicitation_id="el_2",
    message="Which region?",
    session_id="conv_1",
    questions=[
        ElicitationQuestion(
            key="region",
            question="Region?",
            options=[ElicitationOption("us-east-1"), ElicitationOption("eu-west-1")],
        )
    ],
)


def _turn(channel: RecordingChannel, owner: str = "u1") -> DiscordTurn:
    return DiscordTurn(
        key=KEY,
        text="do the thing",
        user_id=owner,
        create_if_missing=False,
        title="",
        channel=channel,
        agent_id="",
        owner_user_id=owner,
    )


def _controller(
    coordinator: ElicitationCoordinator, channel: RecordingChannel, timeout: float = 5.0
) -> ElicitationController:
    async def post_reply(target: RecordingChannel, text: str) -> None:
        await target.send(text)

    return ElicitationController(
        coordinator,
        server_url=SERVER,
        post_reply=post_reply,
        logger=LOGGER,
        timeout_seconds=timeout,
    )


def _card(channel: RecordingChannel):
    return next(m for m in channel.sent if m.embed is not None)


# ── posting ───────────────────────────────────────────────────────────────


async def test_binary_request_posts_a_card_without_blocking() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(5), channel)
    state = ElicitationTurnState()
    await controller.start(client, _turn(channel), BINARY, state)
    card = _card(channel)
    assert "Approval needed" in card.embed.title
    assert card.view is not None
    assert "el_1" in state.pending
    await controller.shutdown()


async def test_unrenderable_request_links_to_the_web_ui_and_posts_no_card() -> None:
    # Free-form typed input can't be collected with components, so the turn
    # stays alive and the user answers where it can be.
    request = ElicitationRequest(
        elicitation_id="el_3",
        message="Name the branch",
        session_id="conv_1",
        needs_typed_input=True,
    )
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(5), channel)
    state = ElicitationTurnState()
    await controller.start(client, _turn(channel), request, state)
    assert f"{SERVER}/approve/conv_1/el_3" in channel.texts[0]
    assert state.pending == {}


async def test_replayed_request_does_not_post_a_second_card() -> None:
    # A stream reconnect can replay the elicitation; a second card would orphan
    # the first and leave two sets of live buttons.
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(5), channel)
    state = ElicitationTurnState()
    await controller.start(client, _turn(channel), BINARY, state)
    await controller.start(client, _turn(channel), BINARY, state)
    assert len([m for m in channel.sent if m.embed is not None]) == 1
    await controller.shutdown()


async def test_card_that_cannot_be_posted_is_declined_server_side() -> None:
    # Otherwise the server stays parked and every later message deflects as
    # "needs action" with no card to answer.
    channel, client = RecordingChannel(), FakeOmnigent()
    channel.fail_next_send = True
    coordinator = ElicitationCoordinator(5)
    controller = _controller(coordinator, channel)
    state = ElicitationTurnState()
    await controller.start(client, _turn(channel), BINARY, state)
    assert client.resolved == [
        {
            "session_id": "conv_1",
            "elicitation_id": "el_1",
            "accepted": False,
            "content": None,
        }
    ]
    assert state.pending == {}
    # The orphaned waiter is dropped rather than left in the coordinator.
    assert coordinator.resolve("conv_1", "el_1", Verdict(accepted=True)) is False


# ── answering ─────────────────────────────────────────────────────────────


async def test_approval_is_posted_then_the_card_shows_approved() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    coordinator = ElicitationCoordinator(5)
    controller = _controller(coordinator, channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)

    coordinator.resolve("conv_1", "el_1", Verdict(accepted=True))
    await asyncio.sleep(0)  # let the resolver POST
    await controller.on_resolved(turn, "el_1", state)

    assert client.resolved[0]["accepted"] is True
    card = _card(channel)
    assert card.embed.title.endswith(ElicitationOutcome.APPROVED.value)
    assert card.view is None  # the controls are gone


async def test_denial_shows_denied() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    coordinator = ElicitationCoordinator(5)
    controller = _controller(coordinator, channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)
    coordinator.resolve("conv_1", "el_1", Verdict(accepted=False))
    await asyncio.sleep(0)
    await controller.on_resolved(turn, "el_1", state)
    assert client.resolved[0]["accepted"] is False
    assert _card(channel).embed.title.endswith(ElicitationOutcome.DENIED.value)


async def test_form_submit_sends_full_labels_not_indices() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    coordinator = ElicitationCoordinator(5)
    controller = _controller(coordinator, channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, FORM, state)
    coordinator.resolve("conv_1", "el_2", Verdict(accepted=True, content={"region": "1"}))
    await asyncio.sleep(0)
    await controller.on_resolved(turn, "el_2", state)
    assert client.resolved[0]["content"] == {"region": "eu-west-1"}
    assert _card(channel).embed.title.endswith(ElicitationOutcome.ANSWERED.value)


async def test_form_cancel_shows_cancelled() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    coordinator = ElicitationCoordinator(5)
    controller = _controller(coordinator, channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, FORM, state)
    coordinator.resolve("conv_1", "el_2", Verdict(accepted=False))
    await asyncio.sleep(0)
    await controller.on_resolved(turn, "el_2", state)
    assert _card(channel).embed.title.endswith(ElicitationOutcome.CANCELLED.value)


async def test_answer_in_the_web_ui_posts_no_verdict_of_our_own() -> None:
    # The server already has an answer; posting ours would race it.
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(5), channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)
    await controller.on_resolved(turn, "el_1", state)
    assert client.resolved == []
    assert _card(channel).embed.title.endswith(ElicitationOutcome.ANSWERED_ELSEWHERE.value)


async def test_finalizing_twice_edits_the_card_once() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(5), channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)
    await controller.on_resolved(turn, "el_1", state)
    edits = _card(channel).edits
    await controller.on_resolved(turn, "el_1", state)
    assert _card(channel).edits == edits


# ── the failure outcomes ──────────────────────────────────────────────────


async def test_no_answer_in_time_declines_and_says_it_timed_out() -> None:
    # A parked card blocks every later message in the channel, so the wait is
    # bounded and the decline releases the server-side park.
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(0.01), channel, timeout=0.01)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)
    await asyncio.sleep(0.05)
    await controller.on_resolved(turn, "el_1", state)
    assert client.resolved[0]["accepted"] is False
    assert _card(channel).embed.title.endswith(ElicitationOutcome.TIMED_OUT.value)


async def test_verdict_the_server_never_received_is_not_shown_as_approved() -> None:
    # Saying "Approved" for an answer that never landed would be a lie: the
    # server is still parked and the turn cannot continue.
    channel, client = RecordingChannel(), FakeOmnigent()
    client.resolve_error = RuntimeError("network down")
    coordinator = ElicitationCoordinator(5)
    controller = _controller(coordinator, channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)
    coordinator.resolve("conv_1", "el_1", Verdict(accepted=True))
    await asyncio.sleep(0)
    await controller.on_resolved(turn, "el_1", state)
    card = _card(channel)
    assert card.embed.title.endswith(ElicitationOutcome.DELIVERY_FAILED.value)
    assert "again to retry" in card.embed.description


async def test_card_open_at_turn_end_is_declined_and_marked_abandoned() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(60), channel, timeout=60)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)
    await controller.finish_pending(client, turn, state)
    assert client.resolved[0]["accepted"] is False
    assert _card(channel).embed.title.endswith(ElicitationOutcome.ABANDONED.value)


async def test_finish_pending_leaves_an_already_finalized_card_alone() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    coordinator = ElicitationCoordinator(5)
    controller = _controller(coordinator, channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)
    coordinator.resolve("conv_1", "el_1", Verdict(accepted=True))
    await asyncio.sleep(0)
    await controller.on_resolved(turn, "el_1", state)
    posted = len(client.resolved)
    await controller.finish_pending(client, turn, state)
    assert len(client.resolved) == posted


async def test_timed_out_decline_that_also_failed_is_re_declined() -> None:
    # Nothing reached the server, so the park is still open. Re-declining is the
    # only way the session isn't wedged, and the label still matches the state.
    channel, client = RecordingChannel(), FakeOmnigent()
    client.resolve_error = RuntimeError("network down")
    coordinator = ElicitationCoordinator(0.01)
    controller = _controller(coordinator, channel, timeout=0.01)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)
    await asyncio.sleep(0.05)
    client.resolve_error = None
    await controller.finish_pending(client, turn, state)
    assert client.resolved and client.resolved[-1]["accepted"] is False
    assert _card(channel).embed.title.endswith(ElicitationOutcome.DELIVERY_FAILED.value)


async def test_shutdown_cancels_a_resolver_still_waiting() -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(60), channel, timeout=60)
    state = ElicitationTurnState()
    await controller.start(client, _turn(channel), BINARY, state)
    resolver = state.pending["el_1"].resolver
    assert resolver is not None
    await controller.shutdown()
    assert resolver.done()


@pytest.mark.parametrize("failure", [RuntimeError("boom")])
async def test_failed_card_edit_never_aborts_the_turn(failure: Exception) -> None:
    channel, client = RecordingChannel(), FakeOmnigent()
    controller = _controller(ElicitationCoordinator(5), channel)
    state, turn = ElicitationTurnState(), _turn(channel)
    await controller.start(client, turn, BINARY, state)

    async def broken_edit(**_kwargs: object) -> None:
        raise failure

    _card(channel).edit = broken_edit  # type: ignore[method-assign]
    await controller.on_resolved(turn, "el_1", state)  # must not raise
