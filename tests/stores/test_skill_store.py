"""The skill store: source dedup, scoped visibility, replace-on-reinstall, and deletes."""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from omnigent.db.utils import get_or_create_engine
from omnigent.stores.skill_store import SkillValueError
from omnigent.stores.skill_store.sqlalchemy_store import SqlAlchemySkillStore


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySkillStore:
    return SqlAlchemySkillStore(db_uri)


def _install(store: SqlAlchemySkillStore, name: str, *, scope="user", owner="alice", **over):
    fields = {
        "name": name,
        "display_name": None,
        "description": f"{name} description",
        "source_url": "https://github.com/acme/skills.git",
        "ref": None,
        "commit": "a" * 40,
        "path": f"skills/{name}",
        "scope": scope,
        "owner_user_id": owner,
        "bundle_location": f"skills/{name}.tar.gz",
        "installed_by": owner,
    }
    fields.update(over)
    return store.install(**fields)


def test_sources_dedupe_on_url_and_ref(store: SqlAlchemySkillStore) -> None:
    first = store.add_source(
        url="https://github.com/acme/skills.git", ref=None, name="Acme", created_by="a"
    )
    again = store.add_source(
        url="https://github.com/acme/skills.git", ref=None, name="Other", created_by="b"
    )
    tagged = store.add_source(
        url="https://github.com/acme/skills.git", ref="v2", name="Acme v2", created_by="a"
    )

    assert again.id == first.id and again.name == "Acme"
    assert tagged.id != first.id
    assert sorted(s.name for s in store.list_sources().data) == ["Acme", "Acme v2"]
    assert store.get_source(first.id) == first
    assert store.delete_source(first.id) is True
    assert store.delete_source(first.id) is False
    assert [s.name for s in store.list_sources().data] == ["Acme v2"]


def test_users_see_org_skills_and_their_own_only(store: SqlAlchemySkillStore) -> None:
    _install(store, "shared", scope="org", owner=None)
    _install(store, "mine", owner="alice")
    _install(store, "theirs", owner="bob")
    # A user-scoped install with no user id: the single local user's own.
    local = _install(store, "local", owner=None)
    assert local.owner_user_id is None and local.scope == "user"

    # Same-second installs order by name, so the listing is stable.
    names = lambda user: sorted(s.name for s in store.list_for_user(user).data)  # noqa: E731
    assert names("alice") == ["mine", "shared"]
    assert names("bob") == ["shared", "theirs"]
    assert names("carol") == ["shared"]
    assert names(None) == ["local", "shared"]

    found = store.find_for_user("alice", ["mine", "theirs", "shared", "nope"])
    assert sorted(s.name for s in found) == ["mine", "shared"]
    assert sorted(s.name for s in store.find_for_user(None, ["local", "mine"])) == ["local"]
    assert store.find_for_user("alice", []) == []
    assert (store.count_installed("user", "alice"), store.count_installed("org", None)) == (1, 1)
    assert store.count_installed("user", None) == 1


def test_org_scope_ignores_the_owner_argument(store: SqlAlchemySkillStore) -> None:
    row = _install(store, "shared", scope="org", owner="alice")
    assert row.owner_user_id is None and row.scope == "org"


