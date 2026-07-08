"""Guard: the benchmark seed's pinned schema revision must equal the DB head.

The performance-benchmark seeder (``dev/benchmarks/omnigent/seed.py``) writes a
corpus shaped for the *current* DB schema and pins the schema it was authored
against in ``SEED_SCHEMA_REVISION``. When a migration moves the Alembic head,
the seed logic (and any cached/generated corpus) may no longer match the schema
— e.g. a new NOT NULL column the seeder doesn't populate. This check fails so
the schema change and the seed are updated together.

Unlike ``sync_version_py.py`` this is **check-only**: there's no safe automatic
fix — regenerating the seed corpus and confirming the seeder still populates
every required column is a human step. On drift, do:

1. Update ``seed.py`` if the new schema needs new columns/values seeded.
2. Set ``SEED_SCHEMA_REVISION`` to the new head (``seed.py --print-head``).
3. Regenerate any cached corpus / ``sample_output.json``.

Wired as a pre-commit hook on ``omnigent/db/db_models.py`` and
``omnigent/db/migrations/`` (mirrors the ``sync-version-py`` local hook).

Usage::

    python scripts/check_benchmark_seed_schema.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# scripts/ -> repo root is one level up; ensure the package imports resolve
# when pre-commit invokes this with an arbitrary CWD.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from dev.benchmarks.omnigent.seed import SEED_SCHEMA_REVISION  # noqa: E402
from omnigent.db.utils import _get_head_db_revision  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Compare the repo's Alembic head to the seed's pinned revision.

    :param argv: Argument list (defaults to ``sys.argv[1:]``). Any positional
        filenames pre-commit passes are accepted and ignored.
    :returns: ``0`` when in sync, ``1`` on drift (with a fix hint on stderr).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    # pre-commit passes the matched filenames; we check a fixed invariant, so
    # accept and ignore them.
    parser.add_argument("files", nargs="*", help=argparse.SUPPRESS)
    parser.parse_args(argv)

    # Head is read from the migration scripts on disk — no DB is contacted.
    head = _get_head_db_revision("sqlite:///:memory:")
    if head == SEED_SCHEMA_REVISION:
        return 0

    print(
        f"benchmark seed schema drift: SEED_SCHEMA_REVISION is "
        f"{SEED_SCHEMA_REVISION!r} but the DB head is {head!r}.\n"
        f"The DB schema changed without the benchmark seed being refreshed. "
        f"Update dev/benchmarks/omnigent/seed.py to seed any new schema, set "
        f"SEED_SCHEMA_REVISION = {head!r}, and regenerate the cached corpus / "
        f"sample_output.json.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
