"""Tests for the ``subagent_routing_override`` stamp backfill (e6f7a8b9c0d1).

The spawn gate went from tri-state (unset = inherit the session's own or its
parent's ``cost_control_mode_override``) to two-state (only an explicit ``"on"``
routes). Live Smart Routing sessions carry no stamp, so the migration writes the
value the old inherit resolution produced — and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_BEFORE = "d5e6f7a8b9c0"
_AFTER = "e6f7a8b9c0d1"


def _new_engine(uri: str) -> sa.Engine:
    """
    Create a raw migration-test engine without auto-upgrading to head.

    :param uri: SQLAlchemy database URI, e.g. ``"sqlite:///tmp/test.db"``.
    :returns: SQLAlchemy engine for the migration under test.
    """
    return sa.create_engine(uri)


def _migrate(engine: sa.Engine, uri: str, revision: str, *, down: bool = False) -> None:
    """
    Run one Alembic upgrade / downgrade on a raw engine.

    :param engine: SQLAlchemy engine under migration.
    :param uri: SQLAlchemy database URI.
    :param revision: Alembic target revision.
    :param down: ``True`` to downgrade instead of upgrade.
    :returns: None.
    """
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if down else command.upgrade)(config, revision)


def _uuid(tag: str) -> bytes:
    """
    Build a stable 16-byte conversation id from a short readable tag.

    :param tag: Short label, e.g. ``"routed"``.
    :returns: 16 raw bytes suitable for the ``BLOB`` id columns.
    """
    return tag.encode().ljust(16, b"\0")[:16]


def _insert(engine: sa.Engine, rows: list[dict[str, object]]) -> None:
    """
    Insert conversation rows with only the columns this migration reads.

    :param engine: SQLAlchemy engine to write through.
    :param rows: One mapping per row: ``id``, ``session_overrides``, and an
        optional ``parent_conversation_id``.
    :returns: None.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations"
                " (workspace_id, id, created_at, updated_at, title,"
                "  parent_conversation_id, root_conversation_id, archived, session_overrides)"
                " VALUES (0, :id, 1700000000, 1700000000, '',"
                "  :parent_conversation_id, :id, 0, :session_overrides)"
            ),
            [{"parent_conversation_id": None, **row} for row in rows],
        )


def _overrides(engine: sa.Engine) -> dict[bytes, str | None]:
    """
    Read every row's raw ``session_overrides`` blob keyed by id.

    :param engine: SQLAlchemy engine to inspect.
    :returns: Mapping of raw id bytes to the stored blob.
    """
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id, session_overrides FROM conversations")).all()
    return {bytes(row[0]): row[1] for row in rows}


_ROUTED = json.dumps({"cost_control_mode_override": "on"}, separators=(",", ":"))
_UNROUTED = json.dumps({"cost_control_mode_override": "off"}, separators=(",", ":"))
_ROUTED_STAMPED = json.dumps(
    {"cost_control_mode_override": "on", "subagent_routing_override": "on"},
    separators=(",", ":"),
)


def _seed(engine: sa.Engine) -> None:
    """
    Seed the row shapes the backfill has to tell apart.

    :param engine: SQLAlchemy engine to write through.
    :returns: None.
    """
    _insert(
        engine,
        [
            # Routed with no stamp — the whole point of the migration.
            {"id": _uuid("routed"), "session_overrides": _ROUTED},
            # Already explicit: an intentional "off" must survive.
            {
                "id": _uuid("routed-off"),
                "session_overrides": json.dumps(
                    {"cost_control_mode_override": "on", "subagent_routing_override": "off"},
                    separators=(",", ":"),
                ),
            },
            # Not routed: nothing to inherit, so nothing to write.
            {"id": _uuid("unrouted"), "session_overrides": _UNROUTED},
            {"id": _uuid("null-blob"), "session_overrides": None},
            # Children resolved through the parent under the old rules.
            {
                "id": _uuid("child-routed"),
                "parent_conversation_id": _uuid("routed"),
                "session_overrides": json.dumps(
                    {"harness_override": "auto"}, separators=(",", ":")
                ),
            },
            {
                "id": _uuid("child-unrouted"),
                "parent_conversation_id": _uuid("unrouted"),
                "session_overrides": json.dumps(
                    {"harness_override": "auto"}, separators=(",", ":")
                ),
            },
            # A routed row whose only other key sorts AFTER the new one: the
            # rewrite must restore the store's fixed key order, not append.
            {
                "id": _uuid("key-order"),
                "session_overrides": json.dumps(
                    {"cost_control_mode_override": "on", "harness_override": "auto"},
                    separators=(",", ":"),
                ),
            },
        ],
    )


