"""Tests for Inbox duplicate detection."""

from __future__ import annotations

import pytest

from omnigent.server import inbox_dedupe
from omnigent.server.inbox_dedupe import (
    DedupeInputs,
    canonical_link,
    dedupe_key,
    find_duplicate,
    normalize_topic,
)
from omnigent.stores.inbox_item_store import NewInboxItem
from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore


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


def _post(store: SqlAlchemyInboxItemStore, inputs: DedupeInputs, **overrides: object) -> str:
    fields = {
        "user_id": "alice",
        "source_session_id": "s" * 32,
        "agent_id": "a" * 32,
        "prompt_version": None,
        "kind": inputs.kind,
        "title": "t",
        "body_md": "b",
        "links": [],
        "suggested_prompt": None,
        "primary_link": inputs.primary_link,
        "topic": inputs.topic,
        "dedupe_key": dedupe_key(inputs),
    }
    fields.update(overrides)
    return store.create(NewInboxItem(**fields)).id  # type: ignore[arg-type]


def test_find_duplicate_matches_until_expiry(db_uri: str) -> None:
    store = SqlAlchemyInboxItemStore(db_uri)
    inputs = DedupeInputs(kind="reminder", primary_link=None, topic="standup")
    assert find_duplicate(store, "alice", inputs, now=100).existing is None

    item_id = _post(store, inputs, expires_at=200)

    match = find_duplicate(store, "alice", inputs, now=150)
    assert match.existing is not None and match.existing.id == item_id
    after = find_duplicate(store, "alice", inputs, now=200)
    assert after.existing is None
    assert after.expired is not None and after.expired.id == item_id


def test_dismissed_items_still_suppress(db_uri: str) -> None:
    store = SqlAlchemyInboxItemStore(db_uri)
    inputs = DedupeInputs(kind="info", primary_link="session:abc", topic="digest")
    item_id = _post(store, inputs)
    store.update(item_id, "alice", state="dismissed", feedback="not_useful", now=10)

    match = find_duplicate(store, "alice", inputs, now=20)

    assert match.existing is not None and match.existing.id == item_id


def test_legacy_versions_still_match(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SqlAlchemyInboxItemStore(db_uri)
    inputs = DedupeInputs(kind="info", primary_link=None, topic="digest")
    item_id = _post(store, inputs)
    monkeypatch.setitem(inbox_dedupe._KEY_FORMULAS, "v2", lambda _inputs: "new-formula")
    monkeypatch.setattr(inbox_dedupe, "CURRENT_KEY_VERSION", "v2")
    monkeypatch.setattr(inbox_dedupe, "LEGACY_KEY_VERSIONS", ("v1",))

    match = find_duplicate(store, "alice", inputs, now=20)

    assert match.existing is not None and match.existing.id == item_id
