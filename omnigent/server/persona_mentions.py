"""cc ``@Persona`` in ticket comments: find mentions and deliver them to persona sessions."""

from __future__ import annotations

import hashlib
import logging
import re
import time
import urllib.parse
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.entities import Project

_logger = logging.getLogger(__name__)

# Built-in identity id for the project's Lead, mirrored from project_identities.
ORCHESTRATOR_MENTION_ID = "orchestrator"
# Agent-authored mentions may wake one persona at most this often, so agents can't loop.
AGENT_MENTION_LIMIT = 3
AGENT_MENTION_WINDOW_SECONDS = 600.0
_COMMENT_EXCERPT_CHARS = 1500


@dataclass(frozen=True)
class Mention:
    """A persona named in a comment: its identity id and display name."""

    id: str
    name: str


def find_mentions(text: str, personas: Sequence[Mention]) -> list[Mention]:
    """Return the personas ``@``-mentioned in *text*, in order of first mention.

    Names match case-insensitively and whole-word, longest first, so ``@PM Lead``
    is not also read as ``@PM``. An ``@`` inside a word (an email) is ignored.
    """
    spans: list[tuple[int, int, Mention]] = []
    for persona in sorted(personas, key=lambda entry: len(entry.name), reverse=True):
        pattern = re.compile(rf"(?<![\w@])@{re.escape(persona.name)}(?![\w])", re.IGNORECASE)
        for match in pattern.finditer(text):
            if not any(start < match.end() and match.start() < end for start, end, _ in spans):
                spans.append((match.start(), match.end(), persona))
    seen: dict[str, Mention] = {}
    for _, _, persona in sorted(spans, key=lambda span: span[0]):
        seen.setdefault(persona.id, persona)
    return list(seen.values())


# Mention ids for people carry this prefix, so they never collide with persona ids.
PERSON_MENTION_PREFIX = "user:"
_NAME_SPLIT = re.compile(r"[._\-+]+")


def person_display_name(user_id: str) -> str:
    """A readable name from a sign-in ID: ``ann.lee@example.com`` reads ``Ann Lee``."""
    local = user_id.split("@", 1)[0]
    words = [word for word in _NAME_SPLIT.split(local) if word]
    return " ".join(word[:1].upper() + word[1:] for word in words) or user_id


def person_aliases(people: Sequence[str], personas: Sequence[Mention]) -> list[Mention]:
    """The ``@`` forms that name each person, as mentions with ``user:<id>`` ids.

    Every person matches their full sign-in ID and its local part. Their display
    name and first name match only when no other person or persona shares them;
    a persona's name always wins.
    """
    taken = {persona.name.casefold() for persona in personas}
    forms: dict[str, list[str]] = {}
    for user_id in dict.fromkeys(people):
        local = user_id.split("@", 1)[0]
        display = person_display_name(user_id)
        first = display.split(" ", 1)[0]
        forms[user_id] = list(dict.fromkeys([user_id, local, display, first]))
    owners: dict[str, set[str]] = {}
    for user_id, names in forms.items():
        for name in names:
            owners.setdefault(name.casefold(), set()).add(user_id)
    aliases: list[Mention] = []
    for user_id, names in forms.items():
        for name in names:
            key = name.casefold()
            if key in taken or len(owners[key]) > 1:
                continue
            aliases.append(Mention(PERSON_MENTION_PREFIX + user_id, name))
    return aliases


def split_mentions(found: Sequence[Mention]) -> tuple[list[Mention], list[dict[str, str]]]:
    """Separate personas from people; people come back as ``{"user_id", "name"}``."""
    personas: list[Mention] = []
    people: dict[str, dict[str, str]] = {}
    for mention in found:
        if mention.id.startswith(PERSON_MENTION_PREFIX):
            user_id = mention.id[len(PERSON_MENTION_PREFIX) :]
            people.setdefault(user_id, {"user_id": user_id, "name": person_display_name(user_id)})
        else:
            personas.append(mention)
    return personas, list(people.values())


