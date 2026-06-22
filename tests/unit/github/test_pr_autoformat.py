from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[3] / ".github" / "scripts" / "pr-template" / "format_body.py"
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


def test_preserves_existing_sections_and_adds_missing_optional_context() -> None:
    original = "## Summary\n\nExisting summary.\n\n## Type of change\n\n- [x] Feature\n"
    formatted = pr_autoformat.format_body(original)

    assert "Existing summary." in formatted
    assert "- [x] Feature" in formatted
    assert formatted.count("## Summary") == 1
    assert formatted.count("## Type of change") == 1
    assert "## ELI5" in formatted
    assert "## Diagram" in formatted
    assert "## Coverage rationale" in formatted
