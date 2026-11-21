"""Streamed-answer machinery for a Discord turn.

Discord has no streaming-message API. Where the Slack integration hands deltas
to ``chat.appendStream`` and lets Slack own buffering, here a reply is **one
message edited in place**: :class:`_LiveReply` accumulates deltas, edits the
message on a fixed cadence, and rolls into a continuation message when the
2000-character cap is reached. :class:`_AnswerReply` layers the turn's answer
semantics on top — the "Working on it…" placeholder lifecycle, seal-⇒-forget
across interruptions, and the tail reconciliation that recovers a committed
final item the deltas didn't carry.

Also home to the structural types the whole package depends on
(:class:`MessageProtocol` / :class:`MessageableProtocol`), so nothing outside
``app.py`` and ``views.py`` imports ``discord``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from omnigent_discord.models import ChannelKey
from omnigent_discord.text import (
    GENERIC_FAILURE_TEXT,
    MESSAGE_CHAR_LIMIT,
    truncate_for_message,
)


class MessageProtocol(Protocol):
    """The bits of a sent ``discord.Message`` this package uses."""

    id: int

    async def edit(self, **kwargs: Any) -> Any: ...

    async def delete(self) -> None: ...


class MessageableProtocol(Protocol):
    """The bits of a ``discord.abc.Messageable`` this package uses."""

    async def send(self, content: str | None = None, **kwargs: Any) -> MessageProtocol: ...


# How much of a message's 2000-char budget the live reply will fill before
# rolling into a continuation. The headroom absorbs the delta that crosses the
# boundary without a mid-token cut, and leaves room for the continuation marker.
_SEGMENT_SOFT_LIMIT = MESSAGE_CHAR_LIMIT - 200

# Prefix on every continuation message, so a reader can tell a rolled-over
# answer from a fresh one.
_CONTINUATION_PREFIX = "…"


def _split_at_break(text: str, limit: int) -> tuple[str, str]:
    """Split ``text`` at the last clean break at or before ``limit``.

    Prefers a paragraph break, then a line break, then a space, so a rolled-over
    answer doesn't cut mid-word. Falls back to a hard cut when a single run has
    no break in the window (a URL, a long token). ``text`` shorter than ``limit``
    returns ``(text, "")``.
    """
    if len(text) <= limit:
        return text, ""
    window = text[:limit]
    cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip(), text[cut:].lstrip()


class _LiveReply:
    """A Discord message the answer is streamed into by repeated edits.

    Owns three concerns Discord forces on a streamed reply that Slack's
    streaming API handles server-side:

    - **Edit cadence.** Message edits are rate-limited per channel, so deltas
      accumulate in memory and are written at most once per
      ``edit_interval_seconds``. A forced :meth:`flush` bypasses the cadence
      (used before an out-of-band post, and when the stream goes quiet).
    - **The 2000-character cap.** When the current message would overflow, it is
      finalized at a clean break and the remainder continues in a new message.
    - **The placeholder.** The message opens showing the caller's placeholder
      text and is overwritten by the first real content, so the reply appears
      where the acknowledgement was rather than as a second message.
    """

    def __init__(
        self,
        channel: MessageableProtocol,
        key: ChannelKey,
        *,
        placeholder: str,
        edit_interval_seconds: float,
        logger: logging.Logger,
    ) -> None:
        self._channel = channel
        self._key = key
        self._placeholder = placeholder
        self._interval = edit_interval_seconds
        self._logger = logger
        self._message: MessageProtocol | None = None
        # Text of the CURRENT message (excluding any continuation prefix).
        self._current = ""
        # Text written to the current message by the last successful edit. When
        # it differs from the rendered body there is unwritten content.
        self._rendered: str | None = None
        # Whether the open message continues an answer that overflowed the
        # character cap (as opposed to starting a fresh segment after a seal).
        self._continuation = False
        # Monotonic time of the last edit, for the cadence check.
        self._last_edit = 0.0
        # Number of messages opened; >1 means the answer was split.
        self.segments = 0

    @property
    def has_unflushed(self) -> bool:
        """Whether accumulated text has not yet been written to Discord."""
        return self._message is not None and self._body() != self._rendered

    async def _open(self, *, continuation: bool) -> MessageProtocol:
        """Send a new message for the reply to stream into."""
        self._continuation = continuation
        self._current = ""
        body = self._body()
        self._message = await self._channel.send(body)
        self._rendered = body
        # The first content after opening is written immediately, whatever the
        # cadence: it is what replaces the placeholder, and holding it back
        # would leave the channel looking stuck.
        self._last_edit = float("-inf")
        self.segments += 1
        return self._message

    async def ensure_open(self) -> None:
        """Post the placeholder now, before any content exists.

        Called once the session is established so the acknowledgement is on
        screen for the whole wait before the first token — the reply is then
        streamed into this same message.
        """
        if self._message is None:
            await self._open(continuation=False)

    def _body(self) -> str:
        """The full content to write for the current message."""
        if not self._current:
            return _CONTINUATION_PREFIX if self._continuation else self._placeholder
        prefix = _CONTINUATION_PREFIX if self._continuation else ""
        return truncate_for_message(f"{prefix}{self._current}")

    async def append(self, delta: str) -> bool:
        """Add ``delta`` to the reply, editing Discord if the cadence allows.

        Returns whether this call actually put content on screen, so the caller
        can drop the placeholder only once the reply is really visible.
        """
        if not delta:
            return False
        if self._message is None:
            await self._open(continuation=False)
        # Roll into a continuation message when the current one would overflow.
        # The head is written and finalized first so the split is visible in
        # order, then the tail starts the new message.
        if len(self._current) + len(delta) > _SEGMENT_SOFT_LIMIT:
            head, tail = _split_at_break(self._current + delta, _SEGMENT_SOFT_LIMIT)
            self._current = head
            await self._write()
            # An exact fit leaves nothing to carry over: opening a continuation
            # now would post a bare "…" that the next delta may never fill.
            # This message is already full, so the next append splits again.
            if not tail:
                return True
            await self._open(continuation=True)
            self._current = tail
            await self._write()
            return True
        self._current += delta
        if self._due():
            await self._write()
            return True
        return False

    def _due(self) -> bool:
        """Whether enough time has passed to spend another edit."""
        now = asyncio.get_running_loop().time()
        return now - self._last_edit >= self._interval

    async def _write(self) -> None:
        """Edit the current message to the accumulated text (best-effort).

        A failed edit is logged and the text kept: the next cadence tick or the
        finalizing flush retries it, so a transient rate-limit or network blip
        loses no answer text.
        """
        message = self._message
        if message is None:
            return
        body = self._body()
        if body == self._rendered:
            return
        try:
            await message.edit(content=body)
        except Exception:
            self._logger.warning(
                "Reply edit failed channel=%s; will retry on next flush", self._key.display()
            )
            return
        self._rendered = body
        self._last_edit = asyncio.get_running_loop().time()

    async def flush(self) -> bool:
        """Force accumulated text onto the screen now, ignoring the cadence.

        Used before an out-of-band post (so streamed text appears above the
        card, not coincident with it) and when the stream goes quiet. Returns
        whether anything was written.
        """
        if not self.has_unflushed:
            return False
        await self._write()
        return True

    async def seal(self) -> None:
        """Finalize the current message so a later post sorts after it.

        Discord orders messages by send time, so text edited into a live
        message after an out-of-band post keeps appearing *above* it. Before
        posting any such message mid-turn (an approval card, a policy/file
        notice), seal the current segment: it ends here, the out-of-band
        message sorts after it, and the next append opens a fresh message that
        sorts after *that* — keeping chronological order across the
        interruption.

        A segment that never received real content is deleted rather than left
        showing a stale placeholder above the interruption. No-op when nothing
        is open.
        """
        if self._message is None:
            return
        if self._current:
            # Write the segment out one last time, then let go of it: the next
            # append opens a fresh (non-continuation) message.
            await self._write()
            self._message = None
            self._current = ""
            self._rendered = None
            self._continuation = False
            return
        # Nothing was ever streamed into this segment — remove the placeholder.
        message, self._message = self._message, None
        self._rendered = None
        self._continuation = False
        try:
            await message.delete()
        except Exception:
            self._logger.warning(
                "Placeholder delete failed channel=%s; continuing", self._key.display()
            )

    async def stop(self, tail: str | None = None) -> None:
        """Deliver any tail and write the reply out one last time.

        ``tail`` is the remainder of a committed final item the deltas didn't
        carry. When nothing ever streamed and there is no tail this is a no-op —
        the caller decides what (if anything) the empty reply should say.
        """
        if tail:
            await self.append(tail)
        if self._message is None:
            return
        await self._write()

    async def replace_with(self, text: str) -> None:
        """Overwrite the open message with ``text`` (a notice, not an answer).

        Reuses the placeholder message so a failed turn leaves one message
        saying what happened, rather than a stale "Working on it…" plus a
        separate notice. Sends a new message when nothing is open.
        """
        body = truncate_for_message(text)
        if self._message is None:
            self._message = await self._channel.send(body)
            self.segments += 1
        else:
            try:
                await self._message.edit(content=body)
            except Exception:
                self._logger.warning(
                    "Notice edit failed channel=%s; continuing", self._key.display()
                )
        self._rendered = body
        self._current = ""
        self._continuation = False

    async def discard_placeholder(self) -> None:
        """Delete the open message if it never received content.

        Used when the reason for stopping is delivered elsewhere (an
        ephemeral re-login prompt), so the channel isn't left with a stale
        "Working on it…".
        """
        if self._message is None or self._current:
            return
        message, self._message = self._message, None
        try:
            await message.delete()
        except Exception:
            self._logger.warning(
                "Placeholder delete failed channel=%s; continuing", self._key.display()
            )


class _AnswerReply:
    """Owns one turn's streamed answer and the interruption/finalization rules.

    Centralizes three invariants:

    - **Placeholder visibility.** The reply opens as a "Working on it…"
      message and is overwritten by the first real content, so the channel
      never shows a gap between the placeholder vanishing and the reply
      appearing.
    - **Seal ⇒ forget.** Sealing a segment before an out-of-band message
      (approval card, notice) also resets the accumulated text, so the tail
      reconciliation only ever considers the current segment.
    - **Tail reconciliation.** The final answer is whatever streamed; if the
      model committed a final item beyond the deltas, only the remainder is
      appended, and a no-delta answer falls back to the committed item.
    """

    def __init__(
        self,
        channel: MessageableProtocol,
        key: ChannelKey,
        *,
        placeholder: str,
        edit_interval_seconds: float,
        logger: logging.Logger,
    ) -> None:
        self._reply = _LiveReply(
            channel,
            key,
            placeholder=placeholder,
            edit_interval_seconds=edit_interval_seconds,
            logger=logger,
        )
        self._channel = channel
        self._key = key
        self._logger = logger
        self._streamed = ""
        self._final: str | None = None
        # The ``message_id`` of the delta stream currently being appended.
        # Native terminal harnesses (claude-native) tag each assistant message
        # item with a stable id and emit several per turn (narration between
        # tool calls). The deltas arrive back to back, so without a boundary the
        # last sentence of one message butts against the first of the next. We
        # insert a paragraph break when the id changes. ``None`` (ordinary
        # in-process streaming) never triggers one.
        self._last_message_id: str | None = None
        # Text put on screen in each sealed segment this turn. Unlike
        # ``_streamed``/``_final`` (which reset at each seal), this survives
        # interruptions, so the no-delta fallback can tell whether the server's
        # newest assistant message is one we ALREADY showed (a trailing notice
        # sealed off an answer we streamed → don't re-post) from a genuinely new
        # message that never streamed (e.g. the post-elicitation answer arrived
        # only committed → DO recover it).
        self._delivered_texts: list[str] = []

    @property
    def segments(self) -> int:
        return self._reply.segments

    @property
    def streamed_len(self) -> int:
        return len(self._streamed)

    async def acknowledge(self) -> None:
        """Show the "Working on it…" placeholder the answer streams into.

        Called once the session is established (after any config summary), so
        the conversation reads metadata → acknowledgement → answer.
        """
        await self._reply.ensure_open()

    async def add_delta(self, delta: str, message_id: str | None = None) -> None:
        # A change in ``message_id`` marks a new assistant message: insert a
        # paragraph break so back-to-back messages don't run together. Only
        # between messages, never before the first, and never for id-less
        # in-process streaming (its id stays None, so this branch never fires).
        if (
            message_id is not None
            and self._last_message_id is not None
            and message_id != self._last_message_id
            and self._streamed
            and not self._streamed.endswith("\n")
        ):
            self._streamed += "\n\n"
            await self._reply.append("\n\n")
        self._last_message_id = message_id
        self._streamed += delta
        await self._reply.append(delta)

    async def flush_if_buffered(self) -> None:
        """Force accumulated-but-unwritten text onto the screen now.

        Called when the read loop detects the stream has gone idle: edits are
        throttled by cadence, so a short burst the agent then pauses after (a
        tool call, thinking) would otherwise stay invisible until more text
        arrives or the turn ends.
        """
        await self._reply.flush()

    def set_final(self, text: str) -> None:
        self._final = text

    async def seal_for_interruption(self) -> None:
        # Before an out-of-band message: reveal any unwritten streamed text
        # FIRST (so it appears above the interruption), finalize the current
        # message so the interruption sorts after it, and forget the
        # accumulated text so the next segment reconciles independently. Record
        # what this segment delivered BEFORE resetting, so the fallback can
        # recognize an already-shown message and not re-post it.
        shown = self._streamed + self._tail()
        if shown:
            self._delivered_texts.append(shown)
        await self._reply.seal()
        self._streamed, self._final = "", None

    async def finalize(self, *, errored: bool) -> bool:
        # Deliver the answer tail and write the reply out one last time.
        # Returns whether a real answer was delivered — when the turn also
        # errored, the caller posts the (generic) failure as a separate message
        # so the answer stays intact; when nothing was produced, a generic
        # failure/empty notice IS the reply. Raw error detail is NEVER shown
        # here (it can carry stack traces / internal paths).
        tail = self._tail()
        # An answer counts as delivered if THIS segment has text OR an earlier
        # segment already showed one before a mid-turn out-of-band post sealed
        # it off. Without the latter, the common "answer streamed, then a
        # trailing notice fired" sequence leaves the current segment empty and
        # would wrongly post "completed without returning…".
        delivered_answer = bool(self._streamed or tail or self._delivered_texts)
        if self._streamed or tail:
            await self._reply.stop(tail or None)
        elif self._delivered_texts:
            # The answer already landed in a prior segment; nothing to write.
            await self._reply.discard_placeholder()
        else:
            await self._reply.replace_with(
                GENERIC_FAILURE_TEXT
                if errored
                else "Omnigent completed without returning response text."
            )
        return delivered_answer

    def _tail(self) -> str:
        # The remainder of the committed final item beyond what already
        # streamed. ``startswith`` also covers the no-delta case (an empty
        # ``_streamed`` is a prefix of everything), so a committed-only answer
        # returns in full.
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
        # Terminal notice (unreachable/host/stream errors, or a no-op abort):
        # replace the placeholder with ``text``. Empty text is a silent stop
        # (nothing to say) — used to clear the placeholder when the reason is
        # delivered elsewhere (e.g. the ephemeral re-login prompt).
        if not text:
            await self._reply.discard_placeholder()
            return
        await self._reply.replace_with(text)
