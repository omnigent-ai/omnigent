from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3] / ".github" / "scripts" / "pr-size" / "compute_label.py"
)
spec = importlib.util.spec_from_file_location("compute_pr_size", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@pytest.mark.parametrize(
    "total, expected",
    [
        (0, "size/XS"),
        (9, "size/XS"),
        (10, "size/S"),
        (49, "size/S"),
        (50, "size/M"),
        (199, "size/M"),
        (200, "size/L"),
        (499, "size/L"),
        (500, "size/XL"),
        (10_000, "size/XL"),
    ],
)
def test_size_label_boundaries(total: int, expected: str) -> None:
    assert module.size_label(total) == expected


@pytest.mark.parametrize(
    "filename",
    ["uv.lock", "package-lock.json", "ap-web/package-lock.json", "ap-web/electron/yarn.lock"],
)
def test_lock_files_are_generated(filename: str) -> None:
    assert module.is_generated(filename)


@pytest.mark.parametrize(
    "filename",
    ["omnigent/runtime/workflow.py", "docs/uv.lock.md", "src/package-lock.json.bak"],
)
def test_source_files_are_not_generated(filename: str) -> None:
    assert not module.is_generated(filename)


def test_total_changes_excludes_generated() -> None:
    files = [
        {"filename": "omnigent/a.py", "additions": 30, "deletions": 10},
        {"filename": "uv.lock", "additions": 5000, "deletions": 4000},
        {"filename": "ap-web/package-lock.json", "additions": 800, "deletions": 0},
    ]
    # Only the source file counts: 30 + 10 = 40 -> size/S.
    assert module.total_changes(files) == 40
    assert module.size_label(module.total_changes(files)) == "size/S"


def test_total_changes_missing_fields_default_to_zero() -> None:
    assert module.total_changes([{"filename": "x.py"}]) == 0
