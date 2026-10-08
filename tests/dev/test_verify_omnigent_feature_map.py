"""The verify-omnigent feature map stays consistent with the repository.

The map (``feature-map/``) points at tests instead of
copying selectors, so these checks turn its most common drift into a failure: a
renamed or deleted test, a feature file missing from the index, a file that
breaks the entry contract, a native harness added without a matrix row, or a UI
test area or CLI command that is neither mapped nor on the index's "Not yet
mapped" checklist.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FEATURES = _REPO_ROOT / "feature-map"
_SKILL = _FEATURES / "skills" / "verify-omnigent"
_SECTIONS = [
    "Sub-features",
    "How to get to it (user POV)",
    "Driving it with the repro environment",
    "Gotchas",
]
_TEST_REF = re.compile(r"\b(tests/[\w/.-]+\.py)(?:::(\w+))?")
_UI_LANES = ("e2e_ui", "browser_ui")
_UI_AREA_REF = re.compile(r"\btests/(e2e_ui|browser_ui)/(\w+)/")
_CLI_REF = re.compile(r"`omnigent ([a-z][\w-]*)")


def _feature_files() -> list[Path]:
    return sorted(p for p in _FEATURES.glob("*.md") if p.name != "README.md")


def _index_section(title: str) -> str:
    index = (_FEATURES / "README.md").read_text()
    return index.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]


def _feature_text() -> str:
    return "\n".join(p.read_text() for p in _feature_files())


def test_index_links_every_feature_file() -> None:
    features = _index_section("Features")
    linked = set(re.findall(r"\]\(\./([\w-]+\.md)\)", features))
    present = {p.name for p in _feature_files()}
    assert linked == present, (
        f"feature-map/README.md must link exactly the feature files; "
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


def _ui_test_areas() -> set[tuple[str, str]]:
    areas = set()
    for lane in _UI_LANES:
        for area in (_REPO_ROOT / "tests" / lane).iterdir():
            if area.is_dir() and any(area.rglob("test_*.py")):
                areas.add((lane, area.name))
    return areas


def test_every_ui_test_area_is_mapped_or_listed() -> None:
    areas = _ui_test_areas()
    assert areas, "expected UI test areas under tests/e2e_ui and tests/browser_ui"
    mapped = set(_UI_AREA_REF.findall(_feature_text()))
    listed = set(_UI_AREA_REF.findall(_index_section("Not yet mapped")))
    missing = sorted(f"tests/{lane}/{area}/" for lane, area in areas - mapped - listed)
    assert not missing, (
        f"UI test areas with no feature file: {missing}. Map them, or list them under "
        "'Not yet mapped' in feature-map/README.md"
    )
    stale = sorted(f"tests/{lane}/{area}/" for lane, area in listed - areas)
    assert not stale, f"'Not yet mapped' lists UI test areas that no longer exist: {stale}"


def test_every_cli_command_is_mapped_or_listed() -> None:
    import click

    from omnigent.cli import cli

    ctx = click.Context(cli)
    commands = {name for name in cli.list_commands(ctx) if not cli.get_command(ctx, name).hidden}
    assert commands, "expected visible top-level omnigent commands"
    mapped = set(_CLI_REF.findall(_feature_text()))
    listed = set(_CLI_REF.findall(_index_section("Not yet mapped")))
    missing = sorted(commands - mapped - listed)
    assert not missing, (
        f"CLI commands with no feature file: {missing}. Map them, or list them under "
        "'Not yet mapped' in feature-map/README.md"
    )
    stale = sorted(listed - commands)
    assert not stale, f"'Not yet mapped' lists CLI commands that no longer exist: {stale}"


def test_package_document_links_resolve() -> None:
    for document in _FEATURES.rglob("*.md"):
        for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
            path = target.split("#", 1)[0]
            if not path or "://" in path:
                continue
            assert (document.parent / path).exists(), f"{document}: broken link {target}"
