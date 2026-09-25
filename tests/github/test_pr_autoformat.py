from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "pr-template" / "format_body.py"
_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "autoformat-pr.yml"
sys.path.insert(0, str(_SCRIPT.parent))
_SPEC = importlib.util.spec_from_file_location("pr_autoformat", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
pr_autoformat = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pr_autoformat)


def test_wraps_existing_description_in_summary_without_deleting_it() -> None:
    formatted = pr_autoformat.format_body("Fix the important thing.")

    assert formatted.startswith("## Summary\n\nFix the important thing.")
    assert "## Type of change" in formatted
    assert "- [ ] Bug fix" in formatted
    assert "## Test coverage" in formatted
    assert "- [ ] Unit tests added / updated" in formatted


def test_preserves_existing_sections_without_adding_explanation_scaffolds() -> None:
    original = "## Summary\n\nExisting summary.\n\n## Type of change\n\n- [x] Feature\n"
    formatted = pr_autoformat.format_body(original)

    assert "Existing summary." in formatted
    assert "- [x] Feature" in formatted
    assert formatted.count("## Summary") == 1
    assert formatted.count("## Type of change") == 1
    assert "## ELI5" not in formatted
    assert "## Diagram" not in formatted
    assert "## Test Plan" in formatted
    assert "## Coverage notes" in formatted
    assert "## Changelog" in formatted


def test_preserves_an_authored_diagram() -> None:
    original = "## Summary\n\nClear summary.\n\n## Diagram\n\nA -> B\n"

    formatted = pr_autoformat.format_body(original)

    assert formatted.count("## Diagram") == 1
    assert "A -> B" in formatted


def test_cli_adds_template_without_unrequested_explanation(tmp_path: Path) -> None:
    source = tmp_path / "body.md"
    destination = tmp_path / "formatted.md"
    source.write_text("A short, plain-language fix.\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    body = destination.read_text(encoding="utf-8")
    assert "## Summary\n\nA short, plain-language fix." in body
    assert "## Test Plan" in body
    assert "## ELI5" not in body
    assert "## Diagram" not in body


def test_scaffolds_changelog_section_with_delete_placeholder() -> None:
    formatted = pr_autoformat.format_body("Fix the important thing.")
    assert "## Changelog" in formatted
    # The scaffolded section defaults to the delete-if-not-noteworthy placeholder.
    assert formatted.rstrip().endswith("else delete this section>")


@pytest.mark.posix_only
@pytest.mark.skipif(shutil.which("jq") is None, reason="the workflow requires jq")
def test_workflow_keeps_an_already_formatted_body_unchanged(tmp_path: Path) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(
        step
        for step in workflow["jobs"]["autoformat"]["steps"]
        if step.get("name") == "Autoformat PR body and assign author"
    )
    extraction = [
        line.strip()
        for line in step["run"].splitlines()
        if line.strip().startswith("jq ") and '> "$body_file"' in line
    ]
    assert len(extraction) == 1

    body = pr_autoformat.format_body(
        "## Summary\n\nExisting text.\n\n## ELI5\n\nAuthored explanation.\n\n"
        "## Diagram\n\nA -> B\n"
    )
    source = tmp_path / "body.md"
    destination = tmp_path / "formatted.md"
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", extraction[0]],
        env={**os.environ, "pr_json": json.dumps({"body": body}), "body_file": str(source)},
        check=True,
    )
    subprocess.run(
        [sys.executable, str(_SCRIPT), str(source), str(destination)],
        check=True,
    )

    assert source.read_text() == body
    assert destination.read_text() == body
