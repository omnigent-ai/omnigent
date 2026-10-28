"""The skill store: source dedup, scoped visibility, replace-on-reinstall, and deletes."""

from __future__ import annotations

import pytest

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
    assert sorted(s.name for s in store.list_sources()) == ["Acme", "Acme v2"]
    assert store.get_source(first.id) == first
    assert store.delete_source(first.id) is True
    assert store.delete_source(first.id) is False
    assert [s.name for s in store.list_sources()] == ["Acme v2"]


def test_users_see_org_skills_and_their_own_only(store: SqlAlchemySkillStore) -> None:
    _install(store, "shared", scope="org", owner=None)
    _install(store, "mine", owner="alice")
    _install(store, "theirs", owner="bob")

    # Same-second installs order by name, so the listing is stable.
    assert [s.name for s in store.list_for_user("alice")] == ["mine", "shared"]
    assert [s.name for s in store.list_for_user("bob")] == ["shared", "theirs"]
    assert [s.name for s in store.list_for_user("carol")] == ["shared"]


def test_org_scope_ignores_the_owner_argument(store: SqlAlchemySkillStore) -> None:
    row = _install(store, "shared", scope="org", owner="alice")
    assert row.owner_user_id is None and row.scope == "org"


def test_reinstall_replaces_in_place_and_keeps_the_id(store: SqlAlchemySkillStore) -> None:
    first = _install(store, "review", commit="a" * 40)
    second = _install(store, "review", commit="b" * 40, description="newer")

    assert second.id == first.id
    assert second.commit == "b" * 40 and second.description == "newer"
    assert second.updated_at is not None and second.installed_at == first.installed_at
    assert len(store.list_for_user("alice")) == 1
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
    )
    assert updated is not None
    assert (updated.commit, updated.bundle_location, updated.display_name) == (
        "c" * 40,
        "skills/review-2.tar.gz",
        "Review",
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


def test_list_reads_are_bounded(store: SqlAlchemySkillStore) -> None:
    for i in range(3):
        _install(store, f"s{i}", scope="org", owner=None)
    assert len(store.list_for_user("alice", limit=2)) == 2
    for i in range(3):
        store.add_source(
            url=f"https://github.com/acme/r{i}.git", ref=None, name=f"r{i}", created_by=None
        )
    assert len(store.list_sources(limit=2)) == 2
