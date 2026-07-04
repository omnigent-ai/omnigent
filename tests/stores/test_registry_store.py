"""Tests for :class:`SqlAlchemyRegistryStore`.

Exercises all public methods against a real SQLite database (migrations
applied via :func:`get_or_create_engine`), following the pattern used
by :mod:`tests.stores.test_comment_store`.

The ``db_uri`` fixture in the root conftest creates a fresh per-test
SQLite file and tears it down automatically.
"""

from __future__ import annotations

import pytest

from omnigent.stores.registry_store.sqlalchemy_store import SqlAlchemyRegistryStore


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyRegistryStore:
    """A fresh :class:`SqlAlchemyRegistryStore` backed by the test SQLite DB."""
    return SqlAlchemyRegistryStore(db_uri)


def _publish(store: SqlAlchemyRegistryStore, **overrides: object) -> object:
    """Helper: publish a minimal agent entry with sensible defaults."""
    defaults: dict = {
        "publication_id": "pa_test0000000000000000000000000001",
        "name": "test-agent",
        "version": "1.0.0",
        "harness": "claude-sdk",
        "description": "A test agent",
        "author": "tester@example.com",
        "created_at": 1_700_000_000,
    }
    defaults.update(overrides)
    return store.publish(**defaults)  # type: ignore[arg-type]


# ── publish ───────────────────────────────────────────────────────────────────


def test_publish_returns_entity(store: SqlAlchemyRegistryStore) -> None:
    """``publish`` returns a ``PublishedAgent`` with all fields populated."""
    agent = _publish(store)
    assert agent.id == "pa_test0000000000000000000000000001"
    assert agent.name == "test-agent"
    assert agent.version == "1.0.0"
    assert agent.harness == "claude-sdk"
    assert agent.description == "A test agent"
    assert agent.author == "tester@example.com"
    assert agent.created_at == 1_700_000_000
    assert agent.stars_count == 0
    assert agent.tags == []
    assert agent.network_access is False
    assert agent.write_access is False


def test_publish_with_all_optional_fields(store: SqlAlchemyRegistryStore) -> None:
    """``publish`` persists optional metadata fields correctly."""
    agent = _publish(
        store,
        category="coding",
        tags=["typescript", "rag"],
        prompt_excerpt="You are a helpful coding assistant.",
        network_access=True,
        write_access=True,
        guardrails="No shell access.",
        source_url="https://example.com/agent.yaml",
    )
    assert agent.category == "coding"
    assert agent.tags == ["typescript", "rag"]
    assert agent.prompt_excerpt == "You are a helpful coding assistant."
    assert agent.network_access is True
    assert agent.write_access is True
    assert agent.guardrails == "No shell access."
    assert agent.source_url == "https://example.com/agent.yaml"


def test_publish_duplicate_raises(store: SqlAlchemyRegistryStore) -> None:
    """Publishing the same ``name@version`` twice raises ``ValueError`` or integrity error."""
    _publish(store)
    with pytest.raises((ValueError, Exception)):  # IntegrityError or ValueError
        _publish(store)


# ── get ───────────────────────────────────────────────────────────────────────


def test_get_returns_exact_version(store: SqlAlchemyRegistryStore) -> None:
    """``get`` retrieves the exact ``name@version`` entry."""
    _publish(store, name="agent-a", version="1.0.0", publication_id="pa_a1")
    _publish(store, name="agent-a", version="2.0.0", publication_id="pa_a2",
             created_at=1_700_000_001)
    result = store.get("agent-a", "1.0.0")
    assert result is not None
    assert result.version == "1.0.0"
    assert result.id == "pa_a1"


def test_get_returns_none_for_missing(store: SqlAlchemyRegistryStore) -> None:
    """``get`` returns ``None`` when the entry does not exist."""
    assert store.get("nonexistent", "1.0.0") is None


# ── get_latest ────────────────────────────────────────────────────────────────


def test_get_latest_returns_most_recent(store: SqlAlchemyRegistryStore) -> None:
    """``get_latest`` returns the entry with the highest ``created_at``."""
    _publish(store, name="multi", version="1.0.0", publication_id="pa_m1",
             created_at=1_700_000_000)
    _publish(store, name="multi", version="2.0.0", publication_id="pa_m2",
             created_at=1_700_000_100)
    latest = store.get_latest("multi")
    assert latest is not None
    assert latest.version == "2.0.0"


