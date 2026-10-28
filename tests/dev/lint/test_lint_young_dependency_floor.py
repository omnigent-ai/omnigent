"""Tests for ``dev/lint/lint_young_dependency_floor.py``.

Mirrors that embargo young releases resolve omnigent's declared requirements,
not uv.lock, so a published floor that only a release inside the uv cooldown
satisfies makes omnigent uninstallable from them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

import dev.lint.lint_young_dependency_floor as lint

NOW = datetime(2026, 10, 22, 15, 12, tzinfo=UTC)
YOUNG_UPLOAD = "2026-10-16T22:15:24Z"
SUPPRESSION = "# custom-lint: disable-next=young-dependency-floor -- security fix, mirror checked"


def _lock(upload_time: str = YOUNG_UPLOAD, span: str | None = "P7D") -> str:
    options = "" if span is None else f'exclude-newer-span = "{span}"\n'
    return (
        f"version = 1\n\n[options]\n{options}\n"
        '[[package]]\nname = "pillow"\nversion = "13.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://example.invalid/pillow.whl", '
        f'upload-time = "{upload_time}" }}]\n'
    )


def _pyproject(dependencies: Sequence[str], extras: Sequence[str] = ()) -> str:
    """One requirement per line; the dependency group always pins the young release."""
    lines = ["[project]", 'name = "omnigent"', "dependencies = [", *dependencies, "]"]
    lines += ["", "[project.optional-dependencies]", *extras]
    lines += ["", "[dependency-groups]", 'test = ["pillow>=13"]', ""]
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("pyproject", "location", "raw"),
    [
        (_pyproject(['    "pillow>=13,<14",']), "pyproject.toml:4", "pillow>=13,<14"),
        (_pyproject([], ['images = ["pillow>=13,<14"]']), "pyproject.toml:7", "pillow>=13,<14"),
        (_pyproject(['    "pillow~=13.0",']), "pyproject.toml:4", "pillow~=13.0"),
        (_pyproject(['    "pillow==13.*",']), "pyproject.toml:4", "pillow==13.*"),
        (_pyproject(['    "pillow===13.0.0",']), "pyproject.toml:4", "pillow===13.0.0"),
        (
            _pyproject(['    "pillow>=13; python_version >= \\"3.12\\"",']),
            "pyproject.toml:4",
            'pillow>=13; python_version >= "3.12"',
        ),
        (_pyproject(['    """pillow>=13,\\', '    <14""",']), "pyproject.toml", "pillow>=13,<14"),
    ],
    ids=[
        "dependency",
        "extra",
        "compatible-release",
        "wildcard-equality",
        "arbitrary-equality",
        "escaped-quotes",
        "line-not-found",
    ],
)
def test_flags_floor_only_a_cooldown_release_satisfies(
    pyproject: str, location: str, raw: str
) -> None:
    assert lint.find_violations(pyproject, _lock(), NOW) == [
        f"{location}: `{raw}` needs pillow 13.0.0+, uploaded 2026-10-16 22:15 UTC "
        "and inside the 7-day cooldown until 2026-10-23 22:15 UTC"
    ]


def test_matches_locked_package_by_normalized_name_with_extras() -> None:
    (violation,) = lint.find_violations(_pyproject(['    "Pillow[avif]>=13",']), _lock(), NOW)
    assert violation.startswith("pyproject.toml:4: `Pillow[avif]>=13` needs Pillow 13.0.0+")


@pytest.mark.parametrize(
    ("pyproject", "lock"),
    [
        (_pyproject(['    "pillow>=12.2,<14",']), _lock()),
        (_pyproject(['    "pillow>=13,<14",']), _lock(upload_time="2026-10-10T00:00:00Z")),
        (_pyproject([f"    {SUPPRESSION}", '    "pillow>=13,<14",']), _lock()),
        (_pyproject([f'    "pillow>=13,<14",  {SUPPRESSION.replace("-next", "")}']), _lock()),
        (_pyproject(['    "pillow<14",', '    "pillow!=12.0",']), _lock()),
        (_pyproject(['    "pillow===local-build",']), _lock()),
        (_pyproject([]), _lock()),
    ],
    ids=[
        "floor-below-young-release",
        "release-aged-out",
        "suppressed",
        "suppressed-same-line",
        "no-floor",
        "unparseable-arbitrary-equality",
        "dependency-group-only",
    ],
)
def test_passes_floors_mirrors_can_install(pyproject: str, lock: str) -> None:
    assert lint.find_violations(pyproject, lock, NOW) == []


@pytest.mark.parametrize(
    ("span", "message"),
    [
        ("P1W", "uv.lock: exclude-newer-span 'P1W' is not a P<days>D duration"),
        (None, "uv.lock: no exclude-newer-span; this lint needs the uv cooldown in uv.toml"),
    ],
    ids=["unparseable", "missing"],
)
def test_reports_cooldown_span_it_cannot_read(span: str | None, message: str) -> None:
    """A missing or unparseable span must not silently disable the rule."""
    assert lint.find_violations(_pyproject([]), _lock(span=span), NOW) == [message]
