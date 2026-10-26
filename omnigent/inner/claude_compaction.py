"""Capture Claude's compacted context without a native transcript on disk."""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import copy
from typing import TYPE_CHECKING

from omnigent.inner.executor import Message

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, Transport


class ClaudeCompactionTranscript:
    """Keep the live context and export only fully observed compactions.

    Raw frames preserve content that the SDK's message parser drops, including
    image blocks and the synthetic-summary marker. The cache belongs to one
    client and shrinks to the retained context at each compaction boundary.
    """

    def __init__(self) -> None:
        self.generation = 0
        self.token_count = 0
        self._messages: dict[str, Message] = {}
        self._boundary: Message | None = None
        self._valid_checkpoint = False

    def observe(self, frame: Message) -> None:
        """Observe a root-session frame before the SDK parses it."""
        if frame.get("parent_tool_use_id") or frame.get("isSidechain"):
            return
        if frame.get("type") == "system" and frame.get("subtype") == "compact_boundary":
            self.generation += 1
            self._boundary = frame
            self._valid_checkpoint = False
            metadata = frame.get("compact_metadata")
            tokens = metadata.get("post_tokens") if isinstance(metadata, dict) else None
            self.token_count = tokens if isinstance(tokens, int) else 0
            return
        if frame.get("type") not in {"user", "assistant"}:
            return
        uuid = frame.get("uuid")
        message = frame.get("message")
        if not isinstance(uuid, str) or not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, (str, list)):
            return
        entry: Message = {"type": "message", "role": frame["type"], "content": content}
        if self._boundary is not None:
            if frame["type"] != "user" or not (
                frame.get("isSynthetic") or frame.get("isCompactSummary")
            ):
                return
            retained = self._retained_messages(uuid)
            self._boundary = None
            if retained is None:
                self._messages = {uuid: entry}
                return
            before, after = retained
            self._messages = {**before, uuid: entry, **after}
            self._valid_checkpoint = bool(content)
            return
        self._messages[uuid] = entry

    def _retained_messages(
        self, summary_uuid: str
    ) -> tuple[dict[str, Message], dict[str, Message]] | None:
        assert self._boundary is not None
        metadata = self._boundary.get("compact_metadata", {})
        if not isinstance(metadata, dict):
            return None
        preserved = metadata.get("preserved_messages")
        if preserved is not None:
            if not isinstance(preserved, dict):
                return None
            uuids = preserved.get("uuids")
            if not isinstance(uuids, list) or not all(isinstance(uuid, str) for uuid in uuids):
                return None
        else:
            preserved = metadata.get("preserved_segment")
            if preserved is None:
                return {}, {}
            if not isinstance(preserved, dict):
                return None
            ordered = list(self._messages)
            try:
                head = ordered.index(preserved["head_uuid"])
                tail = ordered.index(preserved["tail_uuid"])
            except (KeyError, ValueError):
                return None
            if head > tail:
                return None
            uuids = ordered[head : tail + 1]
        if any(uuid not in self._messages for uuid in uuids):
            return None
        retained = {uuid: self._messages[uuid] for uuid in uuids}
        anchor = preserved.get("anchor_uuid")
        if anchor == summary_uuid:
            return {}, retained
        if anchor == self._boundary.get("uuid"):
            return retained, {}
        return None

    def checkpoint_since(self, generation: int) -> list[Message] | None:
        """Return current context only after a new, complete summary arrives."""
        if self.generation == generation or not self._valid_checkpoint:
            return None
        return list(self._messages.values())


def compaction_transport(
    options: ClaudeAgentOptions, transcript: ClaudeCompactionTranscript
) -> Transport:
    """Tap the CLI transport before SDK parsing loses checkpoint fields."""
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    class CapturingTransport(SubprocessCLITransport):
        async def read_messages(self) -> AsyncIterator[Message]:
            async for frame in super().read_messages():
                transcript.observe(frame)
                yield frame

    async def streaming_prompt() -> AsyncIterator[Message]:
        return
        yield  # pragma: no cover — selects the SDK's bidirectional transport mode

    # The SDK sets this on its default transport when can_use_tool is supplied.
    transport_options = copy(options)
    if transport_options.can_use_tool:
        transport_options.permission_prompt_tool_name = "stdio"

    def on_stderr(line: str) -> None:
        if options.stderr is not None:
            options.stderr(line)

    transport_options.stderr = on_stderr
    return CapturingTransport(prompt=streaming_prompt(), options=transport_options)
