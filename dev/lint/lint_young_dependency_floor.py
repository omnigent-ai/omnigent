"""Flag published floors that only a release inside uv.lock's cooldown satisfies.

Mirrors that embargo young releases install from these floors, not from uv.lock.
Only locked versions are known, so ``just check-internal-mirror`` stays authoritative.
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
from packaging.version import InvalidVersion, Version

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
    "A maintainer, the author included, must then add the `dependency-floor-approved`\n"
    "label, or the required Maintainer Approval check stays red."
)

# Specifier operators whose version bounds the admitted releases from below.
_LOWER_BOUND_OPERATORS = frozenset({">=", ">", "==", "~=", "==="})
_SPAN_RE = re.compile(r"P(\d+)D")
_TABLE_RE = re.compile(r"^\[+([^\]]+)\]+")
_REQUIREMENT_TABLES = frozenset({"project", "project.optional-dependencies"})


def _published_requirements(pyproject: dict) -> set[str]:
    """Requirement strings that end up in the published wheel metadata."""
    project = pyproject.get("project", {})
    requirements = set(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.update(extra)
    return requirements


def _line_strings(line: str) -> list[str]:
    """Strings in one line of a TOML array: a ``key = [...]`` line or an element line."""
    for document in (line, f"value = [{line}\n]"):
        try:
            parsed = tomllib.loads(document)
        except tomllib.TOMLDecodeError:
            continue
        return [
            item
            for value in parsed.values()
            if isinstance(value, list)
            for item in value
            if isinstance(item, str)
        ]
    return []


def _requirement_lines(
    pyproject_text: str, published: set[str]
) -> Iterator[tuple[int | None, str]]:
    """Yield ``(line, requirement)`` for each published requirement.

    A requirement whose line cannot be found is yielded with ``None`` so it is
    still checked; it just cannot be waived.
    """
    table = ""
    located: set[str] = set()
    for line_number, line in enumerate(pyproject_text.splitlines(), start=1):
        stripped = line.strip()
        if header := _TABLE_RE.match(stripped):
            table = header.group(1).strip()
            continue
        if table not in _REQUIREMENT_TABLES or stripped.startswith("#"):
            continue
        for text in _line_strings(line):
            if text in published:
                located.add(text)
                yield line_number, text
    for text in sorted(published - located):
        yield None, text


def _floor(requirement: Requirement) -> Version | None:
    """Highest lower bound in the requirement's specifier, or None if unbounded."""
    bounds = []
    for spec in requirement.specifier:
        if spec.operator not in _LOWER_BOUND_OPERATORS:
            continue
        try:
            bounds.append(Version(spec.version.removesuffix(".*")))
        except InvalidVersion:  # `===` accepts arbitrary strings
            continue
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
    span = lock.get("options", {}).get("exclude-newer-span")
    if span is None:
        return ["uv.lock: no exclude-newer-span; this lint needs the uv cooldown in uv.toml"]
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
                location = (
                    "pyproject.toml" if line_number is None else f"pyproject.toml:{line_number}"
                )
                violations.append(
                    f"{location}: `{raw}` needs {requirement.name} {version}+, uploaded "
                    f"{uploaded:%Y-%m-%d %H:%M} UTC and inside the {cooldown.days}-day "
                    f"cooldown until {uploaded + cooldown:%Y-%m-%d %H:%M} UTC"
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