def test_concurrent_installs_of_one_identity_end_on_one_row(
    store: SqlAlchemySkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The race: this writer's pre-insert lookup misses while another writer
    # inserts the same (owner, name, scope). The unique index rejects the
    # second insert and the retry replaces the winner in place.
    first = _install(store, "review", commit="a" * 40)
    misses = [True]
    real_scalar = Session.scalar

    def scalar_missing_once(self: Session, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        if misses and misses.pop():
            return None
        return real_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", scalar_missing_once)
    second = _install(store, "review", commit="b" * 40)
    assert second.id == first.id and second.commit == "b" * 40
    assert [s.name for s in store.list_for_user("alice").data] == ["review"]


def test_visibility_reads_stay_on_the_owner_indexes(
    store: SqlAlchemySkillStore, db_uri: str
) -> None:
    if not db_uri.startswith("sqlite"):
        pytest.skip("query plans are asserted on SQLite")
    engine = get_or_create_engine(db_uri)
    plans: list[str] = []

    def capture(connection, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.startswith("SELECT"):
            rows = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
            plans.append(" | ".join(row[-1] for row in rows.all()))

    for index in range(3):
        _install(store, f"s{index}")
        _install(store, f"o{index}", scope="org", owner=None)
    store.add_source(url="https://github.com/acme/skills.git", ref=None, name="a", created_by=None)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = store.list_for_user("alice", limit=2)
        store.list_for_user("alice", limit=2, after=page.last_id)
        store.find_for_user("alice", ["s1", "o2"])
        store.count_installed("user", "alice")
        store.find_source("https://github.com/acme/skills.git", None)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert plans, "the store's own connection must be instrumented"
    # Every read is an index search; nothing scans a table or sorts in a temp B-tree.
    assert all("SEARCH" in plan and "USING" in plan for plan in plans), plans
    assert not any("SCAN " in plan or "TEMP B-TREE" in plan for plan in plans), plans
    assert any("ix_installed_skills_owner_order" in plan for plan in plans)
    assert any("ux_installed_skills_identity" in plan for plan in plans)
    assert any("ix_skill_sources_identity" in plan for plan in plans)


def test_reinstall_replaces_in_place_and_keeps_the_id(store: SqlAlchemySkillStore) -> None:
    first = _install(store, "review", commit="a" * 40)
    second = _install(store, "review", commit="b" * 40, description="newer")

    assert second.id == first.id
    assert second.commit == "b" * 40 and second.description == "newer"
    assert second.updated_at is not None and second.installed_at == first.installed_at
    assert len(store.list_for_user("alice").data) == 1
    # The same name under another user is a different row.
    other = _install(store, "review", owner="bob")
    assert other.id != first.id


def test_update_toggle_and_delete(store: SqlAlchemySkillStore) -> None:
    row = _install(store, "review")

    updated = store.update(
        row.id,
        commit="c" * 40,
        bundle_location="skills/review-2.tar.gz",
        description="d2",
        display_name="Review",
        path="skills/moved/review",
    )
    assert updated is not None
    assert (updated.commit, updated.bundle_location, updated.display_name, updated.path) == (
        "c" * 40,
        "skills/review-2.tar.gz",
        "Review",
        "skills/moved/review",
    )

    disabled = store.set_enabled(row.id, False)
    assert disabled is not None and disabled.enabled is False
    assert store.get(row.id) is not None and store.get(row.id).enabled is False

    gone = store.delete(row.id)
    assert gone is not None and gone.bundle_location == "skills/review-2.tar.gz"
    assert store.get(row.id) is None
    assert store.delete(row.id) is None
    assert (
        store.update(row.id, commit=None, bundle_location="x", description="", display_name=None)
        is None
    )
    assert store.set_enabled(row.id, True) is None


def test_list_pages_continue_without_omissions_or_duplicates(store: SqlAlchemySkillStore) -> None:
    for i in range(7):
        _install(store, f"s{i:02d}", scope="org", owner=None)
    seen: list[str] = []
    after = None
    pages = 0
    while True:
        page = store.list_for_user("alice", limit=3, after=after)
        pages += 1
        seen.extend(s.name for s in page.data)
        assert len(page.data) <= 3
        if not page.has_more:
            assert page.last_id == (page.data[-1].id if page.data else None)
            break
        after = page.last_id
    assert pages == 3
    assert sorted(seen) == [f"s{i:02d}" for i in range(7)] and len(seen) == len(set(seen))

    for i in range(4):
        store.add_source(
            url=f"https://github.com/acme/r{i}.git", ref=None, name=f"r{i}", created_by=None
        )
    first = store.list_sources(limit=3)
    rest = store.list_sources(limit=3, after=first.last_id)
    assert first.has_more and not rest.has_more
    assert len(first.data) + len(rest.data) == 4
    assert len({s.id for s in first.data + rest.data}) == 4
    with pytest.raises(SkillValueError, match="unknown cursor"):
        store.list_sources(after="0" * 32)


def test_page_size_is_clamped(store: SqlAlchemySkillStore) -> None:
    for i in range(3):
        _install(store, f"s{i}", scope="org", owner=None)
    assert len(store.list_for_user("alice", limit=0).data) == 1
    assert len(store.list_for_user("alice", limit=10_000).data) == 3


def test_values_that_would_not_fit_are_rejected_before_writing(
    store: SqlAlchemySkillStore,
) -> None:
    # Multibyte text counts in characters, matching the column declarations.
    with pytest.raises(SkillValueError, match="description"):
        _install(store, "wordy", description="é" * 1025)
    with pytest.raises(SkillValueError, match="display_name"):
        _install(store, "titled", display_name="名" * 257)
    with pytest.raises(SkillValueError, match="url"):
        store.add_source(
            url="https://github.com/" + "a" * 600, ref=None, name="x", created_by=None
        )
    # Identities are bounded too; a trusted header is not a stored-byte limit.
    with pytest.raises(SkillValueError, match="owner_user_id"):
        _install(store, "whose", owner="u" * 129)
    with pytest.raises(SkillValueError, match="installed_by"):
        _install(store, "whose", installed_by="u" * 129)
    with pytest.raises(SkillValueError, match="created_by"):
        store.add_source(url="https://github.com/a/b", ref=None, name="x", created_by="u" * 129)
    assert store.list_for_user("alice").data == []
    row = _install(store, "ok")
    with pytest.raises(SkillValueError, match="description"):
        store.update(
            row.id, commit=None, bundle_location="x", description="z" * 1025, display_name=None
        )
    assert store.get(row.id).description == "ok description"