def test_upgrade_stamps_only_the_rows_the_old_gate_would_have_routed(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'stamp.db'}"
    engine = _new_engine(uri)
    try:
        _migrate(engine, uri, _BEFORE)
        _seed(engine)

        _migrate(engine, uri, _AFTER)

        assert _overrides(engine) == {
            _uuid("routed"): _ROUTED_STAMPED,
            _uuid("routed-off"): json.dumps(
                {"cost_control_mode_override": "on", "subagent_routing_override": "off"},
                separators=(",", ":"),
            ),
            _uuid("unrouted"): _UNROUTED,
            _uuid("null-blob"): None,
            # Stamped from the parent, matching the old inherit resolution.
            _uuid("child-routed"): json.dumps(
                {"subagent_routing_override": "on", "harness_override": "auto"},
                separators=(",", ":"),
            ),
            _uuid("child-unrouted"): json.dumps(
                {"harness_override": "auto"}, separators=(",", ":")
            ),
            # The new key lands in the store's slot, before harness_override.
            _uuid("key-order"): json.dumps(
                {
                    "cost_control_mode_override": "on",
                    "subagent_routing_override": "on",
                    "harness_override": "auto",
                },
                separators=(",", ":"),
            ),
        }
    finally:
        engine.dispose()


def test_downgrade_leaves_the_backfilled_values_in_place(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'stamp-down.db'}"
    engine = _new_engine(uri)
    try:
        _migrate(engine, uri, _BEFORE)
        _seed(engine)
        _migrate(engine, uri, _AFTER)
        after_upgrade = _overrides(engine)

        _migrate(engine, uri, _BEFORE, down=True)

        # Deliberately a no-op: every stamped value equals what the old
        # tri-state gate already resolved, and clearing it could not tell a
        # backfill apart from a value the user chose since.
        assert _overrides(engine) == after_upgrade
    finally:
        engine.dispose()


def test_upgrade_leaves_unparseable_and_non_dict_blobs_alone(tmp_path: Path) -> None:
    """
    Add #36: a blob the migration cannot read is skipped, not fatal.

    ``session_overrides`` is written by the store, so in practice it is always a
    JSON object — but a hand-edited row, a truncated write, or a schema from a
    much older build is enough to make one unreadable, and a migration that
    raises on one row leaves the whole deployment un-upgradable.
    """
    uri = f"sqlite:///{tmp_path / 'stamp-junk.db'}"
    engine = _new_engine(uri)
    try:
        _migrate(engine, uri, _BEFORE)
        _insert(
            engine,
            [
                # Not JSON at all.
                {"id": _uuid("garbage"), "session_overrides": "{not json"},
                # Valid JSON, wrong shape: a list, a bare string, a JSON null.
                {"id": _uuid("list"), "session_overrides": "[]"},
                {"id": _uuid("string"), "session_overrides": '"on"'},
                {"id": _uuid("json-null"), "session_overrides": "null"},
                # Empty string, which is neither absent nor parseable.
                {"id": _uuid("empty"), "session_overrides": ""},
                # A routed row alongside them, so the migration is proven to have
                # done its job rather than bailed out at the first bad row.
                {"id": _uuid("routed"), "session_overrides": _ROUTED},
            ],
        )

        _migrate(engine, uri, _AFTER)

        stored = _overrides(engine)
        assert stored[_uuid("routed")] == _ROUTED_STAMPED
        for tag, unchanged in (
            ("garbage", "{not json"),
            ("list", "[]"),
            ("string", '"on"'),
            ("json-null", "null"),
            ("empty", ""),
        ):
            assert stored[_uuid(tag)] == unchanged, tag
    finally:
        engine.dispose()


def test_upgrade_handles_an_already_stamped_row_and_a_dangling_parent(tmp_path: Path) -> None:
    """
    Add #36, the other edges: an existing stamp and a parent that is not there.

    A row stamped by the create path before the migration ran must not be
    rewritten (the blob is fixed-width and a needless write costs space for no
    change), and a child whose parent row is missing has nothing to inherit — the
    old tri-state rule resolved that to "not routed", so the new stamp is absent.
    """
    uri = f"sqlite:///{tmp_path / 'stamp-edges.db'}"
    engine = _new_engine(uri)
    try:
        _migrate(engine, uri, _BEFORE)
        _insert(
            engine,
            [
                {"id": _uuid("already-on"), "session_overrides": _ROUTED_STAMPED},
                {
                    "id": _uuid("orphan"),
                    "parent_conversation_id": _uuid("gone"),
                    "session_overrides": json.dumps(
                        {"harness_override": "auto"}, separators=(",", ":")
                    ),
                },
            ],
        )

        _migrate(engine, uri, _AFTER)

        assert _overrides(engine) == {
            _uuid("already-on"): _ROUTED_STAMPED,
            _uuid("orphan"): json.dumps({"harness_override": "auto"}, separators=(",", ":")),
        }
    finally:
        engine.dispose()


def test_re_running_the_upgrade_changes_nothing(tmp_path: Path) -> None:
    """
    Add #37: the backfill is idempotent.

    A stamped row is indistinguishable from a row the user set by hand, so a
    second pass has to be a no-op — otherwise a re-run (a retried deploy, a
    downgrade-then-upgrade cycle) would overwrite a deliberate ``"off"``.
    """
    uri = f"sqlite:///{tmp_path / 'stamp-twice.db'}"
    engine = _new_engine(uri)
    try:
        _migrate(engine, uri, _BEFORE)
        _seed(engine)
        _migrate(engine, uri, _AFTER)
        once = _overrides(engine)

        _migrate(engine, uri, _BEFORE, down=True)
        _migrate(engine, uri, _AFTER)

        assert _overrides(engine) == once
    finally:
        engine.dispose()
