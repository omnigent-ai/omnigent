"""Keep ``uv.lock`` in the lock format the pinned uv writes.

uv bumps the lockfile ``revision`` now and then, and a newer revision can also
partition ``resolution-markers`` differently. ``uv lock --check`` still passes
on an older revision, so nothing notices until Dependabot, which runs a current
uv, rewrites the header in every Python bump and the auto-merge policy refuses
each one as a resolution-settings change.

``check`` compares the committed revision with the one this uv writes for an
empty project. ``migrate`` rewrites the lock in the current format with every
pin unchanged: a refresh moves the revision, and one same-version relock lands
on the marker ordering uv keeps stable across later bumps (a bare refresh does
not, so the next Dependabot bump would reorder it again).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

PROBE_PYPROJECT = (
    '[project]\nname = "uv-lock-format-probe"\nversion = "0"\nrequires-python = ">=3.12"\n'
)


def lock_revision(text: str) -> int:
    """Return the ``revision`` a lockfile declares; the first format had none."""
    return int(tomllib.loads(text).get("revision", 1))


def pins(text: str) -> dict[tuple[str, str], tuple[str | None, tuple[str, ...]]]:
    """Map every locked package to its artifacts, so a migration can prove it moved nothing."""
    result = {}
    for package in tomllib.loads(text).get("package", []):
        source = package.get("source", {})
        key = (package["name"], source.get("registry") or source.get("editable") or "")
        result[(key, package["version"])] = (
            package.get("sdist", {}).get("url"),
            tuple(wheel["url"] for wheel in package.get("wheels", [])),
        )
    return result


def run_uv(uv: str, *args: str, cwd: Path) -> None:
    subprocess.run([uv, *args], cwd=cwd, check=True, capture_output=True, text=True)


def probe_revision(uv: str) -> int:
    """The revision this uv writes, read off a throwaway empty project."""
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp)
        (probe / "pyproject.toml").write_text(PROBE_PYPROJECT)
        run_uv(uv, "lock", "--offline", cwd=probe)
        return lock_revision((probe / "uv.lock").read_text())


def anchor_package(text: str) -> str:
    """A ``name==version`` locked exactly once, for the same-version relock."""
    versions: dict[str, set[str]] = {}
    for package in tomllib.loads(text)["package"]:
        if "registry" in package.get("source", {}):
            versions.setdefault(package["name"], set()).add(package["version"])
    for name in sorted(versions):
        if len(versions[name]) == 1:
            return f"{name}=={next(iter(versions[name]))}"
    raise SystemExit("uv.lock has no registry package locked at a single version")


def migrate(uv: str, lock_path: Path) -> tuple[int, int]:
    """Rewrite the lock in this uv's format; fail and restore it if any pin moved."""
    before = lock_path.read_text()
    project = lock_path.parent
    run_uv(uv, "lock", "--refresh", cwd=project)
    run_uv(uv, "lock", "--upgrade-package", anchor_package(before), cwd=project)
    after = lock_path.read_text()
    if pins(after) != pins(before):
        lock_path.write_text(before)
        raise SystemExit("migration changed a pin; uv.lock restored, inspect by hand")
    return lock_revision(before), lock_revision(after)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["check", "migrate"])
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--uv", default=os.environ.get("UV", "uv"))
    args = parser.parse_args(argv)

    committed = lock_revision(args.lock.read_text())
    current = probe_revision(args.uv)
    print(f"uv.lock revision={committed}; uv writes revision={current}")
    if args.command == "check":
        return 0 if committed == current else 1
    if committed == current:
        print("uv.lock is already in the current format")
        return 0
    old, new = migrate(args.uv, args.lock)
    print(f"migrated uv.lock from revision {old} to {new} with every pin unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
