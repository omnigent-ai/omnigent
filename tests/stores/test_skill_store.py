"""The skill store: source dedup, principal visibility, atomic installs, budgets and deletes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from omnigent.db.utils import get_or_create_engine
from omnigent.skills.limits import MAX_REF_CHARS, MAX_SOURCE_PATH_CHARS, MAX_URL_CHARS
from omnigent.stores.skill_store import SkillInstall, SkillQuotaError, SkillValueError
from omnigent.stores.skill_store.sqlalchemy_store import SqlAlchemySkillStore

URL = "https://github.com/acme/skills.git"


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySkillStore:
    return SqlAlchemySkillStore(db_uri)


def _item(name: str, **over) -> SkillInstall:  # type: ignore[no-untyped-def]  # test inputs
    fields = {
        "name": name,
        "display_name": None,
        "description": f"{name} description",
        "source_url": URL,
        "ref": None,
        "commit": "a" * 40,
        "path": f"skills/{name}",
        "bundle_location": f"skills/{name}.tar.gz",
        "bundle_bytes": 10,
        "bundle_entries": 3,
        "user_invocable": True,
    }
    fields.update(over)
    return SkillInstall(**fields)  # type: ignore[arg-type]


def _install(store: SqlAlchemySkillStore, name: str, *, scope="user", owner="alice", **over):  # type: ignore[no-untyped-def]
    return store.install_many(
        [_item(name, **over)], scope=scope, owner_user_id=owner, installed_by=owner
    )[0].skill


def test_sources_dedupe_on_url_and_ref(store: SqlAlchemySkillStore) -> None:
    first = store.add_source(url=URL, ref=None, name="Acme", created_by="a")
    again = store.add_source(url=URL, ref=None, name="Other", created_by="b")
    tagged = store.add_source(url=URL, ref="v2", name="Acme v2", created_by="a")

    assert again.id == first.id and again.name == "Acme" and first.ref is None
    assert tagged.id != first.id and tagged.ref == "v2"
    assert sorted(s.name for s in store.list_sources().data) == ["Acme", "Acme v2"]
    assert store.get_source(first.id) == first
    assert store.find_source(URL, None) == first and store.find_source(URL, "v9") is None
    assert store.delete_source(first.id) is True
    assert store.delete_source(first.id) is False
    assert [s.name for s in store.list_sources().data] == ["Acme v2"]


def test_source_lookups_answer_absent_for_values_no_source_row_can_hold(
    store: SqlAlchemySkillStore,
) -> None:
    at_limit = "p" * MAX_SOURCE_PATH_CHARS
    store.add_source(url=URL, ref=None, path="", name="root", created_by=None)
    store.add_source(url=URL, ref=None, path=at_limit, name="deep", created_by=None)
    too_long_url = "https://github.com/" + "a" * MAX_URL_CHARS
    assert store.find_source(URL, None, at_limit) is not None
    assert store.find_source(URL, None, at_limit + "p") is None
    assert store.find_source(too_long_url, None) is None
    assert store.find_source(URL, "r" * (MAX_REF_CHARS + 1)) is None
    # Ancestors too long for a source row are dropped; the shorter ones still match.
    longer = at_limit + "/skill"
    assert store.listed_source_paths(URL, None, ["", at_limit, longer]) == {"", at_limit}
    assert store.listed_source_paths(too_long_url, None, [""]) == set()
    assert store.listed_source_paths(URL, "r" * (MAX_REF_CHARS + 1), [""]) == set()


def test_concurrent_source_seeding_ends_on_one_row(
    store: SqlAlchemySkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two replicas seed the same configured source: the second one's lookup
    # misses, its insert hits the unique index, and the retry returns the winner.
    first = store.add_source(url=URL, ref=None, name="Acme", created_by=None)
    misses = [True]
    real_scalar = Session.scalar

    def scalar_missing_once(self: Session, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        if misses and misses.pop():
            return None
        return real_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", scalar_missing_once)
    assert store.add_source(url=URL, ref=None, name="Acme", created_by=None).id == first.id
    assert len(store.list_sources().data) == 1


def test_users_see_org_skills_and_their_own_only(store: SqlAlchemySkillStore) -> None:
    # An org install ignores the owner argument: it is everyone's.
    shared = _install(store, "shared", scope="org", owner="alice")
    assert shared.owner_user_id is None and shared.scope == "org"
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


def test_the_principal_row_keeps_its_totals_through_every_write(
    store: SqlAlchemySkillStore, db_uri: str
) -> None:
    # Quota checks read these totals under the lock instead of scanning rows,
    # so every write path has to keep them exact.
    from sqlalchemy.orm import Session as OrmSession

    from omnigent.db.db_models import SqlInstalledSkill, SqlSkillPrincipal

    def totals() -> tuple[int, int]:
        with OrmSession(get_or_create_engine(db_uri)) as session:
            lock = session.get(SqlSkillPrincipal, (0, "user:alice"))
            rows = session.query(SqlInstalledSkill).filter_by(principal="user:alice").all()
            expected = (len(rows), sum(r.bundle_bytes for r in rows if r.enabled))
            entries = sum(r.bundle_entries for r in rows if r.enabled)
            assert lock is not None and (lock.installed_count, lock.enabled_bytes) == expected
            assert lock.enabled_entries == entries
            return expected

    a = _install(store, "a", bundle_bytes=10, bundle_entries=4)
    b = _install(store, "b", bundle_bytes=20, bundle_entries=7)
    assert totals() == (2, 30)
    store.set_enabled(b.id, False)
    assert totals() == (2, 10)
    store.update(
        a.id,
        commit=None,
        bundle_location="skills/a2.tar.gz",
        bundle_bytes=15,
        bundle_entries=3,
        description="d",
        display_name=None,
    )
    assert totals() == (2, 15)
    _install(store, "a", bundle_bytes=5)  # a reinstall replaces the enabled row's bytes
    assert totals() == (2, 5)
    store.set_enabled(b.id, True)
    assert totals() == (2, 25)
    store.delete(a.id)
    assert totals() == (1, 20)
    assert store.count_installed("user", "alice") == 1


def test_a_batch_installs_all_or_nothing_within_the_budgets(store: SqlAlchemySkillStore) -> None:
    results = store.install_many(
        [_item("a"), _item("b", bundle_bytes=30)],
        scope="user",
        owner_user_id="alice",
        installed_by="alice",
    )
    assert [r.skill.name for r in results] == ["a", "b"]
    assert all(r.replaced_location is None for r in results)

    # Past the row cap: nothing of the batch is written, not even the first name.
    with pytest.raises(SkillQuotaError, match="At most 3 skills"):
        store.install_many(
            [_item("c"), _item("d")],
            scope="user",
            owner_user_id="alice",
            installed_by="alice",
            max_rows=3,
        )
    assert sorted(s.name for s in store.list_for_user("alice").data) == ["a", "b"]

    # Past the byte budget, counting only enabled rows; a disabled one frees its bytes.
    with pytest.raises(SkillQuotaError, match="exceed 0 MB"):
        store.install_many(
            [_item("c", bundle_bytes=70)],
            scope="user",
            owner_user_id="alice",
            installed_by="alice",
            max_bytes=100,
        )
    store.set_enabled(results[1].skill.id, False)
    store.install_many(
        [_item("c", bundle_bytes=70)],
        scope="user",
        owner_user_id="alice",
        installed_by="alice",
        max_bytes=100,
    )
    with pytest.raises(SkillQuotaError, match="Enabling b"):
        store.set_enabled(results[1].skill.id, True, max_bytes=100)
    with pytest.raises(SkillQuotaError, match="Updating c"):
        store.update(
            store.find_for_user("alice", ["c"])[0].id,
            commit=None,
            bundle_location="skills/c2.tar.gz",
            bundle_bytes=95,
            bundle_entries=3,
            description="d",
            display_name=None,
            max_bytes=100,
        )
    # Past the entry budget: a runner's unpack limit counts files as well as bytes.
    with pytest.raises(SkillQuotaError, match="exceed 10 files"):
        store.install_many(
            [_item("e", bundle_entries=5)],
            scope="user",
            owner_user_id="alice",
            installed_by="alice",
            max_entries=10,
        )
    assert store.find_for_user("alice", ["e"]) == []
    # A repeated name in one batch is refused before anything is written.
    with pytest.raises(SkillValueError, match="repeated"):
        store.install_many(
            [_item("x"), _item("x")], scope="user", owner_user_id="alice", installed_by="alice"
        )


def test_concurrent_installs_near_capacity_cannot_both_pass(
    store: SqlAlchemySkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two distinct-name installs race for the last slot from two threads. The
    # first holds the principal's lock (SQLite: the immediate transaction)
    # while the second starts, so the second counts after the first committed
    # and is refused; without the lock both would count one row and both pass.
    import threading
    import time

    from omnigent.stores.skill_store import sqlalchemy_store as module

    _install(store, "first")
    real_lock = module.SqlAlchemySkillStore._lock_principal
    real_ensure = module.SqlAlchemySkillStore._ensure_principal
    first_writer_holds = threading.Event()
    second_writer_at_the_door = threading.Event()
    slow = [True]

    def lock_slowly(session: Session, principal: str):  # type: ignore[no-untyped-def]
        lock = real_lock(session, principal)
        if slow and slow.pop():
            # Hold the principal inside the transaction until the other writer
            # is past its own preamble and waiting on this very lock.
            first_writer_holds.set()
            second_writer_at_the_door.wait(timeout=5)
            time.sleep(0.2)
        return lock

    def ensure_then_signal(self, principal: str) -> None:  # type: ignore[no-untyped-def]
        real_ensure(self, principal)
        if first_writer_holds.is_set():
            second_writer_at_the_door.set()

    monkeypatch.setattr(module.SqlAlchemySkillStore, "_lock_principal", staticmethod(lock_slowly))
    monkeypatch.setattr(module.SqlAlchemySkillStore, "_ensure_principal", ensure_then_signal)
    outcomes: dict[str, object] = {}

    def install(name: str) -> None:
        try:
            store.install_many(
                [_item(name)],
                scope="user",
                owner_user_id="alice",
                installed_by="alice",
                max_rows=2,
            )
            outcomes[name] = "ok"
        except SkillQuotaError as exc:
            outcomes[name] = exc

    first = threading.Thread(target=install, args=("second",))
    first.start()
    assert first_writer_holds.wait(timeout=5)
    second = threading.Thread(target=install, args=("third",))
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert outcomes["second"] == "ok"
    assert isinstance(outcomes["third"], SkillQuotaError)
    assert sorted(s.name for s in store.list_for_user("alice").data) == ["first", "second"]


def test_visibility_reads_stay_on_the_principal_indexes(
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
    store.add_source(url=URL, ref=None, name="a", created_by=None)
    store.add_source(url=URL, ref="v2", name="b", created_by=None)
    first_sources = store.list_sources(limit=1)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = store.list_for_user("alice", limit=2)
        store.list_for_user("alice", limit=2, after=page.last_id)
        continuations = len(plans)
        store.list_sources(limit=1, after=first_sources.last_id)
        store.find_for_user("alice", ["s1", "o2"])
        store.count_installed("user", "alice")
        store.find_source(URL, None)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert plans, "the store's own connection must be instrumented"
    # Every read is an index search; nothing scans a table or sorts in a temp B-tree.
    assert all("SEARCH" in plan and "USING" in plan for plan in plans), plans
    assert not any("SCAN " in plan or "TEMP B-TREE" in plan for plan in plans), plans
    assert any("ix_installed_skills_principal_order" in plan for plan in plans)
    assert any("ux_installed_skills_identity" in plan for plan in plans)
    assert any("ux_skill_sources_identity" in plan for plan in plans)
    # A continuation seeks to its cursor: the cursor columns bound the index
    # range, so later pages do not walk the rows before them.
    assert all(
        "(installed_at,id)>(?,?)" in plan for plan in plans[continuations - 2 : continuations]
    ), plans
    assert "ix_skill_sources_order" in plans[continuations]
    assert "(created_at,id)>(?,?)" in plans[continuations], plans


def test_reinstall_replaces_in_place_and_reports_the_old_tarball(
    store: SqlAlchemySkillStore,
) -> None:
    first = _install(store, "review", commit="a" * 40)
    results = store.install_many(
        [
            _item(
                "review", commit="b" * 40, description="newer", bundle_location="skills/r2.tar.gz"
            )
        ],
        scope="user",
        owner_user_id="alice",
        installed_by="alice",
    )
    second = results[0].skill
    assert results[0].replaced_location == "skills/review.tar.gz"
    assert second.id == first.id
    assert second.commit == "b" * 40 and second.description == "newer"
    assert second.updated_at is not None and second.installed_at == first.installed_at
    assert len(store.list_for_user("alice").data) == 1
    # Same content again: nothing to drop.
    assert (
        store.install_many(
            [_item("review", commit="b" * 40, bundle_location="skills/r2.tar.gz")],
            scope="user",
            owner_user_id="alice",
            installed_by="alice",
        )[0].replaced_location
        is None
    )
    # The same name under another user is a different row.
    other = _install(store, "review", owner="bob")
    assert other.id != first.id


def test_update_toggle_and_delete(store: SqlAlchemySkillStore) -> None:
    row = _install(store, "review")

    result = store.update(
        row.id,
        commit="c" * 40,
        bundle_location="skills/review-2.tar.gz",
        bundle_bytes=42,
        bundle_entries=3,
        description="d2",
        display_name="Review",
        path="skills/moved/review",
    )
    assert result is not None
    updated = result.skill
    assert (
        updated.commit,
        updated.bundle_location,
        updated.bundle_bytes,
        updated.display_name,
        updated.path,
    ) == ("c" * 40, "skills/review-2.tar.gz", 42, "Review", "skills/moved/review")
    # The tarball to drop is the one the row held under the lock; the same
    # location again replaces nothing.
    assert result.replaced_location == "skills/review.tar.gz"
    same = store.update(
        row.id,
        commit="c" * 40,
        bundle_location="skills/review-2.tar.gz",
        bundle_bytes=42,
        bundle_entries=3,
        description="d2",
        display_name="Review",
    )
    assert same is not None and same.replaced_location is None

    disabled = store.set_enabled(row.id, False)
    assert disabled is not None and disabled.enabled is False
    assert store.get(row.id) is not None and store.get(row.id).enabled is False

    gone = store.delete(row.id)
    assert gone is not None and gone.bundle_location == "skills/review-2.tar.gz"
    assert store.get(row.id) is None
    assert store.delete(row.id) is None
    assert (
        store.update(
            row.id,
            commit=None,
            bundle_location="x",
            bundle_bytes=1,
            bundle_entries=3,
            description="",
            display_name=None,
        )
        is None
    )
    assert store.set_enabled(row.id, True) is None


def test_list_pages_continue_without_omissions_or_duplicates(
    store: SqlAlchemySkillStore, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        assert (page.last_id is None) == (not page.data)
        if not page.has_more:
            break
        # The cursor row is removed before the next page is read: the walk
        # carries on from its position instead of failing or stopping short.
        store.delete(page.data[-1].id)
        after = page.last_id
    assert pages == 3
    assert sorted(seen) == [f"s{i:02d}" for i in range(7)] and len(seen) == len(set(seen))

    for i in range(4):
        store.add_source(
            url=f"https://github.com/acme/r{i}.git", ref=None, name=f"r{i}", created_by=None
        )
    first = store.list_sources(limit=3)
    assert store.delete_source(first.data[-1].id)
    rest = store.list_sources(limit=3, after=first.last_id)
    assert first.has_more and not rest.has_more
    assert len(first.data) + len(rest.data) == 4
    assert len({s.id for s in first.data + rest.data}) == 4
    # The last is a 19-digit epoch past signed 64-bit.
    for bad in ("0" * 32, "1.zz", "x", "-1." + "0" * 32, "9" * 19 + "." + "0" * 32):
        with pytest.raises(SkillValueError, match="invalid cursor"):
            store.list_sources(after=bad)
        with pytest.raises(SkillValueError, match="invalid cursor"):
            store.list_for_user("alice", after=bad)

    # One page merges the org's and the user's partitions; with equal timestamps
    # the id decides, and a cursor from either partition continues both.
    from omnigent.stores.skill_store import sqlalchemy_store

    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: 1_700_000_000)
    # Ids rise in install order, so pages of two end on an org row, then a user row.
    ids = (uuid.UUID(int=n) for n in range(1, 100))
    monkeypatch.setattr(sqlalchemy_store.uuid, "uuid4", lambda: next(ids))
    for i, scope in enumerate(("user", "org", "org", "user", "user", "org")):
        _install(store, f"m{i}", scope=scope, owner="bob" if scope == "user" else None)
        _install(store, f"other{i}", owner="carol")
    merged: list[tuple[int, str]] = []
    cursor_scopes: set[str] = set()
    after = None
    while True:
        page = store.list_for_user("bob", limit=2, after=after)
        merged.extend((s.installed_at, s.id) for s in page.data if s.name.startswith("m"))
        if not page.has_more:
            break
        cursor_scopes.add(page.data[-1].scope)
        after = page.last_id
    assert len(merged) == 6 and len(set(merged)) == 6
    assert merged == sorted(merged)
    assert cursor_scopes == {"org", "user"}


def test_the_user_invocable_flag_is_stored_and_refetched(store: SqlAlchemySkillStore) -> None:
    # An internal skill installs like any other; the menu is what filters it.
    internal = _install(store, "orchestrate", user_invocable=False)
    assert internal.user_invocable is False
    assert _install(store, "review").user_invocable is True
    assert store.get(internal.id).user_invocable is False
    assert store.find_for_user("alice", ["orchestrate"])[0].user_invocable is False

    # An update refetches the skill, so a frontmatter change takes effect.
    result = store.update(
        internal.id,
        commit="b" * 40,
        bundle_location="skills/orchestrate-2.tar.gz",
        bundle_bytes=10,
        bundle_entries=3,
        description="now public",
        display_name=None,
        user_invocable=True,
    )
    assert result is not None and result.skill.user_invocable is True


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
        store.install_many(
            [_item("whose")], scope="user", owner_user_id="alice", installed_by="u" * 129
        )
    with pytest.raises(SkillValueError, match="created_by"):
        store.add_source(url="https://github.com/a/b", ref=None, name="x", created_by="u" * 129)
    assert store.list_for_user("alice").data == []
    row = _install(store, "ok")
    with pytest.raises(SkillValueError, match="description"):
        store.update(
            row.id,
            commit=None,
            bundle_location="x",
            bundle_bytes=1,
            bundle_entries=3,
            description="z" * 1025,
            display_name=None,
        )
    assert store.get(row.id).description == "ok description"


def test_every_write_changes_the_installed_revision(store: SqlAlchemySkillStore) -> None:
    # A warm session's turns carry this token; any change to what its owner
    # would be delivered has to change it, or the runner keeps a stale spec.
    seen = [store.installed_revision("alice")]

    def changed() -> None:
        current = store.installed_revision("alice")
        assert current not in seen
        seen.append(current)

    row = _install(store, "a")
    changed()
    _install(store, "a", bundle_bytes=10)  # a same-size reinstall still counts
    changed()
    store.set_enabled(row.id, False)
    changed()
    store.update(
        row.id,
        commit=None,
        bundle_location="skills/a2.tar.gz",
        bundle_bytes=10,
        bundle_entries=3,
        description="d",
        display_name=None,
    )
    changed()
    _install(store, "shared", scope="org")
    changed()
    store.delete(row.id)
    changed()
    assert store.installed_revision("bob") != store.installed_revision("alice")


def test_descriptive_fields_round_trip_through_their_storage_envelope(
    store: SqlAlchemySkillStore, db_uri: str
) -> None:
    # Callers see plain fields; the table holds one opaque, bounded envelope.
    from omnigent.db.envelope import ENVELOPE_VERSION, PLAINTEXT

    installed = _install(store, "review", display_name="Code Review", description="Review — 名")
    assert (installed.display_name, installed.description) == ("Code Review", "Review — 名")
    updated = store.update(
        installed.id,
        commit=None,
        bundle_location="skills/review-2.tar.gz",
        bundle_bytes=10,
        bundle_entries=3,
        description="Review a diff",
        display_name=None,
    )
    assert updated is not None
    assert (updated.skill.display_name, updated.skill.description) == (None, "Review a diff")
    with get_or_create_engine(db_uri).connect() as connection:
        stored = connection.exec_driver_sql(
            "SELECT metadata_envelope FROM installed_skills WHERE name = 'review'"
        ).scalar_one()
    assert bytes(stored[:2]) == bytes((ENVELOPE_VERSION, PLAINTEXT))
    assert store.get(installed.id).description == "Review a diff"
