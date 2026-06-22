"""Guard that every Alembic migration is SQLite-compatible.

Omnigent runs the same migration chain against Postgres/Lakebase (the
server) and a local SQLite ``chat.db`` (the machine-global default). SQLite's
``ALTER TABLE`` is far more limited than Postgres': it cannot ``DROP COLUMN``
(before SQLite 3.35), ``ALTER COLUMN``, or add/drop most constraints. Alembic's
supported way to do those on SQLite is *batch mode*
(``op.batch_alter_table``), which recreates the table.

A migration that calls ``op.drop_column`` / ``op.alter_column`` directly emits
raw ``ALTER TABLE ... DROP COLUMN``, which crashes on older SQLite with
``near "DROP": syntax error`` — exactly the failure a customer hit on
``5db033a3d4b7`` after the SQLite ``chat.db`` shipped. Modern SQLite (>= 3.35)
accepts ``DROP COLUMN``, so a runtime "upgrade head" test on a new SQLite build
(e.g. CI) passes even when the migration is broken for older clients. The
static guard below is therefore the real cross-version protection; the runtime
round-trip complements it by exercising the batch blocks end-to-end.
"""

from __future__ import annotations

import ast
import tempfile
import warnings
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import omnigent.db

# DDL ops that SQLite's native ALTER TABLE cannot perform — they MUST be issued
# through ``op.batch_alter_table`` (table recreate). Index ops
# (``create_index`` / ``drop_index``) are intentionally absent: SQLite supports
# ``CREATE INDEX`` / ``DROP INDEX`` directly, so calling them on the ``op``
# proxy is safe.
_SQLITE_UNSAFE_OPS = frozenset(
    {
        "drop_column",
        "alter_column",
        "drop_constraint",
        "create_foreign_key",
        "create_unique_constraint",
        "create_check_constraint",
    }
)

_VERSIONS_DIR = Path(omnigent.db.__file__).parent / "migrations" / "versions"


def _raw_unsafe_op_calls(source: str) -> list[tuple[str, int]]:
    """Return ``(op_name, lineno)`` for raw ``op.<unsafe>(...)`` calls in *source*.

    Only flags calls on the bare ``op`` proxy (``op.drop_column(...)``), not
    ``batch_op.drop_column(...)`` inside a ``with op.batch_alter_table(...)``
    block — the latter is the correct, SQLite-safe form. Detection is the
    receiver name (``op`` vs ``batch_op``), matching how Alembic routes the
    call.

    :param source: The full source text of one migration module.
    :returns: One entry per offending call site, in source order.
    """
    tree = ast.parse(source)
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _SQLITE_UNSAFE_OPS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
        ):
            offenders.append((node.func.attr, node.lineno))
    return offenders


def test_no_migration_uses_sqlite_unsafe_raw_ddl() -> None:
    """Every migration must batch SQLite-unsafe DDL instead of calling it raw.

    Scans all version modules and fails if any issues ``op.drop_column`` /
    ``op.alter_column`` / a constraint op directly. This is the protection that
    holds on EVERY SQLite version — a runtime ``upgrade head`` test passes on
    SQLite >= 3.35 even when the migration is broken for older clients.

    A failure here means a migration will crash on older SQLite with
    ``near "DROP": syntax error`` (or the constraint equivalent). The fix is to
    wrap the op in ``with op.batch_alter_table("<table>") as batch_op:`` and
    call ``batch_op.<op>(...)``.
    """
    version_files = sorted(_VERSIONS_DIR.glob("*.py"))
    # Sanity: the scan found the migration directory and it's populated. A 0
    # here would make the test vacuously pass — i.e. the guard checks nothing.
    assert len(version_files) > 20, (
        f"Expected the migrations/versions dir at {_VERSIONS_DIR} to hold the "
        f"full migration chain (>20 files); found {len(version_files)}. The "
        f"path is wrong, so this guard would scan nothing."
    )
    offenders: dict[str, list[tuple[str, int]]] = {}
    for path in version_files:
        raw = _raw_unsafe_op_calls(path.read_text())
        if raw:
            offenders[path.name] = raw
    assert offenders == {}, (
        "These migrations call SQLite-unsafe DDL on the bare `op` proxy; they "
        "will crash on SQLite (e.g. `ALTER TABLE ... DROP COLUMN` is rejected "
        "pre-3.35). Wrap each in `with op.batch_alter_table(<table>) as "
        f"batch_op:` and use `batch_op.<op>(...)`. Offenders: {offenders}"
    )


def test_full_migration_chain_round_trips_on_sqlite() -> None:
    """Upgrade to head, downgrade to base, and re-upgrade on a fresh SQLite DB.

    Exercises every migration's ``upgrade`` AND ``downgrade`` on SQLite —
    including the batch ``drop_column`` blocks this change added — so a
    malformed batch conversion (bad table name, wrong column, broken data
    migration) fails loudly. The downgrade leg matters because the
    ``get_or_create_engine`` fixtures elsewhere only ever run ``upgrade head``,
    leaving downgrade batch blocks otherwise uncovered.

    On SQLite >= 3.35 this would also pass with raw ``DROP COLUMN``; it does NOT
    replace :func:`test_no_migration_uses_sqlite_unsafe_raw_ddl` (which is the
    version-independent guard) — it verifies the conversions are valid SQL and
    that the chain is reversible.
    """
    with tempfile.TemporaryDirectory() as tmp:
        uri = f"sqlite:///{Path(tmp) / 'chain.db'}"
        config = Config()
        # Point Alembic at the real script tree but supply our own SQLite URL,
        # so the test owns its invocation rather than reaching into the
        # production engine helper.
        config.set_main_option(
            "script_location", str(Path(omnigent.db.__file__).parent / "migrations")
        )
        config.set_main_option("sqlalchemy.url", uri)

        # Some downgrades log expected data-loss warnings; they're not failures.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            command.upgrade(config, "head")
            command.downgrade(config, "base")
            command.upgrade(config, "head")

        engine = sa.create_engine(uri)
        try:
            inspector = sa.inspect(engine)
            tables = set(inspector.get_table_names())
            # The chain reached head: the comments table exists (created mid-chain
            # and never dropped at head). If absent, upgrade didn't complete.
            assert "comments" in tables, (
                f"`comments` table missing after upgrade head; chain did not "
                f"complete. Tables present: {sorted(tables)}"
            )
            comment_cols = {c["name"] for c in inspector.get_columns("comments")}
            # The legacy single-line anchor column was dropped by 5db033a3d4b7
            # via batch mode. Its presence would mean that migration's batch
            # drop silently no-op'd (the original customer-facing bug surfaced
            # as a crash; a regression could instead skip the drop).
            assert "line" not in comment_cols, (
                f"`comments.line` should have been dropped by migration "
                f"5db033a3d4b7; still present. Columns: {sorted(comment_cols)}"
            )
        finally:
            engine.dispose()
