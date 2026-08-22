"""Tests for the bridge-id label prefix migration (c4d5e6f7a8b9)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_PREVIOUS_REVISION = "b3c4d5e6f7a8"
_REVISION = "c4d5e6f7a8b9"
_SESSION_ID = "3066bdff8fbc4e9eafbd0978c4a61537"
_OTHER_ID = "348df11325544a27a380459299f2800f"


def _upgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, revision)


def _downgrade(uri: str, engine: sa.Engine, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, revision)


def _insert_labels(engine: sa.Engine, rows: list[tuple[str, str, str]]) -> None:
    with engine.begin() as conn:
        for conversation_id, key, value in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO conversation_labels"
                    " (workspace_id, conversation_id, key, value, updated_at)"
                    " VALUES (0, :cid, :key, :value, 1)"
                ),
                {"cid": conversation_id, "key": key, "value": value},
            )


def _label_values(engine: sa.Engine) -> dict[str, str]:
    with engine.begin() as conn:
        return {
            row[0]: row[1]
            for row in conn.execute(sa.text("SELECT key, value FROM conversation_labels"))
        }


def test_upgrade_strips_conv_prefix_from_bridge_id_labels(tmp_path: Path) -> None:
    """
    Bridge-id labels must end up spelling their session id the bare way.

    A label left at ``conv_<hex>`` while the session id is ``<hex>`` sends the
    harness executor and the terminal to two different bridge directories, so
    every turn fails the stale-session guard. Only the prefix goes: a
    ``-cleared`` marker and non-bridge-id labels survive verbatim.
    """
    uri = f"sqlite:///{tmp_path / 'bridge_labels.db'}"
    engine = sa.create_engine(uri)
    _upgrade(uri, engine, _PREVIOUS_REVISION)
    _insert_labels(
        engine,
        [
            (_SESSION_ID, "omnigent.claude_native.bridge_id", f"conv_{_SESSION_ID}"),
            (_SESSION_ID, "omnigent.codex_native.bridge_id", f"conv_{_SESSION_ID}-cleared"),
            (_OTHER_ID, "omnigent.opencode_native.bridge_id", _OTHER_ID),
            (_OTHER_ID, "omnigent.fork.source_id", f"conv_{_SESSION_ID}"),
        ],
    )

    _upgrade(uri, engine, _REVISION)

    assert _label_values(engine) == {
        "omnigent.claude_native.bridge_id": _SESSION_ID,
        "omnigent.codex_native.bridge_id": f"{_SESSION_ID}-cleared",
        "omnigent.opencode_native.bridge_id": _OTHER_ID,
        # Not a bridge id: resolved through a uuid column bind, which already
        # strips the legacy prefix, so the stored value stays as it is.
        "omnigent.fork.source_id": f"conv_{_SESSION_ID}",
    }


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    """A second run over already-stripped values must be a no-op."""
    uri = f"sqlite:///{tmp_path / 'bridge_labels_rerun.db'}"
    engine = sa.create_engine(uri)
    _upgrade(uri, engine, _PREVIOUS_REVISION)
    _insert_labels(
        engine,
        [(_SESSION_ID, "omnigent.claude_native.bridge_id", f"conv_{_SESSION_ID}")],
    )

    _upgrade(uri, engine, _REVISION)
    # downgrade() leaves the data alone, so re-upgrading replays upgrade().
    _downgrade(uri, engine, _PREVIOUS_REVISION)
    _upgrade(uri, engine, _REVISION)

    assert _label_values(engine) == {"omnigent.claude_native.bridge_id": _SESSION_ID}
