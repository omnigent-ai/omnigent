"""The Slack relay's durable state and reply formatting, without Slack or the watcher."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from omnigent.server.routes.slack_relay import delivery_id, render_reply, slack_text
from omnigent.stores.project_slack_relay_store import SlackEvent, SlackRelayStore
from omnigent.stores.project_slack_store import ProjectSlackStore


def _event(ts: str, *, thread: str | None = None, mentioned: bool = False, user: str = "U1"):
    return SlackEvent(
        ts=ts,
        thread_ts=thread or ts,
        user=user,
        user_name="Ann Lee",
        text="hello",
        permalink="",
        mentioned=mentioned,
    )


def _binding(db_uri: str) -> str:
    store = ProjectSlackStore(db_uri)
    return store.claim(
        uuid.uuid4().hex,
        slack_team_id="T01",
        channel_id="C0001",
        channel_name="eng",
        destination={},
        owner_user_id="owner@example.com",
        actor_user_id="owner@example.com",
    ).id


def test_mentions_start_threads_and_follow_ups_need_no_mention(db_uri: str) -> None:
    relay = SlackRelayStore(db_uri)
    binding = _binding(db_uri)
    root = _event("1700000000.000100", mentioned=True)
    chatter = _event("1700000000.000200")
    deliver, skip = relay.classify(binding, [chatter, root])
    assert deliver == [root] and skip == [chatter]
    relay.commit(binding, delivered=deliver, skipped=skip, cursor="1700000000.000200")

    follow_up = _event("1700000001.000100", thread=root.ts)
    deliver, skip = relay.classify(binding, [follow_up, root])
    assert deliver == [follow_up] and skip == []  # the root is already handled
    state = relay.state(binding)
    assert state["cursor"] == "1700000000.000200"
    assert state["threads"] == [{"thread_ts": root.ts, "last_seen_ts": root.ts}]


def test_cursor_only_moves_forward(db_uri: str) -> None:
    relay = SlackRelayStore(db_uri)
    binding = _binding(db_uri)
    relay.commit(binding, delivered=[], skipped=[], cursor="1700000005.000000")
    relay.commit(binding, delivered=[], skipped=[], cursor="1700000001.000000")
    assert relay.state(binding)["cursor"] == "1700000005.000000"


def test_root_author_is_mentioned_once_per_request(db_uri: str) -> None:
    relay = SlackRelayStore(db_uri)
    binding = _binding(db_uri)
    root = _event("1700000000.000100", mentioned=True)
    relay.commit(binding, delivered=[root], skipped=[], cursor=None)
    mentions: list[str | None] = []

    def render(reply_id: str, mention: str | None) -> str:
        mentions.append(mention)
        return f"reply {reply_id}"

    relay.enqueue_reply(binding, root.ts, render)
    relay.enqueue_reply(binding, root.ts, render)
    # A new message in the thread is a new request: one more mention.
    follow_up = _event("1700000002.000100", thread=root.ts, user="U2")
    relay.commit(binding, delivered=[follow_up], skipped=[], cursor=None)
    relay.enqueue_reply(binding, root.ts, render)
    assert mentions == ["U1", None, "U1"]


def test_outbox_lease_uncertain_and_retry(db_uri: str) -> None:
    relay = SlackRelayStore(db_uri)
    binding = _binding(db_uri)
    root = _event("1700000000.000100", mentioned=True)
    relay.commit(binding, delivered=[root], skipped=[], cursor=None)
    reply_id = relay.enqueue_reply(binding, root.ts, lambda reply_id, _: "text")

    first = relay.claim_replies(binding)
    assert [(reply.id, reply.check_first) for reply in first] == [(reply_id, False)]
    assert relay.claim_replies(binding) == []  # still leased
    assert relay.ack_reply(binding, reply_id, uncertain=True) == "uncertain"
    again = relay.claim_replies(binding)
    assert again[0].check_first  # look before reposting
    assert relay.ack_reply(binding, reply_id, slack_ts="1700000003.000100") == "posted"
    assert relay.claim_replies(binding) == []
    assert relay.ack_reply(binding, reply_id, failed="late") == "posted"


def test_failed_replies_retry_then_stop(db_uri: str) -> None:
    relay = SlackRelayStore(db_uri)
    binding = _binding(db_uri)
    root = _event("1700000000.000100", mentioned=True)
    relay.commit(binding, delivered=[root], skipped=[], cursor=None)
    reply_id = relay.enqueue_reply(binding, root.ts, lambda reply_id, _: "text")
    states = []
    for _ in range(3):
        relay.claim_replies(binding)
        states.append(relay.ack_reply(binding, reply_id, failed="channel_not_found"))
    assert states == ["pending", "pending", "failed"]


def test_reply_formatting() -> None:
    project = SimpleNamespace(name="Payments Web")
    text = render_reply(
        "See **this** [ticket](https://factory.example.com/t/1).",
        project=project,
        base_url="https://factory.example.com/",
        reply_id="abc",
        mention_user="U1",
    )
    assert text.startswith("<@U1> See *this* <https://factory.example.com/t/1|ticket>.")
    footer = "#omni-reply-abc|via Payments Web · Lead>_"
    assert text.endswith(f"_<https://factory.example.com/projects/Payments%20Web{footer}")
    assert slack_text("plain") == "plain"
    assert len(delivery_id("b", "1.2")) == 32 and delivery_id("b", "1.2") != delivery_id(
        "b", "1.3"
    )


def test_channel_links_parse_with_or_without_a_workspace_id() -> None:
    import pytest

    from omnigent.errors import OmnigentError
    from omnigent.server.routes.project_slack import SlackLinkBody, parse_channel

    archive = "https://example.enterprise.slack.com/archives/C0BUJ6MKKQF"
    assert parse_channel(SlackLinkBody(channel_link=archive)) == (None, "C0BUJ6MKKQF")
    assert parse_channel(
        SlackLinkBody(channel_link=archive + "?thread=1", slack_team_id="t01x")
    ) == (
        "T01X",
        "C0BUJ6MKKQF",
    )
    client = "https://app.slack.com/client/T0ZZ1/C0ZZ2"
    assert parse_channel(SlackLinkBody(channel_link=client)) == ("T0ZZ1", "C0ZZ2")
    with pytest.raises(OmnigentError):
        parse_channel(SlackLinkBody(channel_link="https://example.com/C0ZZ2"))
