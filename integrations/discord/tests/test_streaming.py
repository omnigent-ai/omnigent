"""The edit-in-place reply: cadence, rollover, sealing, and finalization.

Discord has no streaming-message API, so these invariants are the Discord
integration's own — they replace everything the Slack SDK does server-side.
"""

from __future__ import annotations

import logging

import pytest
from fakes import RecordingChannel
from omnigent_discord.models import ChannelKey
from omnigent_discord.streaming import _AnswerReply, _LiveReply
from omnigent_discord.text import GENERIC_FAILURE_TEXT, MESSAGE_CHAR_LIMIT

KEY = ChannelKey(channel_id="500", guild_id="900")
ACK = "_Working on it…_"
LOGGER = logging.getLogger("test")


def _live(channel: RecordingChannel, interval: float = 0.0) -> _LiveReply:
    return _LiveReply(channel, KEY, placeholder=ACK, edit_interval_seconds=interval, logger=LOGGER)


def _answer(channel: RecordingChannel, interval: float = 0.0) -> _AnswerReply:
    return _AnswerReply(
        channel, KEY, placeholder=ACK, edit_interval_seconds=interval, logger=LOGGER
    )


# ── the placeholder ───────────────────────────────────────────────────────


async def test_reply_opens_as_the_placeholder() -> None:
    channel = RecordingChannel()
    reply = _live(channel)
    await reply.append("Hello")
    assert channel.sent[0].history[0] == ACK


async def test_placeholder_is_replaced_by_content_not_duplicated() -> None:
    # The reply streams INTO the acknowledgement, so the channel never carries
    # both a stale "Working on it…" and a separate answer message.
    channel = RecordingChannel()
    reply = _live(channel)
    await reply.append("Hello there")
    await reply.stop()
    assert channel.texts == ["Hello there"]


async def test_empty_turn_replaces_the_placeholder_with_a_notice() -> None:
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.acknowledge()  # nothing ever streams into it
    delivered = await reply.finalize(errored=False)
    assert delivered is False
    assert channel.texts == ["Omnigent completed without returning response text."]


async def test_failed_turn_with_no_answer_shows_the_generic_failure() -> None:
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.acknowledge()
    delivered = await reply.finalize(errored=True)
    assert delivered is False
    assert channel.texts == [GENERIC_FAILURE_TEXT]


# ── edit cadence ──────────────────────────────────────────────────────────


async def test_edits_are_throttled_to_the_cadence() -> None:
    # Editing on every delta would burn the per-channel rate limit and stall
    # the turn, so after the first write deltas accumulate between writes.
    channel = RecordingChannel()
    reply = _live(channel, interval=3600.0)
    for _ in range(50):
        await reply.append("word ")
    assert channel.sent[0].edits == 1
    assert reply.has_unflushed is True


async def test_first_content_is_written_immediately() -> None:
    # Whatever the cadence, the delta that replaces the placeholder must land
    # at once — holding it back leaves the channel looking stuck.
    channel = RecordingChannel()
    reply = _live(channel, interval=3600.0)
    await reply.append("first token")
    assert channel.sent[0].content == "first token"
    assert reply.has_unflushed is False


async def test_flush_forces_held_text_onto_the_screen() -> None:
    channel = RecordingChannel()
    reply = _live(channel, interval=3600.0)
    await reply.append("shown ")  # the first write always lands
    await reply.append("held back")
    assert reply.has_unflushed is True
    assert await reply.flush() is True
    assert channel.sent[0].content == "shown held back"
    assert reply.has_unflushed is False


async def test_flush_is_a_no_op_when_nothing_is_held() -> None:
    channel = RecordingChannel()
    reply = _live(channel, interval=3600.0)
    await reply.append("shown")
    await reply.flush()
    edits = channel.sent[0].edits
    assert await reply.flush() is False
    assert channel.sent[0].edits == edits


async def test_failed_edit_keeps_the_text_for_the_next_flush() -> None:
    # A transient rate-limit or network blip must lose no answer text.
    channel = RecordingChannel()
    reply = _live(channel, interval=0.0)
    await reply.append("first ")
    message = channel.sent[0]
    calls = {"n": 0}
    original = message.edit

    async def flaky_edit(**kwargs: object):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return await original(**kwargs)

    message.edit = flaky_edit  # type: ignore[method-assign]
    await reply.append("second")
    assert reply.has_unflushed is True
    await reply.flush()
    assert message.content == "first second"


# ── rollover at the character cap ─────────────────────────────────────────


async def test_long_answer_rolls_into_a_continuation_message() -> None:
    channel = RecordingChannel()
    reply = _live(channel)
    await reply.append("word " * 500)  # 2500 chars, past the 2000 cap
    await reply.stop()
    assert len(channel.live) == 2
    assert all(len(m.content or "") <= MESSAGE_CHAR_LIMIT for m in channel.live)
    assert channel.live[1].content.startswith("…")


async def test_rollover_splits_at_a_word_boundary() -> None:
    channel = RecordingChannel()
    reply = _live(channel)
    await reply.append("alpha " * 400)
    await reply.stop()
    first = channel.live[0].content or ""
    # A clean split leaves whole words, never a truncated "alph".
    assert first.endswith("alpha")


