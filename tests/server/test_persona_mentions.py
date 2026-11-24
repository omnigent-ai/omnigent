"""cc @Persona parsing, rate limiting and delivery outcomes."""

from __future__ import annotations

from types import SimpleNamespace

from omnigent.server.persona_mentions import (
    Mention,
    MentionRateLimiter,
    deliver_mentions,
    find_mentions,
    mention_message,
    mention_stable_id,
)

PM = Mention("pm", "PM")
LEAD = Mention("lead", "PM Lead")
ORCHESTRATOR = Mention("orchestrator", "Orchestrator")
PERSONAS = [ORCHESTRATOR, PM, LEAD]


def test_find_mentions_matches_whole_names_longest_first_and_dedupes() -> None:
    text = "cc @pm lead and @Orchestrator, again @PM; not a@pm.example or @PMs"
    assert find_mentions(text, PERSONAS) == [LEAD, ORCHESTRATOR, PM]
    assert find_mentions("no mentions here", PERSONAS) == []


def test_rate_limiter_caps_wakes_per_persona_within_the_window() -> None:
    now = [0.0]
    limiter = MentionRateLimiter(limit=2, window_seconds=10, clock=lambda: now[0])
    assert [limiter.allow("p", "pm") for _ in range(3)] == [True, True, False]
    assert limiter.allow("p", "lead") is True
    now[0] = 10.0
    assert limiter.allow("p", "pm") is True


async def test_owner_mentions_always_deliver_and_agents_skip_themselves_and_loops() -> None:
    project = SimpleNamespace(id="p", name="Shipping")
    ticket = {"id": "t1", "title": "Fix login"}
    sessions = {"pm": "pm-session", "lead": "lead-session", "orchestrator": None}
    delivered: list[tuple[str, str]] = []

    async def open_session(mention: Mention) -> str | None:
        return sessions[mention.id]

    async def deliver(session_id: str, stable_id: str, text: str) -> bool:
        delivered.append((session_id, stable_id))
        assert 'ticket "Fix login" (`t1`)' in text and "> Please look" in text
        return True

    limiter = MentionRateLimiter(limit=1, window_seconds=60)
    common = {"open_session": open_session, "deliver": deliver, "limiter": limiter}
    owner = await deliver_mentions(
        project, ticket, "Please look", PERSONAS, event_id="e1", author_session_id=None, **common
    )
    assert owner == {"orchestrator": "unavailable", "pm": "delivered", "lead": "delivered"}
    linked = await deliver_mentions(
        project,
        ticket,
        "Please look",
        [PM],
        event_id="e0",
        author_session_id=None,
        already_notified=frozenset({"pm-session"}),
        **common,
    )
    assert linked == {"pm": "linked"}
    agent = await deliver_mentions(
        project,
        ticket,
        "Please look",
        [PM, LEAD],
        event_id="e2",
        author_session_id="pm-session",
        **common,
    )
    assert agent == {"pm": "self", "lead": "delivered"}
    again = await deliver_mentions(
        project,
        ticket,
        "Please look",
        [LEAD],
        event_id="e3",
        author_session_id="pm-session",
        **common,
    )
    assert again == {"lead": "rate_limited"}
    assert delivered == [
        ("pm-session", mention_stable_id("e1", "pm")),
        ("lead-session", mention_stable_id("e1", "lead")),
        ("lead-session", mention_stable_id("e2", "lead")),
    ]


def test_mention_message_links_the_ticket_and_names_the_author() -> None:
    project = SimpleNamespace(id="p", name="My project")
    message = mention_message(
        project, {"id": "t1", "title": "T"}, "line one\nline two", author_session_id="s1"
    )
    assert "cc'd by session `s1`" in message
    assert "> line one\n> line two" in message
    assert "/projects/My%20project?ticket=t1" in message
    assert len(mention_stable_id("e1", "pm")) == 32
