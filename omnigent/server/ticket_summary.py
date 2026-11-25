"""Summarize a ticket's history with the server's configured judge model.

The model is the operator's server-level ``llm:`` block, the same one the
smart-routing judge and policy functions use, so no model is hard-coded and a
deployment without one simply offers no summaries. Results are cached per
ticket by its last event position, so a summary is regenerated only after new
activity, and each person may generate a bounded number per window.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError

#: Tickets with more events than this offer a summary in the UI.
SUMMARY_MIN_EVENTS = 20
#: Most history characters sent to the model; the start and the newest events are kept.
_HISTORY_MAX_CHARS = 40_000
_EVENT_TEXT_MAX_CHARS = 1_200
_CACHE_SIZE = 256
#: Generations each person may start per window.
_RATE_LIMIT = 10
_RATE_WINDOW_S = 600.0

_INSTRUCTIONS = (
    "You summarize the history of one work ticket for a person who hasn't followed it. "
    "Write plain, short sentences for a non-specialist. Use exactly these four sections, "
    "each a few bullets at most:\n"
    "**What was asked**\n**Key decisions**\n**Where it stands** (current state and any "
    "blockers, including what is waiting on a person)\n**Who is doing what**\n"
    "Name people and roles as they appear in the history. Leave out commit hashes, IDs "
    "and file paths unless they are the point. Don't invent anything that isn't in the "
    "history; if a section has nothing, say so in a few words."
)


@dataclass(frozen=True)
class TicketSummary:
    """A generated summary and the history it covers.

    :param text: The summary, in markdown.
    :param through_position: The last event position it covers.
    :param generated_at: Unix seconds when it was generated.
    :param model: The model that wrote it.
    """

    text: str
    through_position: int
    generated_at: int
    model: str

    def as_dict(self, *, cached: bool) -> dict[str, Any]:
        return {
            "text": self.text,
            "through_position": self.through_position,
            "generated_at": self.generated_at,
            "model": self.model,
            "cached": cached,
        }


def _who(event: Mapping[str, Any], role_names: Mapping[str, str]) -> str:
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        return role_names.get(session_id, "an agent")
    user_id = event.get("user_id")
    return str(user_id) if user_id else "someone"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_event(event: Mapping[str, Any], role_names: Mapping[str, str]) -> str:
    """One history line: position, who, and what happened."""
    payload = event.get("payload") or {}
    kind = event.get("kind")
    who = _who(event, role_names)
    if kind == "created":
        what = f"created the ticket: {payload.get('title', '')}. {payload.get('description', '')}"
    elif kind == "comment":
        what = f"commented: {payload.get('text', '')}"
    elif kind == "updated":
        parts = []
        changes = payload.get("changes") or {}
        if isinstance(payload.get("stage"), dict):
            parts.append(f"moved it to {payload['stage'].get('name')}")
        if "labels" in changes:
            labels = ", ".join(
                f"{label.get('key')}" + (f"={label['value']}" if label.get("value") else "")
                for label in changes["labels"] or []
                if isinstance(label, dict)
            )
            parts.append(f"set labels: {labels}")
        for field in ("title", "description"):
            if field in changes:
                parts.append(f"changed the {field}")
        if payload.get("action") and payload.get("involvement"):
            parts.append(f"{payload['action']}ed a session: {payload['involvement']}")
        if payload.get("text"):
            parts.append(f"noted: {payload['text']}")
        what = "; ".join(parts) or "updated it"
    elif kind == "output":
        what = f"attached {payload.get('type', 'an output')}: {payload.get('url', '')}"
    else:
        what = f"{kind}: {json.dumps(payload, ensure_ascii=False)[:300]}"
    return f"[{event.get('position')}] {who} {_clip(what, _EVENT_TEXT_MAX_CHARS)}"


def render_history(lines: list[str]) -> str:
    """The history for the prompt, keeping the opening events and the newest ones."""
    text = "\n".join(lines)
    if len(text) <= _HISTORY_MAX_CHARS:
        return text
    head, tail, used = [], [], 0
    budget = _HISTORY_MAX_CHARS
    for line in lines[:10]:
        if used + len(line) > budget // 4:
            break
        head.append(line)
        used += len(line) + 1
    for line in reversed(lines[len(head) :]):
        if used + len(line) > budget:
            break
        tail.append(line)
        used += len(line) + 1
    skipped = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"[… {skipped} earlier events left out …]", *reversed(tail)])


class TicketSummarizer:
    """Generates, caches and rate-limits ticket summaries."""

    def __init__(self, llm_client: Any | None) -> None:
        """:param llm_client: The server's judge client, or ``None`` when none is configured."""
        self._llm = llm_client
        self._cache: OrderedDict[str, TicketSummary] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._starts: dict[str, deque[float]] = {}

    @property
    def available(self) -> bool:
        return self._llm is not None

    @property
    def model(self) -> str:
        return str(getattr(self._llm, "_model", "") or "")

    def cached(self, ticket_id: str, last_position: int) -> TicketSummary | None:
        """The cached summary if it still covers the ticket's newest event."""
        summary = self._cache.get(ticket_id)
        if summary is None or summary.through_position != last_position:
            return None
        self._cache.move_to_end(ticket_id)
        return summary

    def latest(self, ticket_id: str) -> TicketSummary | None:
        """The last summary generated for the ticket, even if newer activity followed."""
        return self._cache.get(ticket_id)

    def _take_rate_slot(self, user_key: str) -> None:
        now = time.monotonic()
        starts = self._starts.setdefault(user_key, deque())
        while starts and now - starts[0] > _RATE_WINDOW_S:
            starts.popleft()
        if len(starts) >= _RATE_LIMIT:
            raise OmnigentError(
                "You've summarized a lot of tickets just now; try again in a few minutes",
                code=ErrorCode.CONFLICT,
            )
        starts.append(now)

    async def summarize(
        self,
        ticket_id: str,
        events: Iterable[Mapping[str, Any]],
        role_names: Mapping[str, str],
        *,
        user_key: str,
    ) -> tuple[TicketSummary, bool]:
        """Return ``(summary, cached)``, generating only when the history moved on.

        :raises OmnigentError: ``RUNNER_UNAVAILABLE`` with no model configured,
            ``CONFLICT`` past the person's budget, ``INTERNAL_ERROR`` when
            the model call fails.
        """
        if self._llm is None:
            raise OmnigentError(
                "Ticket summaries aren't set up on this server", code=ErrorCode.RUNNER_UNAVAILABLE
            )
        history = list(events)
        last_position = max((int(event.get("position", -1)) for event in history), default=-1)
        lock = self._locks.setdefault(ticket_id, asyncio.Lock())
        async with lock:
            hit = self.cached(ticket_id, last_position)
            if hit is not None:
                return hit, True
            self._take_rate_slot(user_key)
            prompt = render_history([render_event(event, role_names) for event in history])
            try:
                response = await self._llm.create(
                    instructions=_INSTRUCTIONS,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": f"Ticket history:\n{prompt}"}
                            ],
                        }
                    ],
                )
                text = response.output[0].content[0].text.strip()
            except Exception as exc:
                raise OmnigentError(
                    "The summary couldn't be generated; try again", code=ErrorCode.INTERNAL_ERROR
                ) from exc
            summary = TicketSummary(text, last_position, int(time.time()), self.model)
            self._cache[ticket_id] = summary
            self._cache.move_to_end(ticket_id)
            while len(self._cache) > _CACHE_SIZE:
                evicted, _ = self._cache.popitem(last=False)
                self._locks.pop(evicted, None)
            return summary, False


def build_ticket_summarizer() -> TicketSummarizer:
    """A summarizer on the server's ``llm:`` block, or an unavailable one without it."""
    from omnigent.runtime import get_caps
    from omnigent.runtime.policies.builder import (
        _build_policy_llm_client,
        _resolve_server_llm_connection,
    )

    server_llm = get_caps().llm
    try:
        client = _build_policy_llm_client(server_llm, _resolve_server_llm_connection(server_llm))
    except OSError:
        client = None
    return TicketSummarizer(client)