def test_get_latest_returns_none_for_unknown_name(store: SqlAlchemyRegistryStore) -> None:
    """``get_latest`` returns ``None`` when no version is published for the name."""
    assert store.get_latest("unknown-agent") is None


# ── browse ────────────────────────────────────────────────────────────────────


def test_browse_returns_all_entries(store: SqlAlchemyRegistryStore) -> None:
    """``browse`` with no filters returns all published agents."""
    _publish(store, name="a1", publication_id="pa_b1", created_at=1_000)
    _publish(store, name="a2", publication_id="pa_b2", created_at=1_001)
    page = store.browse()
    assert page.has_more is False
    assert len(page.data) == 2


def test_browse_filter_by_category(store: SqlAlchemyRegistryStore) -> None:
    """``browse`` with ``category=`` returns only matching entries."""
    _publish(store, name="coding-agent", publication_id="pa_c1", category="coding")
    _publish(store, name="ops-agent", publication_id="pa_c2", category="ops",
             created_at=1_700_000_001)
    page = store.browse(category="coding")
    assert len(page.data) == 1
    assert page.data[0].name == "coding-agent"


def test_browse_filter_by_harness(store: SqlAlchemyRegistryStore) -> None:
    """``browse`` with ``harness=`` returns only matching entries."""
    _publish(store, name="claude-agent", publication_id="pa_h1", harness="claude-sdk")
    _publish(store, name="codex-agent", publication_id="pa_h2", harness="codex",
             created_at=1_700_000_001)
    page = store.browse(harness="codex")
    assert len(page.data) == 1
    assert page.data[0].harness == "codex"


def test_browse_filter_by_tag(store: SqlAlchemyRegistryStore) -> None:
    """``browse`` with ``tag=`` returns only agents whose tags include the value."""
    _publish(store, name="rag-agent", publication_id="pa_t1", tags=["rag", "search"])
    _publish(store, name="plain-agent", publication_id="pa_t2", tags=[],
             created_at=1_700_000_001)
    page = store.browse(tag="rag")
    assert len(page.data) == 1
    assert page.data[0].name == "rag-agent"


def test_browse_keyword_search(store: SqlAlchemyRegistryStore) -> None:
    """``browse`` with ``q=`` matches on name and description."""
    _publish(store, name="typescript-helper", publication_id="pa_q1",
             description="Helps with TypeScript projects.")
    _publish(store, name="python-expert", publication_id="pa_q2",
             description="Expert in Python.", created_at=1_700_000_001)
    page = store.browse(q="typescript")
    assert len(page.data) == 1
    assert page.data[0].name == "typescript-helper"


def test_browse_pagination_limit(store: SqlAlchemyRegistryStore) -> None:
    """``browse`` with ``limit=1`` returns one entry and sets ``has_more``."""
    for i in range(3):
        _publish(store, name=f"agent-{i}", publication_id=f"pa_p{i}",
                 created_at=1_700_000_000 + i)
    page = store.browse(limit=1)
    assert len(page.data) == 1
    assert page.has_more is True


def test_browse_cursor_pagination(store: SqlAlchemyRegistryStore) -> None:
    """``browse`` cursor returns the next page correctly."""
    for i in range(3):
        _publish(store, name=f"paged-{i}", publication_id=f"pa_pp{i}",
                 created_at=1_700_000_000 + i)
    page1 = store.browse(limit=2)
    assert len(page1.data) == 2
    assert page1.has_more is True

    page2 = store.browse(limit=2, after=page1.last_id)
    assert len(page2.data) == 1
    assert page2.has_more is False


# ── star ──────────────────────────────────────────────────────────────────────


def test_star_increments_count(store: SqlAlchemyRegistryStore) -> None:
    """``star`` increments ``stars_count`` and returns the new value."""
    _publish(store)
    new_count = store.star("test-agent", "1.0.0")
    assert new_count == 1
    new_count = store.star("test-agent", "1.0.0")
    assert new_count == 2


def test_star_raises_for_missing_agent(store: SqlAlchemyRegistryStore) -> None:
    """``star`` raises ``KeyError`` when the ``name@version`` is not found."""
    with pytest.raises(KeyError):
        store.star("nonexistent", "1.0.0")