async def test_rollover_loses_no_text() -> None:
    channel = RecordingChannel()
    reply = _live(channel)
    body = " ".join(f"w{i:04d}" for i in range(600))
    await reply.append(body)
    await reply.stop()
    joined = " ".join(m.content.lstrip("…").strip() for m in channel.live)
    assert joined.split() == body.split()


# ── sealing for ordering ──────────────────────────────────────────────────


async def test_seal_finalizes_the_segment_so_a_later_post_sorts_after_it() -> None:
    # Discord orders by send time, so text edited in after a card would keep
    # appearing above it. Sealing ends the segment; the next append opens a new
    # message below the card.
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.add_delta("before the card")
    await reply.seal_for_interruption()
    await channel.send("[approval card]")
    await reply.add_delta("after the card")
    await reply.finalize(errored=False)
    assert [m.content for m in channel.live] == [
        "before the card",
        "[approval card]",
        "after the card",
    ]


async def test_seal_removes_a_placeholder_that_never_got_content() -> None:
    # Otherwise a stale "Working on it…" sits above the card for the whole wait.
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.acknowledge()
    assert channel.sent[0].content == ACK
    await reply.seal_for_interruption()
    assert channel.sent[0].deleted is True
    assert channel.live == []


async def test_held_text_is_revealed_before_the_interruption() -> None:
    # With a long cadence the text is still held when the card fires; the seal
    # must write it out first so it reads above the card, as it did on screen.
    channel = RecordingChannel()
    reply = _answer(channel, interval=3600.0)
    await reply.add_delta("short answer")
    await reply.seal_for_interruption()
    assert channel.live[0].content == "short answer"


async def test_answer_shown_before_a_seal_still_counts_as_delivered() -> None:
    # The common "answer streamed, then a trailing notice sealed it off"
    # sequence must not report "completed without returning response text".
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.add_delta("the answer")
    await reply.seal_for_interruption()
    delivered = await reply.finalize(errored=False)
    assert delivered is True
    assert channel.texts == ["the answer"]


# ── tail reconciliation ───────────────────────────────────────────────────


async def test_only_the_unstreamed_remainder_of_the_final_item_is_appended() -> None:
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.add_delta("Hello")
    reply.set_final("Hello, world.")
    await reply.finalize(errored=False)
    assert channel.texts == ["Hello, world."]


async def test_committed_only_answer_is_delivered_in_full() -> None:
    channel = RecordingChannel()
    reply = _answer(channel)
    reply.set_fallback_text("Committed answer.")
    delivered = await reply.finalize(errored=False)
    assert delivered is True
    assert channel.texts == ["Committed answer."]


async def test_rescoped_final_item_does_not_duplicate_the_stream() -> None:
    # When the final item is not a superset of what streamed, the stream wins —
    # appending it would show the answer twice.
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.add_delta("Streamed text.")
    reply.set_final("Something else entirely.")
    await reply.finalize(errored=False)
    assert channel.texts == ["Streamed text."]


async def test_needs_fallback_text_is_false_once_something_streamed() -> None:
    channel = RecordingChannel()
    reply = _answer(channel)
    assert reply.needs_fallback_text() is True
    await reply.add_delta("something")
    assert reply.needs_fallback_text() is False


async def test_already_delivered_recognizes_a_sealed_off_answer() -> None:
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.add_delta("the answer")
    await reply.seal_for_interruption()
    assert reply.already_delivered("the answer") is True
    assert reply.already_delivered("a different answer") is False


# ── message boundaries ────────────────────────────────────────────────────


async def test_new_assistant_message_gets_a_paragraph_break() -> None:
    # Native terminal harnesses emit several tagged messages per turn; without a
    # boundary they run together ("…once more.The credentials…").
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.add_delta("First message.", "m1")
    await reply.add_delta("Second message.", "m2")
    await reply.finalize(errored=False)
    assert channel.texts == ["First message.\n\nSecond message."]


async def test_id_less_streaming_gets_no_extra_breaks() -> None:
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.add_delta("one ")
    await reply.add_delta("two")
    await reply.finalize(errored=False)
    assert channel.texts == ["one two"]


# ── terminal notices ──────────────────────────────────────────────────────


async def test_stop_with_replaces_the_placeholder_with_the_notice() -> None:
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.acknowledge()
    await reply.stop_with("⚠️ Could not reach the server.")
    assert channel.texts == ["⚠️ Could not reach the server."]


async def test_stop_with_empty_text_removes_the_placeholder_silently() -> None:
    # Used when the reason is delivered elsewhere (the private re-login prompt).
    channel = RecordingChannel()
    reply = _answer(channel)
    await reply.acknowledge()
    await reply.stop_with("")
    assert channel.live == []


@pytest.mark.parametrize("interval", [0.0, 1.0])
async def test_segments_counts_the_messages_the_answer_occupied(interval: float) -> None:
    channel = RecordingChannel()
    reply = _answer(channel, interval=interval)
    await reply.add_delta("x" * 2500)
    await reply.finalize(errored=False)
    assert reply.segments == 2


async def test_split_with_nothing_left_over_posts_no_bare_continuation() -> None:
    # When everything past the break is whitespace the carry-over is empty;
    # opening a continuation then would post a bare "…" the next delta may
    # never fill.
    channel = RecordingChannel()
    reply = _live(channel)
    await reply.append("a" * 1799)
    await reply.append("  ")  # crosses the soft limit, but adds no content
    assert [m.content for m in channel.live] == ["a" * 1799]
