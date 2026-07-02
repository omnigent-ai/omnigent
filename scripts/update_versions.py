"""
Bump the omnigent project version across all packages in lockstep.

The three distributions in this repo release together at a single
version:

- ``omnigent``         — root ``pyproject.toml``
- ``omnigent-client``  — ``sdks/python-client/pyproject.toml``
- ``omnigent-ui-sdk``  — ``sdks/ui/pyproject.toml``

Each declares its own ``[project].version`` and ``==``-pins its
siblings — the lockstep contract that
``.github/workflows/release-omnigent.yml`` verifies at tag time. This
script rewrites every one of those locations at once so they never
drift.

It edits ONLY the ``[project].version`` line and the sibling ``==``
pins, matched by package name — never a blind version-string replace —
so unrelated version literals (host/runner wire-protocol versions,
docstring examples, third-party dependency floors like
``databricks-mcp>=0.1.0``) are left untouched.

``web/package.json`` (a ``0.0.0`` sentinel for the private SPA) and
``web/electron/package.json`` (the desktop app's independent
version) are intentionally OUT of scope: neither is part of the
release-validated Python lockstep.

After editing the ``pyproject.toml`` files, regenerate the lockfile so
the embedded sibling specifiers track the new version::

    uv lock

Usage::

    # Stamp an exact version (cutting a release or release candidate):
    python scripts/update_versions.py pre-release --new-version 0.1.2
    python scripts/update_versions.py pre-release --new-version 0.1.2rc1

    # After releasing X, move main to the next dev version:
    python scripts/update_versions.py post-release --new-version 0.1.2
    #   -> stamps 0.1.3.dev0 everywhere

    # Verify every location agrees (prints the resolved version):
    python scripts/update_versions.py check
    python scripts/update_versions.py check --expect 0.1.2
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import tomllib
from packaging.version import InvalidVersion, Version

# scripts/update_versions.py -> repo root is one level up.
_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Package:
    """
    One lockstep-versioned distribution in the repo.

    :param name: Distribution name, e.g. ``"omnigent"``.
    :param pyproject: Path to the package's ``pyproject.toml``, e.g.
        ``Path("sdks/python-client/pyproject.toml")``.
    :param sibling_pins: Sibling distribution names this package
        ``==``-pins, e.g. ``("omnigent-client", "omnigent-ui-sdk")``.
        Empty for a package that pins no siblings.
    """

    name: str
    pyproject: Path
    sibling_pins: tuple[str, ...]


def packages(root: Path) -> list[Package]:
    """
    Return the lockstep packages with their paths rooted at *root*.

    :param root: Repo root, e.g. ``Path("/repo")``.
    :returns: The three :class:`Package` entries.
    """
    return [
        Package(
            "omnigent",
            root / "pyproject.toml",
            ("omnigent-client", "omnigent-ui-sdk"),
        ),
        Package(
            "omnigent-client",
            root / "sdks" / "python-client" / "pyproject.toml",
            ("omnigent",),
        ),
        Package(
            "omnigent-ui-sdk",
            root / "sdks" / "ui" / "pyproject.toml",
            ("omnigent-client",),
        ),
    ]


# ``version = "..."`` on its own line (the [project].version field).
_VERSION_LINE = re.compile(r'^version = "[^"]*"$', re.MULTILINE)

# ``VERSION = "..."`` on its own line — the runtime constant in
# ``omnigent/version.py`` that mirrors the canonical [project].version.
_VERSION_CONSTANT = re.compile(r'^VERSION = "[^"]*"$', re.MULTILINE)


def _version_py(root: Path) -> Path:
    """Return the path to the runtime version constant module."""
    return root / "omnigent" / "version.py"


def _pin_pattern(name: str) -> re.Pattern[str]:
    """
    Build the regex matching a quoted ``"<name>==<ver>",`` dependency.

    Anchored on the exact distribution *name* so a blind version
    literal is never matched, and capturing the leading indent so it
    is preserved on rewrite.

    :param name: Distribution name to match, e.g. ``"omnigent-client"``.
    :returns: A compiled multiline pattern.
    """
    return re.compile(rf'^(?P<indent>\s*)"{re.escape(name)}==[^"]*",$', re.MULTILINE)


def _sub_exactly_once(pattern: re.Pattern[str], repl: str, text: str, where: str) -> str:
    """
    Substitute *pattern* with *repl* in *text*, requiring one match.

    Failing loud on zero or multiple matches turns a drifted file
    format (renamed field, duplicated pin) into an immediate error
    rather than a silent partial edit.

    :param pattern: Compiled pattern to replace.
    :param repl: Replacement string (may reference groups).
    :param text: Source text.
    :param where: Human description for the error, e.g.
        ``"[project].version in pyproject.toml"``.
    :returns: The edited text.
    :raises ValueError: If the match count is not exactly one.
    """
    new_text, count = pattern.subn(repl, text)
    if count != 1:
        raise ValueError(f"expected exactly 1 match for {where}, found {count}")
    return new_text


def read_version(root: Path) -> str:
    """
    Read the canonical version from the root ``pyproject.toml``.

    :param root: Repo root.
    :returns: The version string, e.g. ``"0.1.2.dev0"``.
    """
    data = tomllib.loads((root / "pyproject.toml").read_text())
    return data["project"]["version"]


def set_version(root: Path, new_version: str) -> list[Path]:
    """
    Rewrite every package's version + sibling pins to *new_version*.

    Also rewrites the runtime ``VERSION`` constant in ``omnigent/version.py``
    so the value the runtime imports stays equal to ``[project].version`` —
    the automated bump path must keep both in lockstep (the ``sync-version-py``
    pre-commit fixer only fires in the local dev flow).

    :param root: Repo root.
    :param new_version: PEP 440 version to stamp, e.g. ``"0.1.2"``.
    :returns: The list of files changed (in edit order).
    :raises ValueError: If any expected line is missing or duplicated.
    """
    changed: list[Path] = []
    for pkg in packages(root):
        text = pkg.pyproject.read_text()
        text = _sub_exactly_once(
            _VERSION_LINE,
            f'version = "{new_version}"',
            text,
            f"[project].version in {pkg.pyproject}",
        )
        for sibling in pkg.sibling_pins:
            text = _sub_exactly_once(
                _pin_pattern(sibling),
                rf'\g<indent>"{sibling}=={new_version}",',
                text,
                f"{sibling}== pin in {pkg.pyproject}",
            )
        pkg.pyproject.write_text(text)
        changed.append(pkg.pyproject)

    version_py = _version_py(root)
    version_text = _sub_exactly_once(
        _VERSION_CONSTANT,
        f'VERSION = "{new_version}"',
        version_py.read_text(),
        f"VERSION constant in {version_py}",
    )
    version_py.write_text(version_text)
    changed.append(version_py)

    return changed


def next_dev_version(released: str) -> str:
    """
    Compute the next development version after releasing *released*.

    Mirrors MLflow's post-release convention: bump the patch component
    and append ``.dev0`` (e.g. ``0.1.2`` -> ``0.1.3.dev0``).

    :param released: The just-released version, e.g. ``"0.1.2"``.
    :returns: The next dev version, e.g. ``"0.1.3.dev0"``.
    """
    v = Version(released)
    return f"{v.major}.{v.minor}.{v.micro + 1}.dev0"


def _read_version_constant(root: Path) -> str:
    """
    Return the ``VERSION`` literal from ``omnigent/version.py``.

    :param root: Repo root.
    :returns: The quoted value of the ``VERSION`` assignment.
    :raises ValueError: If the assignment is missing or not unique.
    """
    version_py = _version_py(root)
    matches = _VERSION_CONSTANT.findall(version_py.read_text())
    if len(matches) != 1:
        raise ValueError(
            f'expected exactly one `VERSION = "..."` line in {version_py}, found {len(matches)}'
        )
    return matches[0].split('"')[1]


def check(root: Path, expect: str | None = None) -> str:
    """
    Verify every package agrees on the version and pins its siblings.

    Also checks the runtime ``VERSION`` constant in ``omnigent/version.py``
    against the resolved version, so a bump that forgets it fails here rather
    than in the ``test_version_matches_pyproject`` backstop on the bot PR.

    :param root: Repo root.
    :param expect: If given, additionally assert the resolved version
        equals this (compared as PEP 440), e.g. ``"0.1.2"``.
    :returns: The single resolved version string.
    :raises ValueError: If versions disagree, a sibling pin is missing
        or not pinned to the package's own version, the runtime ``VERSION``
        constant differs, or the resolved version differs from *expect*.
    """
    versions: dict[str, str] = {}
    for pkg in packages(root):
        project = tomllib.loads(pkg.pyproject.read_text())["project"]
        versions[pkg.name] = project["version"]
        deps = project.get("dependencies", [])
        for sibling in pkg.sibling_pins:
            pin = f"{sibling}=={project['version']}"
            if pin not in deps:
                raise ValueError(f"{pkg.pyproject}: missing exact pin {pin!r}")
    unique = set(versions.values())
    if len(unique) != 1:
        raise ValueError(f"package versions disagree: {versions}")
    resolved = unique.pop()
    constant = _read_version_constant(root)
    if Version(constant) != Version(resolved):
        raise ValueError(
            f"omnigent/version.py VERSION {constant!r} != [project].version {resolved!r}"
        )
    if expect is not None and Version(resolved) != Version(expect):
        raise ValueError(f"resolved version {resolved} != expected {expect}")
    return resolved


def _validate_pep440(value: str) -> str:
    """
    Validate *value* is a PEP 440 version, exiting loudly otherwise.

    :param value: Candidate version string, e.g. ``"0.1.2rc1"``.
    :returns: The same value.
    """
    try:
        Version(value)
    except InvalidVersion as exc:
        raise SystemExit(f"invalid version {value!r}: {exc}") from exc
    return value


def _cmd_pre_release(root: Path, new_version: str) -> None:
    """Stamp *new_version* exactly across all packages."""
    _validate_pep440(new_version)
    changed = set_version(root, new_version)
    check(root, expect=new_version)
    print(f"Set version to {new_version} in:", file=sys.stderr)
    for path in changed:
        print(f"  {path.relative_to(root)}", file=sys.stderr)
    print("Now run `uv lock` to update the lockfile.", file=sys.stderr)


def _cmd_post_release(root: Path, released: str) -> None:
    """Stamp the next ``.dev0`` after releasing *released*."""
    _validate_pep440(released)
    current = Version(read_version(root))
    if not current.is_devrelease:
        raise SystemExit(
            f"current version {current} is not a dev release; post-release must run "
            "on main (which carries a .devN version), not a release branch"
        )
    new_version = next_dev_version(released)
    set_version(root, new_version)
    check(root, expect=new_version)
    print(f"Bumped main to {new_version} (after release {released}).", file=sys.stderr)
    print("Now run `uv lock` to update the lockfile.", file=sys.stderr)


def _cmd_check(root: Path, expect: str | None) -> None:
    """Verify consistency and print the resolved version to stdout."""
    print(check(root, expect=expect))


def main(argv: list[str] | None = None) -> None:
    """
    Parse args and dispatch to the requested subcommand.

    :param argv: Argument list (defaults to ``sys.argv[1:]``).
    """
    parser = argparse.ArgumentParser(description="Bump omnigent package versions in lockstep")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("pre-release", help="Stamp an exact version across all packages")
    pre.add_argument("--new-version", required=True, help="Version to stamp, e.g. 0.1.2")

    post = sub.add_parser("post-release", help="Stamp the next .dev0 after a release")
    post.add_argument("--new-version", required=True, help="The just-released version, e.g. 0.1.2")

    chk = sub.add_parser("check", help="Verify all packages agree (prints the version)")
    chk.add_argument("--expect", default=None, help="Assert the resolved version equals this")

    args = parser.parse_args(argv)
    if args.command == "pre-release":
        _cmd_pre_release(_REPO_ROOT, args.new_version)
    elif args.command == "post-release":
        _cmd_post_release(_REPO_ROOT, args.new_version)
    elif args.command == "check":
        _cmd_check(_REPO_ROOT, args.expect)


if __name__ == "__main__":
    main()