class MentionRateLimiter:
    """Sliding-window cap on agent-authored mention wakes per target persona."""

    def __init__(
        self,
        limit: int = AGENT_MENTION_LIMIT,
        window_seconds: float = AGENT_MENTION_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._wakes: dict[tuple[str, str], deque[float]] = {}

    def allow(self, project_id: str, persona_id: str) -> bool:
        now = self._clock()
        wakes = self._wakes.setdefault((project_id, persona_id), deque())
        while wakes and now - wakes[0] >= self.window_seconds:
            wakes.popleft()
        if len(wakes) >= self.limit:
            return False
        wakes.append(now)
        return True


def mention_message(
    project: Project,
    ticket: dict[str, Any],
    text: str,
    *,
    author_session_id: str | None,
) -> str:
    """The self-contained turn a mentioned persona receives."""
    author = "the project owner" if author_session_id is None else f"session `{author_session_id}`"
    excerpt = text if len(text) <= _COMMENT_EXCERPT_CHARS else text[:_COMMENT_EXCERPT_CHARS] + "…"
    quoted = "\n".join(f"> {line}" for line in excerpt.splitlines() or [""])
    link = f"/projects/{urllib.parse.quote(project.name)}?ticket={ticket['id']}"
    if author_session_id is None:
        return (
            f'A person addressed you on ticket "{ticket["title"]}" (`{ticket["id"]}`):\n\n'
            f"{quoted}\n\n"
            f"Open it: {link}\n"
            "Only the agents they @mentioned were asked to answer. Read it with "
            "sys_ticket_get, then reply on the ticket with sys_ticket_comment in plain "
            "language for them, and act on it if it asks for work."
        )
    return (
        f'You were cc\'d by {author} on ticket "{ticket["title"]}" (`{ticket["id"]}`):\n\n'
        f"{quoted}\n\n"
        f"Open it: {link}\n"
        "Read it with sys_ticket_get and act if it needs you; reply on the ticket with "
        "sys_ticket_comment. If it doesn't need you, no reply is needed. Don't link "
        "yourself to the ticket with sys_session_link_ticket unless you take on its work; "
        "linked sessions receive every later comment on it."
    )


@dataclass(frozen=True)
class PersonaMentionTargets:
    """Project-route callbacks the ticket routes use to resolve and wake personas."""

    project: Callable[[str, str | None], Awaitable[Project]]
    personas: Callable[[Project], Awaitable[list[Mention]]]
    open: Callable[[Any, Project, Mention], Awaitable[str | None]]
    # The owner and shared members' sign-in IDs; empty where nothing is shared.
    people: Callable[[Project], Awaitable[list[str]]] | None = None


def mention_stable_id(event_id: str, persona_id: str) -> str:
    """32 hex characters per comment and persona, so a retried delivery dedupes."""
    return hashlib.sha256(f"mention:{event_id}:{persona_id}".encode()).hexdigest()[:32]


OpenSession = Callable[[Mention], Awaitable[str | None]]
Deliver = Callable[[str, str, str], Awaitable[bool]]


async def deliver_mentions(
    project: Project,
    ticket: dict[str, Any],
    text: str,
    mentions: Sequence[Mention],
    *,
    event_id: str,
    author_session_id: str | None,
    open_session: OpenSession,
    deliver: Deliver,
    limiter: MentionRateLimiter,
) -> dict[str, str]:
    """Deliver one comment to each mentioned persona's session.

    *open_session* returns the persona's current session, restoring it if needed;
    *deliver* enqueues ``(session_id, stable_id, text)``. Owner comments always
    deliver; agent comments skip the author's own session and are rate-limited.

    :returns: Persona id to outcome: ``delivered``, ``queued``, ``self``,
        ``rate_limited``, ``unavailable`` or ``failed``.
    """
    message = mention_message(project, ticket, text, author_session_id=author_session_id)
    outcomes: dict[str, str] = {}
    for mention in mentions:
        try:
            session_id = await open_session(mention)
            if session_id is None:
                outcomes[mention.id] = "unavailable"
                continue
            if author_session_id is not None:
                if session_id == author_session_id:
                    outcomes[mention.id] = "self"
                    continue
                if not limiter.allow(project.id, mention.id):
                    outcomes[mention.id] = "rate_limited"
                    continue
            scheduled = await deliver(session_id, mention_stable_id(event_id, mention.id), message)
            outcomes[mention.id] = "delivered" if scheduled else "queued"
        except Exception:
            _logger.warning(
                "Could not deliver a ticket mention to persona %s", mention.id, exc_info=True
            )
            outcomes[mention.id] = "failed"
    return outcomes
