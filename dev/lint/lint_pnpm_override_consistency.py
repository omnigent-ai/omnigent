#!/usr/bin/env python3
"""Fail when a pnpm `overrides:` pin can't satisfy a package's declared range.

The scenario: a dep is pinned in `pnpm-workspace.yaml` `overrides:` to a version
that does NOT satisfy the range the same package declares in a workspace
`package.json`. Because overrides outrank `package.json`, the declared bump is
silently inert — a `package.json` edit (e.g. a security bump) resolves to the
override's version instead, and neither `--frozen-lockfile` nor a lockfile-regen
check notices (the lock is internally consistent for the given overrides).

This is the trap that let a postcss security bump land without taking effect:
`package.json` said `^8.5.18` while `overrides:` still pinned `8.5.15`.

Scope: compares each plain-version override against every workspace
`package.json` range for the same package. Skips `catalog:` refs and
`parent>child` scoped keys (different semantics). Uses a small npm-flavored
semver check covering the operators this repo actually uses (`^`, `~`, exact,
and simple comparators); unrecognized ranges are reported so they can't pass
silently.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = REPO_ROOT / "pnpm-workspace.yaml"

_DEP_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def _parse_version(text: str) -> tuple[int, int, int] | None:
    """Parse a bare `x.y.z` core version, ignoring any prerelease/build suffix."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def satisfies(version: str, range_spec: str) -> bool | None:
    """Does `version` satisfy the npm `range_spec`?

    Returns True/False for handled ranges, or None when the range uses syntax
    this checker doesn't model (caller treats None as "can't verify" → report).
    """
    ver = _parse_version(version)
    if ver is None:
        return None

    spec = range_spec.strip()
    # Wildcards / "any" match everything.
    if spec in ("", "*", "x", "latest"):
        return True

    lead = spec[0]

    # Exact pin (no operator).
    if lead.isdigit() or lead == "v":
        pinned = _parse_version(spec)
        return None if pinned is None else ver == pinned

    if lead == "^":
        base = _parse_version(spec[1:])
        if base is None:
            return None
        if ver < base:
            return False
        # ^ allows changes that don't modify the left-most non-zero element.
        if base[0] > 0:
            return ver[0] == base[0]
        if base[1] > 0:
            return ver[0] == 0 and ver[1] == base[1]
        return ver[0] == 0 and ver[1] == 0 and ver[2] == base[2]

    if lead == "~":
        base = _parse_version(spec[1:])
        if base is None:
            return None
        # ~ allows patch-level changes (or minor when only major.minor given,
        # which we can't distinguish post-parse; treat as minor-locked).
        return ver >= base and ver[0] == base[0] and ver[1] == base[1]

    m = re.match(r"^(>=|<=|>|<|=)\s*(.+)$", spec)
    if m:
        op, rest = m.group(1), m.group(2)
        base = _parse_version(rest)
        if base is None:
            return None
        return {
            ">=": ver >= base,
            "<=": ver <= base,
            ">": ver > base,
            "<": ver < base,
            "=": ver == base,
        }[op]

    # Compound ranges (" || ", " - ", spaces) aren't used for overridden deps
    # here; report rather than guess.
    return None


def _load_declared_ranges(packages: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Map dep name -> list of (package_path, range) across workspace manifests."""
    declared: dict[str, list[tuple[str, str]]] = {}
    for rel in packages:
        manifest = REPO_ROOT / rel / "package.json"
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text())
        for section in _DEP_SECTIONS:
            for name, rng in (data.get(section) or {}).items():
                declared.setdefault(name, []).append((rel, rng))
    return declared


def main() -> int:
    workspace = yaml.safe_load(WORKSPACE.read_text())
    overrides = workspace.get("overrides") or {}
    packages = workspace.get("packages") or []
    declared = _load_declared_ranges(packages)

    violations: list[str] = []
    unverified: list[str] = []

    for name, pin in overrides.items():
        # Skip parent>child scoped overrides and catalog references.
        if ">" in name:
            continue
        if isinstance(pin, str) and pin.startswith("catalog"):
            continue
        # Only plain-version pins are comparable; a ranged override value can't
        # be checked against a range the same way.
        if _parse_version(str(pin)) is None:
            continue
        if name not in declared:
            continue  # override targets a purely-transitive dep; no range to compare

        for pkg_path, rng in declared[name]:
            result = satisfies(str(pin), rng)
            if result is False:
                violations.append(
                    f"  {name}: overrides pins {pin}, but {pkg_path}/package.json "
                    f"declares {rng} (pin does not satisfy the range)"
                )
            elif result is None:
                unverified.append(
                    f"  {name}: unrecognized range {rng!r} in {pkg_path}/package.json "
                    f"(override pins {pin}) — cannot verify"
                )

    if violations:
        print(
            "pnpm-workspace.yaml overrides conflict with a declared package.json range.\n"
            "An override pins a version outside the range the package requests, so the\n"
            "declared version is silently ignored. Update the override to a version that\n"
            "satisfies the range (or align the range), then regenerate pnpm-lock.yaml:\n"
        )
        print("\n".join(violations))
        if unverified:
            print("\nAlso could not verify (report only):")
            print("\n".join(unverified))
        return 1

    if unverified:
        # Unverifiable ranges are informational, not a failure.
        print("pnpm override consistency: could not verify some ranges:")
        print("\n".join(unverified))

    return 0


if __name__ == "__main__":
    sys.exit(main())
