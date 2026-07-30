"""Sync ``omnigent/version.py``'s ``VERSION`` to the canonical pyproject version.

The root ``pyproject.toml``'s ``[project].version`` is the single source of
truth for the release version (stamped in lockstep with the SDK packages by
``scripts/update_versions.py``). The runtime, however, reads
``omnigent.version.VERSION`` — a plain constant it can import without touching
package metadata. This script keeps that constant equal to the canonical
pyproject version so the two never drift.

It is a pre-commit *fixer*: it rewrites the ``VERSION`` literal in place and
exits non-zero when it changed anything, so the commit aborts and the developer
re-stages the synced file (mirroring ``end-of-file-fixer`` and
``normalize_uv_lock_registry``).

Pass ``--check`` to validate without writing: it exits non-zero (and prints the
mismatch) when the constant is stale, but leaves the file untouched. (CI-side
drift is caught by ``scripts/update_versions.py check`` and the
``test_version_matches_pyproject`` test; this flag is for ad-hoc local use.)

Usage::

    python scripts/sync_version_py.py            # fix
    python scripts/sync_version_py.py --check     # verify
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import tomllib

# scripts/sync_version_py.py -> repo root is one level up.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_VERSION_PY = _REPO_ROOT / "omnigent" / "version.py"

# The ``VERSION = "..."`` assignment (its own line) in omnigent/version.py.
_VERSION_ASSIGN = re.compile(r'^VERSION = "[^"]*"$', re.MULTILINE)


def _canonical_version() -> str:
    """Return ``[project].version`` from the root ``pyproject.toml``."""
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def _current_constant(text: str) -> str:
    """Return the ``VERSION`` literal currently in *text*.

    :param text: Contents of ``omnigent/version.py``.
    :returns: The quoted value of the ``VERSION`` assignment.
    :raises ValueError: If the assignment is missing or not unique.
    """
    matches = _VERSION_ASSIGN.findall(text)
    if len(matches) != 1:
        raise ValueError(
            f'expected exactly one `VERSION = "..."` line in {_VERSION_PY}, found {len(matches)}'
        )
    return matches[0].split('"')[1]


def main(argv: list[str] | None = None) -> int:
    """Sync (or, with ``--check``, verify) the ``VERSION`` constant.

    :param argv: Argument list (defaults to ``sys.argv[1:]``).
    :returns: Process exit code — ``0`` when already in sync, ``1`` when a
        rewrite was needed (fix mode) or a drift was found (check mode).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify without writing; exit non-zero on drift",
    )
    # pre-commit passes the matched filenames; we operate on fixed paths, so
    # accept and ignore them.
    parser.add_argument("files", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    canonical = _canonical_version()
    text = _VERSION_PY.read_text(encoding="utf-8")
    current = _current_constant(text)

    if current == canonical:
        return 0

    if args.check:
        print(
            f"{_VERSION_PY.name}: VERSION is {current!r} but pyproject.toml is "
            f"{canonical!r}; run `python scripts/sync_version_py.py` to fix",
            file=sys.stderr,
        )
        return 1

    new_text = _VERSION_ASSIGN.sub(f'VERSION = "{canonical}"', text)
    _VERSION_PY.write_text(new_text, encoding="utf-8")
    print(
        f"{_VERSION_PY.name}: synced VERSION {current!r} -> {canonical!r} (re-stage the file)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
