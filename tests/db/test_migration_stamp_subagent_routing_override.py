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
