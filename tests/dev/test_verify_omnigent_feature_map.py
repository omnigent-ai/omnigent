"""The verify-omnigent feature map stays consistent with the repository.

The map (``.claude/skills/verify-omnigent/features/``) points at tests instead of
copying selectors, so these checks turn its most common drift into a failure: a
renamed or deleted test, a feature file missing from the index, a file that
breaks the entry contract, or a native harness added without a matrix row.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL = _REPO_ROOT / ".claude" / "skills" / "verify-omnigent"
_FEATURES = _SKILL / "features"
_SECTIONS = [
    "Sub-features",
    "How to get to it (user POV)",
    "Driving it with the repro environment",
    "Gotchas",
]
_TEST_REF = re.compile(r"\b(tests/[\w/.-]+\.py)(?:::(\w+))?")


def _feature_files() -> list[Path]:
    return sorted(p for p in _FEATURES.glob("*.md") if p.name != "README.md")


def test_index_links_every_feature_file() -> None:
    index = (_FEATURES / "README.md").read_text()
    features = index.split("\n## Features\n", 1)[1]
    linked = set(re.findall(r"\]\(\./([\w-]+\.md)\)", features))
    present = {p.name for p in _feature_files()}
    assert linked == present, (
        f"features/README.md must link exactly the feature files; "
        f"unlinked: {sorted(present - linked)}, missing: {sorted(linked - present)}"
    )


@pytest.mark.parametrize("path", _feature_files(), ids=lambda p: p.name)
def test_feature_file_follows_entry_contract(path: Path) -> None:
    text = path.read_text()
    lines = text.splitlines()
    assert lines[0].startswith("# "), f"{path.name} must start with an H1 title"
    intro = text.split("\n## ", 1)[0].split("\n", 1)[1].strip()
    assert intro, f"{path.name} needs a paragraph describing the feature before its sections"
    sections = [line[3:] for line in lines if line.startswith("## ")]
    assert sections == _SECTIONS, f"{path.name} sections must be {_SECTIONS}, got {sections}"
    driving = text.split("\n## Driving it with the repro environment\n", 1)[1]
    assert driving.lstrip().startswith("Preconditions:"), (
        f"{path.name}: the driving section must start with 'Preconditions:'"
    )


def _test_references() -> list[tuple[str, str, str | None]]:
    refs = []
    for path in _feature_files():
        for match in _TEST_REF.finditer(path.read_text()):
            refs.append((path.name, match.group(1), match.group(2)))
    return sorted(set(refs), key=lambda ref: (ref[0], ref[1], ref[2] or ""))


@pytest.mark.parametrize(
    ("feature", "test_path", "test_name"),
    _test_references(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_referenced_test_exists(feature: str, test_path: str, test_name: str | None) -> None:
    source = _REPO_ROOT / test_path
    assert source.is_file(), f"{feature} references {test_path}, which does not exist"
    if test_name:
        pattern = rf"^\s*(?:async\s+)?def {re.escape(test_name)}\b"
        assert re.search(pattern, source.read_text(), re.M), (
            f"{feature} references {test_path}::{test_name}, which is not defined there"
        )


def test_native_harness_matrix_lists_every_harness() -> None:
    harnesses = sorted(
        p.name.removesuffix("_native")
        for p in (_REPO_ROOT / "omnigent" / "harnesses").glob("*_native")
        if p.is_dir()
    )
    assert harnesses, "expected native harness packages under omnigent/harnesses"
    matrix = (_FEATURES / "native-harnesses.md").read_text()
    rows = set(re.findall(r"^\|\s*`([\w-]+)-native`\s*\|", matrix, re.M))
    missing = [name for name in harnesses if name not in rows]
    assert not missing, f"native-harnesses.md matrix has no row for: {missing}"


def test_verify_env_helper_is_executable() -> None:
    helper = _SKILL / "scripts" / "verify-env"
    assert os.access(helper, os.X_OK), f"{helper} must be executable"
