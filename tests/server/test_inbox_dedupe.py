"""Tests for Inbox duplicate detection."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.entities import InboxItem
from omnigent.server import inbox_dedupe
from omnigent.server.inbox_dedupe import (
    DedupeInputs,
    InvalidLinkError,
    canonical_link,
    dedupe_key,
    find_duplicate,
    normalize_topic,
)


@pytest.mark.parametrize(
    "ref",
    [
        "https://github.com/Acme/App/pull/7",
        "http://github.com/acme/app/pull/7/",
        "https://www.github.com/acme/app/pull/7/files",
        "github.com/acme/app/pull/7#discussion_r1",
        "https://github.com/acme/app/pull/7/commits?w=1",
    ],
)
def test_pull_request_spellings_share_one_form(ref: str) -> None:
    assert canonical_link({"type": "pr", "ref": ref}) == "github.com/acme/app/pull/7"


def test_other_links() -> None:
    assert canonical_link({"type": "session", "ref": "abc"}) == "session:abc"
    assert (
        canonical_link({"type": "url", "ref": "https://Docs.Example.com/a/?x=1#top"})
        == "docs.example.com/a?x=1"
    )
    assert canonical_link({"type": "url", "ref": "  "}) is None
    assert canonical_link({"type": "url", "ref": "https://example.test:443/a"}) == "example.test/a"
    assert canonical_link({"type": "url", "ref": "http://example.test:80/a"}) == "example.test/a"
    assert canonical_link({"type": "url", "ref": "http://example.test:443/a"}) == (
        "example.test:443/a"
    )
    assert canonical_link({"type": "url", "ref": "https://example.test:8443/a"}) != canonical_link(
        {"type": "url", "ref": "https://example.test:9443/a"}
    )


@pytest.mark.parametrize(
    "link",
    [
        {"type": "url", "ref": "https://["},
        {"type": "url", "ref": "https://example.test:99999/a"},
        {"type": "url", "ref": "javascript:alert(1)"},
        {"type": "url", "ref": "ftp://example.test/a"},
        {"type": "session", "ref": "s" * 2048},
    ],
    ids=["unparseable", "bad-port", "script", "non-web-scheme", "too-long-once-canonical"],
)
def test_links_the_server_cannot_store_are_rejected(link: dict[str, str]) -> None:
    with pytest.raises(InvalidLinkError):
        canonical_link(link)


def test_normalize_topic() -> None:
    assert normalize_topic("  Ramp Up_flag ") == "ramp-up-flag"


def test_key_is_versioned_and_depends_on_every_input() -> None:
    base = DedupeInputs(kind="followup", primary_link="github.com/acme/app/pull/7", topic="x")
    key = dedupe_key(base)

    assert key.startswith("v1:")
    assert dedupe_key(base) == key
    for changed in (
        DedupeInputs("reminder", base.primary_link, base.topic),
        DedupeInputs(base.kind, None, base.topic),
        DedupeInputs(base.kind, base.primary_link, "y"),
    ):
        assert dedupe_key(changed) != key
    assert dedupe_key(DedupeInputs("info", "example.test/a|b", "c")) != dedupe_key(
        DedupeInputs("info", "example.test/a", "b|c")
    )


class _Lookup:
    """Returns scripted items by key and records the keys asked for."""

    def __init__(self, items: dict[str, InboxItem]) -> None:
        self.items = items
        self.asked: list[str] = []

    def get_by_dedupe_key(self, user_id: str, key: str) -> InboxItem | None:
        self.asked.append(key)
        return self.items.get(key)


def _item(**overrides: Any) -> InboxItem:
    fields: dict[str, Any] = {
        "id": "1" * 32,
        "user_id": "alice",
        "source_session_id": "2" * 32,
        "agent_id": "3" * 32,
        "prompt_version": None,
        "kind": "info",
        "title": "t",
        "body_md": "b",
        "links": [],
        "actions": [],
        "chart": None,
        "primary_link": None,
        "topic": "digest",
        "dedupe_key": "",
        "state": "unread",
        "resolution": "none",
        "feedback_text": None,
        "created_at": 1,
    }
    fields.update(overrides)
    return InboxItem(**fields)


_INPUTS = DedupeInputs(kind="info", primary_link=None, topic="digest")


def test_a_live_item_matches_until_it_expires() -> None:
    item = _item(expires_at=200)
    lookup = _Lookup({dedupe_key(_INPUTS): item})

    assert find_duplicate(lookup, "alice", _INPUTS, now=150).existing == item  # type: ignore[arg-type]
    after = find_duplicate(lookup, "alice", _INPUTS, now=200)  # type: ignore[arg-type]
    assert (after.existing, after.expired) == (None, item)
    assert find_duplicate(_Lookup({}), "alice", _INPUTS, now=1).existing is None  # type: ignore[arg-type]


def test_dismissed_items_still_suppress() -> None:
    item = _item(state="dismissed", resolution="not_relevant")
    lookup = _Lookup({dedupe_key(_INPUTS): item})

    assert find_duplicate(lookup, "alice", _INPUTS, now=1).existing == item  # type: ignore[arg-type]


def test_legacy_versions_still_match(monkeypatch: pytest.MonkeyPatch) -> None:
    item = _item()
    lookup = _Lookup({dedupe_key(_INPUTS, "v1"): item})
    monkeypatch.setitem(inbox_dedupe._KEY_FORMULAS, "v2", lambda _inputs: "new-formula")
    monkeypatch.setattr(inbox_dedupe, "CURRENT_KEY_VERSION", "v2")
    monkeypatch.setattr(inbox_dedupe, "LEGACY_KEY_VERSIONS", ("v1",))

    assert find_duplicate(lookup, "alice", _INPUTS, now=1).existing == item  # type: ignore[arg-type]
    assert lookup.asked == ["v2:new-formula", dedupe_key(_INPUTS, "v1")]
