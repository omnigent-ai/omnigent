"""CI's reused total matches coverage.py's report and failure behavior."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import coverage
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/ci/coverage-total.py"


def _run(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    return subprocess.run(
        [sys.executable, *args], cwd=directory, env=env, capture_output=True, text=True
    )


def _reports(directory: Path) -> tuple[Path, str]:
    markdown = _run(directory, "-m", "coverage", "report", "--format=markdown", "--ignore-errors")
    total = _run(directory, "-m", "coverage", "report", "--format=total", "--ignore-errors")
    assert markdown.returncode == total.returncode == 0, (markdown.stderr, total.stderr)
    path = directory / "report.md"
    path.write_text(markdown.stdout)
    return path, total.stdout


@pytest.mark.parametrize(
    ("covered", "precision"), [(0, 0), (1, 0), (1334, 0), (2000, 0), (2001, 0), (1334, 2)]
)
def test_total_matches_cli_rounding(tmp_path: Path, covered: int, precision: int) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n" * 2001)
    (tmp_path / ".coveragerc").write_text(f"[report]\nprecision = {precision}\n")
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({str(source): set(range(1, covered + 1))})
    data.write()
    markdown, expected = _reports(tmp_path)
    result = _run(tmp_path, str(SCRIPT), str(markdown))
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert result.stderr == ""  # The fast path must not fall back to a second analysis.


def test_branch_coverage_and_omitted_missing_source(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text('if __name__ == "__main__":\n    value = 1\nelse:\n    value = 2\n')
    ignored = tmp_path / "ignored.py"
    ignored.write_text("value = 3\n")
    (tmp_path / ".coveragerc").write_text("[run]\nbranch = true\n[report]\nomit = ignored.py\n")
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_arcs(
        {
            str(source): {(-1, 1), (1, 2), (2, -1)},
            str(ignored): {(-1, 1), (1, -1)},
            str(tmp_path / "generated.py"): {(-1, 1), (1, -1)},
        }
    )
    data.write()
    markdown, expected = _reports(tmp_path)
    assert "Branch" in markdown.read_text()
    assert "ignored.py" not in markdown.read_text()
    result = _run(tmp_path, str(SCRIPT), str(markdown))
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert result.stderr == ""


@pytest.mark.parametrize(
    "markdown_text",
    [
        "",
        "| **TOTAL** | **2** | **0** | **101%** |",
        "| **TOTAL** | **2** | **1** | **50%** |\n" * 2,
    ],
)
def test_changed_format_falls_back_to_cli(tmp_path: Path, markdown_text: str) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 1\nother = 2\n")
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({str(source): {1}})
    data.write()
    (tmp_path / "report.md").write_text(markdown_text)
    result = _run(tmp_path, str(SCRIPT), "report.md")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "50\n"
    assert "regenerating" in result.stderr


def test_fallback_failure_is_not_published_as_zero(tmp_path: Path) -> None:
    (tmp_path / "report.md").write_text("")
    result = _run(tmp_path, str(SCRIPT), "report.md")
    assert result.returncode != 0
    assert result.stdout != "0\n"
