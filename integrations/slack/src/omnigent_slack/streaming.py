"""Streamed-answer machinery for a Slack turn.

``_LiveReply`` wraps Slack's ``chat.*Stream`` API (buffering, seal-for-ordering,
reopen-on-finalize). ``_AnswerReply`` layers the turn's answer semantics on top:
the "Working on it…" ack lifecycle, seal-⇒-forget across interruptions, and the
tail reconciliation that recovers a committed final item the deltas didn't carry.
Also home to the ``SlackClientProtocol``/``SlackStreamProtocol`` structural types
(the Slack-client surface the whole package depends on).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError

from omnigent_slack.models import ThreadKey
from omnigent_slack.text import truncate_for_slack


class SlackStreamProtocol(Protocol):
    async def append(self, *, markdown_text: str | None = ..., chunks: Any = ...) -> Any: ...

    async def stop(self, *, markdown_text: str | None = ...) -> Any: ...


class SlackClientProtocol(Protocol):
    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_getPermalink(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_stream(self, **kwargs: Any) -> SlackStreamProtocol: ...


# Slack streaming messages have a limited lifetime: after a stretch with no
# activity Slack finalizes the message itself, and any further append/stop then
# fails with this error. A long-running turn (waiting on a sub-agent, a slow
# tool) can outlast that window, so the bot opens a fresh streaming reply and
# continues into it rather than treating this as a turn failure.
_STREAM_CLOSED_ERROR = "message_not_in_streaming_state"


def _is_stream_closed_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, SlackApiError)
        and getattr(exc.response, "get", lambda _k: None)("error") == _STREAM_CLOSED_ERROR
    )


class _LiveReply:
    """A streaming Slack reply that reopens itself when Slack finalizes it.

    Slack finalizes a streaming message after an idle stretch, and a long turn
    (parked on a sub-agent, a slow tool) can outlast that window. When an
    append or stop hits ``message_not_in_streaming_state``, this opens a fresh
    streaming message in the same thread and continues, so the answer keeps
    streaming live across as many messages as the turn needs. The already-
    delivered messages stay intact — Slack has finalized them.
    """

    def __init__(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        *,
        recipient_user_id: str,
    ) -> None:
        self._client = client
        self._key = key
        self._recipient_user_id = recipient_user_id
        self._stream: SlackStreamProtocol | None = None
        # Number of streaming messages opened; >1 means the reply was split
        # because Slack closed an earlier segment mid-turn.
        self.segments = 0
        # Whether text has been appended but not yet flushed to Slack (the SDK
        # buffers until buffer_size). Lets ``flush`` skip an empty API call.
        self._pending_unflushed = False

    async def _open(self) -> SlackStreamProtocol:
        self._stream = await self._client.chat_stream(
            channel=self._key.channel_id,
            thread_ts=self._key.thread_ts,
            recipient_user_id=self._recipient_user_id,
            recipient_team_id=self._key.team_id,
        )
        self.segments += 1
        return self._stream

    async def append(self, markdown_text: str) -> bool:
        # The SDK buffers in memory and only calls Slack once the buffer fills,
        # returning a response on that flush and None while still buffering.
        # Return whether this append actually put text on screen so the caller
        # can hold the placeholder until the streamed message is visible.
        stream = self._stream or await self._open()
        try:
            flushed = await stream.append(markdown_text=markdown_text)
        except SlackApiError as exc:
            if not _is_stream_closed_error(exc):
                raise
            # Slack finalized the message out from under us; continue the answer
            # in a fresh streaming reply so nothing stalls or is lost.
            flushed = await (await self._open()).append(markdown_text=markdown_text)
        # Track buffered-but-unflushed text so ``flush`` can force it visible.
        self._pending_unflushed = flushed is None
        return flushed is not None

    async def flush(self) -> None:
        # Force any buffered-but-unflushed text onto the screen NOW, without
        # finalizing the segment. The SDK flushes its buffer when ``append`` is
        # called with ``chunks`` set (even an empty list), so a short answer
        # doesn't stay invisible until the segment is stopped. Used before an
        # out-of-band post so streamed text appears BEFORE the card/notice, not
        # coincident with it (matches the web UI's live reveal). No-op when
        # nothing is buffered or no stream is open.
        if self._stream is None or not self._pending_unflushed:
            return
        try:
            await self._stream.append(chunks=[])
        except SlackApiError as exc:
            if not _is_stream_closed_error(exc):
                raise
            # Segment was finalized under us; the buffered text already landed.
        self._pending_unflushed = False

    async def stop(self, markdown_text: str | None = None) -> None:
        # chat.stopStream rejects empty text, so only pass markdown_text when
        # there is some. Nothing ever streamed and no tail to deliver → no-op.
        if self._stream is None:
            if not markdown_text:
                return
            await self._open()
        try:
            await self._stop_current(markdown_text)
        except SlackApiError as exc:
            if not _is_stream_closed_error(exc):
                raise
            if markdown_text:
                await self._open()
                await self._stop_current(markdown_text)

    async def seal(self) -> None:
        """Finalize the current streaming segment so a later message sorts after it.

        Slack orders messages by the timestamp fixed when a streaming message
        opens, so text appended to a long-lived stream stays anchored there.
        Before posting any out-of-band message mid-turn (an approval card, a
        policy/file notice), seal the current answer segment: it ends here, the
        out-of-band message sorts after it, and the next append opens a fresh
        segment that sorts after *that* — keeping chronological order across an
        interruption. No-op when nothing is streaming.
        """
        if self._stream is None:
            return
        stream = self._stream
        # Drop the reference first so the next append opens a fresh segment even
        # if the stop below races a Slack-side finalize.
        self._stream = None
        self._pending_unflushed = False
        try:
            await stream.stop()
        except SlackApiError as exc:
            if not _is_stream_closed_error(exc):
                raise

    async def _stop_current(self, markdown_text: str | None) -> None:
        assert self._stream is not None
        if markdown_text:
            await self._stream.stop(markdown_text=markdown_text)
        else:
            await self._stream.stop()


class _AnswerReply:
    """Owns one turn's streamed answer: the live reply, the accumulated text,
    the "Working on it…" placeholder, and the interruption/finalization rules.

    Centralizes three invariants that were previously enforced by convention
    inside the turn loop:

    - **Placeholder visibility.** The ``ack`` is removed only once real content
      is on screen — the first append that actually flushes to Slack, or the
      finalizing ``stop()`` for a buffered answer — so the thread never shows a
      gap between the placeholder vanishing and the reply appearing.
    - **Seal ⇒ forget.** Sealing a segment before an out-of-band message
      (approval card, notice) also resets the accumulated text, so the tail
      reconciliation only ever considers the current segment.
    - **Tail reconciliation.** The final answer is whatever streamed; if the
      model reported a final item beyond the deltas, only the remainder is
      appended, and a no-delta answer falls back to the committed item.
    """

    def __init__(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        *,
        recipient_user_id: str,
        ack_ts: str | None,
        logger: logging.Logger,
    ) -> None:
        self._reply = _LiveReply(client, key, recipient_user_id=recipient_user_id)
        self._client = client
        self._key = key
        self._ack_ts = ack_ts
        self._logger = logger
        self._streamed = ""
        self._final: str | None = None
        # Text put on screen in each sealed segment this turn. Unlike
        # ``_streamed``/``_final`` (which reset at each seal), this survives
        # interruptions, so the no-delta fallback can tell whether the server's
        # newest assistant message is one we ALREADY showed (a trailing notice
        # sealed off an answer we streamed → don't re-post) from a genuinely new
        # message that never streamed (e.g. the post-elicitation answer arrived
        # only committed → DO recover it).
        self._delivered_texts: list[str] = []

    def set_ack(self, ack_ts: str | None) -> None:
        """Attach the placeholder ack posted after the reply was constructed.

        The ack is posted only after any session-config summary, so the thread
        reads metadata → "Working on it…" → answer. Once set, the ack is cleared
        by the same rules as if it had been passed at construction.
        """
        self._ack_ts = ack_ts

    @property
    def segments(self) -> int:
        return self._reply.segments

    @property
    def streamed_len(self) -> int:
        return len(self._streamed)

    async def add_delta(self, delta: str) -> None:
        # Append the delta; the SDK buffers and only flushes to Slack once the
        # buffer fills. Clear the placeholder only on the flush that actually
        # puts content on screen — never while still buffering — so there's no
        # empty gap.
        self._streamed += delta
        if await self._reply.append(delta):
            await self._clear_ack()

    def set_final(self, text: str) -> None:
        self._final = text

    async def seal_for_interruption(self) -> None:
        # Before an out-of-band message: reveal any buffered streamed text FIRST
        # (so it appears above the interruption as it did on screen in the web UI,
        # not coincident with the card), drop the placeholder (it would sit stale
        # above the interruption for the whole wait), finalize the current segment
        # so the interruption sorts after it, and forget the accumulated text so
        # the next segment reconciles independently. Record what this segment
        # delivered BEFORE resetting, so the fallback can recognize an
        # already-shown message and not re-post it.
        await self._reply.flush()
        shown = self._streamed + self._tail()
        if shown:
            self._delivered_texts.append(shown)
        await self._clear_ack()
        await self._reply.seal()
        self._streamed, self._final = "", None

    async def finalize(self, *, error_text: str | None) -> bool:
        # Deliver the answer tail, then clear the placeholder only after that
        # final flush (a short buffered answer becomes visible only at stop()).
        # Returns whether a real answer was delivered — when an error also
        # occurred, the caller posts the failure as a separate reply so the
        # answer stays intact; when nothing was produced, the error IS the reply.
        tail = self._tail()
        delivered_answer = bool(self._streamed or tail)
        if delivered_answer:
            await self._reply.stop(tail or None)
        else:
            await self._reply.stop(
                f"Omnigent request failed: {error_text}"
                if error_text
                else "Omnigent completed without returning response text."
            )
        await self._clear_ack()
        return delivered_answer

    def _tail(self) -> str:
        # The remainder of the committed final item beyond what already streamed.
        # ``startswith`` also covers the no-delta case (an empty ``_streamed`` is a
        # prefix of everything), so a committed-only answer returns in full.
        if self._final and self._final.startswith(self._streamed):
            return self._final[len(self._streamed) :]
        return ""

    def needs_fallback_text(self) -> bool:
        # True when the current (final) segment has no answer to deliver — the
        # caller may then recover the server's newest committed message. This is
        # a per-segment check; ``already_delivered`` guards against re-posting a
        # message an earlier sealed segment already showed.
        return not self._streamed and not self._tail()

    def already_delivered(self, text: str) -> bool:
        # Whether ``text`` matches something already put on screen this turn (a
        # sealed segment, or the current one). Lets the fallback distinguish a
        # message that already streamed but was sealed off by a trailing notice
        # (don't re-post) from one that never streamed (recover it).
        candidate = text.strip()
        if not candidate:
            return True
        shown = [*self._delivered_texts, self._streamed + self._tail()]
        return any(candidate == s.strip() for s in shown if s)

    def set_fallback_text(self, text: str) -> None:
        self._final = text

    async def stop_with(self, text: str) -> None:
        # Terminal notice (auth/unreachable/host errors, or a no-op abort): clear
        # the placeholder, then deliver ``text`` as a plain thread reply. Empty
        # text is a silent stop (nothing to say). A notice is not a streamed
        # answer, so it goes via a normal message, not the streaming reply.
        await self._clear_ack()
        if text:
            await self._client.chat_postMessage(
                channel=self._key.channel_id,
                thread_ts=self._key.thread_ts,
                text=truncate_for_slack(text),
            )

    async def _clear_ack(self) -> None:
        # Best-effort, idempotent: a failed delete must not abort the turn.
        if not self._ack_ts:
            return
        ack_ts, self._ack_ts = self._ack_ts, None
        try:
            await self._client.chat_delete(channel=self._key.channel_id, ts=ack_ts)
        except Exception:
            self._logger.warning("Ack delete failed thread=%s; continuing", self._key.display())
