"""Flag dependency floors that only a release inside the uv cooldown satisfies.

uv.toml's cooldown plus a per-package ``exclude-newer-package`` exemption can
lock a release younger than the cooldown. That is fine for uv.lock, but package
mirrors that embargo young releases (such as the Databricks PyPI proxy) resolve
the requirements declared in pyproject.toml, not the lock. A floor that forces
the young release (``pillow>=13`` while 13.0.0 is days old) makes omnigent
uninstallable from those mirrors until the release ages in.

This rule fails when a requirement in ``[project].dependencies`` or
``[project.optional-dependencies]`` has a lower bound at or above a locked
version uploaded inside uv.lock's ``exclude-newer-span``. Dependency groups are
repo-only and never reach published metadata, so they are not checked. An
urgent bump the mirror already serves is suppressed on the requirement line
with a reason; see :mod:`dev.lint._framework`.

Release age only approximates mirror availability: a mirror can also withhold
an older release, which only ``just check-internal-mirror`` detects.
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from dev.lint._framework import disabled_rules_by_line

REPO_ROOT = Path(__file__).resolve().parents[2]

RULE_NAME = "young-dependency-floor"
HINT = (
    "A declared floor needs a release younger than the uv cooldown, so mirrors that\n"
    "embargo young releases (e.g. the Databricks PyPI proxy) cannot install omnigent.\n"
    "Keep the floor below that release and let uv.lock pin it. For an urgent bump\n"
    "(e.g. a security fix), confirm the mirror serves it with\n"
    "`just check-internal-mirror`, then add above the requirement:\n"
    "  # custom-lint: disable-next=young-dependency-floor -- <why urgent; mirror checked>\n"
    "A maintainer other than the PR author must then add the `dependency-floor-approved`\n"
    "label, or the required Maintainer Approval check stays red."
)

# Specifier operators whose version bounds the admitted releases from below.
_LOWER_BOUND_OPERATORS = frozenset({">=", ">", "==", "~="})
_SPAN_RE = re.compile(r"P(\d+)D")
_TABLE_RE = re.compile(r"^\[+([^\]]+)\]+")
_QUOTED_RE = re.compile(r"\"([^\"]+)\"|'([^']+)'")
_REQUIREMENT_TABLES = frozenset({"project", "project.optional-dependencies"})


def _published_requirements(pyproject: dict) -> set[str]:
    """Requirement strings that end up in the published wheel metadata."""
    project = pyproject.get("project", {})
    requirements = set(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.update(extra)
    return requirements


def _requirement_lines(pyproject_text: str, published: set[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(line, requirement)`` where a published requirement is written."""
    table = ""
    for line_number, line in enumerate(pyproject_text.splitlines(), start=1):
        stripped = line.strip()
        if header := _TABLE_RE.match(stripped):
            table = header.group(1).strip()
            continue
        if table not in _REQUIREMENT_TABLES or stripped.startswith("#"):
            continue
        for match in _QUOTED_RE.finditer(line):
            text = match.group(1) or match.group(2)
            if text in published:
                yield line_number, text


def _floor(requirement: Requirement) -> Version | None:
    """Highest lower bound in the requirement's specifier, or None if unbounded."""
    bounds = [
        Version(spec.version.removesuffix(".*"))
        for spec in requirement.specifier
        if spec.operator in _LOWER_BOUND_OPERATORS
    ]
    return max(bounds, default=None)


def _locked_uploads(lock: dict) -> dict[str, list[tuple[Version, datetime]]]:
    """Map each locked registry package to its versions and first upload time."""
    uploads: dict[str, list[tuple[Version, datetime]]] = {}
    for package in lock.get("package", []):
        artifacts = [package.get("sdist") or {}, *package.get("wheels", [])]
        times = [datetime.fromisoformat(a["upload-time"]) for a in artifacts if "upload-time" in a]
        if times:
            name = canonicalize_name(package["name"])
            uploads.setdefault(name, []).append((Version(package["version"]), min(times)))
    return uploads


def find_violations(pyproject_text: str, lock_text: str, now: datetime) -> list[str]:
    """Return ``path:line: message`` for each floor forcing a release inside the cooldown."""
    lock = tomllib.loads(lock_text)
    span = lock.get("options", {}).get("exclude-newer-span", "")
    span_match = _SPAN_RE.fullmatch(span)
    if span_match is None:
        return [f"uv.lock: exclude-newer-span {span!r} is not a P<days>D duration"]
    cooldown = timedelta(days=int(span_match.group(1)))

    uploads = _locked_uploads(lock)
    published = _published_requirements(tomllib.loads(pyproject_text))
    suppressed = disabled_rules_by_line(pyproject_text)
    violations = []
    for line_number, raw in _requirement_lines(pyproject_text, published):
        if RULE_NAME in suppressed.get(line_number, set()):
            continue
        requirement = Requirement(raw)
        floor = _floor(requirement)
        if floor is None:
            continue
        for version, uploaded in uploads.get(canonicalize_name(requirement.name), []):
            if uploaded + cooldown > now and floor >= version:
                violations.append(
                    f"pyproject.toml:{line_number}: `{raw}` needs {requirement.name} "
                    f"{version}+, uploaded {uploaded:%Y-%m-%d} and inside the "
                    f"{cooldown.days}-day cooldown until {uploaded + cooldown:%Y-%m-%d}"
                )
    return violations


def check() -> list[str]:
    """custom_lint entry point: check the repository's pyproject.toml and uv.lock."""
    return find_violations(
        (REPO_ROOT / "pyproject.toml").read_text(),
        (REPO_ROOT / "uv.lock").read_text(),
        datetime.now(UTC),
    )


def main() -> int:
    """Standalone entry point; custom_lint calls :func:`check` directly."""
    messages = check()
    for message in messages:
        sys.stdout.write(f"{message}\n")
    if messages:
        sys.stdout.write(f"\n{HINT}\n")
    return 1 if messages else 0


if __name__ == "__main__":
    sys.exit(main())
