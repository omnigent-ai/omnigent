"""The agents table gains nullable git-source columns; existing rows read NULL."""

from __future__ import annotations

from sqlalchemy import inspect

from omnigent.db.db_models import SqlAgent
from omnigent.db.utils import get_or_create_engine

# All five git-provenance columns ship together in ga10gitsrc01, including
# git_host_id (which folds in the former ga20githost1 migration).
_GIT_COLUMNS = ("git_url", "git_ref", "git_subpath", "git_commit", "git_host_id")


def test_sqlagent_has_git_columns() -> None:
    cols = {c.name for c in SqlAgent.__table__.columns}
    assert set(_GIT_COLUMNS) <= cols


def test_git_columns_are_nullable() -> None:
    by_name = {c.name: c for c in SqlAgent.__table__.columns}
    for name in _GIT_COLUMNS:
        assert by_name[name].nullable is True


def test_fresh_engine_creates_git_columns(tmp_path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    from omnigent.db.db_models import OmnigentBase

    OmnigentBase.metadata.create_all(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("agents")}
    assert set(_GIT_COLUMNS) <= cols


def test_migration_upgrade_downgrade_reversible(tmp_path) -> None:
    """Exercise ga10gitsrc01 up→down against a scratch DB via alembic.

    The other tests only introspect the ORM. This drives alembic to head, then
    down one revision (reverting exactly ga10gitsrc01), asserting the
    migration's reversibility — all five git columns appear on upgrade and are
    gone on downgrade.
    """
    import os
    import subprocess
    from pathlib import Path

    from sqlalchemy import create_engine, text

    db_url = f"sqlite:///{tmp_path / 'ga10_migrate.db'}"
    repo_root = str(Path(__file__).parent.parent.parent.resolve())
    ini_path = f"{repo_root}/omnigent/db/alembic.ini"
    env = {**os.environ, "OMNIGENT_DB_URL": db_url}

    def _alembic(*args: str) -> None:
        result = subprocess.run(
            ["uv", "run", "alembic", "-c", ini_path, *args],
            capture_output=True,
            text=True,
            cwd=repo_root,
            env=env,
        )
        assert result.returncode == 0, f"alembic {args} failed:\n{result.stdout}\n{result.stderr}"

    _alembic("upgrade", "head")
    # After head, all five git columns are present.
    up_cols = {c["name"] for c in inspect(create_engine(db_url)).get_columns("agents")}
    assert {"git_url", "git_ref", "git_subpath", "git_commit", "git_host_id"} <= up_cols

    # Downgrade exactly one revision — reverts ga10gitsrc01, removing all
    # five git columns (relative -1 keeps this robust to what the parent
    # revision id happens to be).
    _alembic("downgrade", "-1")
    with create_engine(db_url).connect() as conn:
        down_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(agents)"))}
    assert not ({"git_url", "git_ref", "git_subpath", "git_commit", "git_host_id"} & down_cols), (
        f"git columns should be gone after downgrade, found: {down_cols}"
    )
