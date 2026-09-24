from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "pr-template" / "format_body.py"
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
