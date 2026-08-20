"""Tests for the no-skipped-tests hook (``dev/lint/lint_no_skipped_tests.py``).

Unconditional ``@pytest.mark.skip``, module-level
``pytestmark = pytest.mark.skip(...)``, and bare module-scope
``pytest.skip(...)`` are invisible coverage loss. ``@pytest.mark.skipif``
(a genuine environmental gate) is exempt. The scanner returns
``(line, message)`` pairs.
"""

from __future__ import annotations

from pathlib import Path

from dev.lint.lint_no_skipped_tests import main, scan


def test_scan_flags_decorator_skip(tmp_path: Path) -> None:
    """``@pytest.mark.skip`` on a test trips the hook, naming the function."""
    f = tmp_path / "test_sample.py"
    f.write_text(
        'import pytest\n@pytest.mark.skip(reason="later")\ndef test_thing() -> None: ...\n'
    )
    hits = scan(f)
    # The decorator line (2) is flagged; the message names the function so
    # the CI failure is actionable.
    assert len(hits) == 1, f"Expected exactly one skip flagged, got {hits!r}."
    assert hits[0][0] == 2, f"Skip should be flagged on the decorator line 2, got {hits[0][0]}."
    assert "test_thing" in hits[0][1], f"Message should name the function, got {hits[0][1]!r}."


def test_scan_flags_module_level_pytestmark_skip(tmp_path: Path) -> None:
    """A module-level ``pytestmark = pytest.mark.skip(...)`` trips the hook."""
    f = tmp_path / "test_sample.py"
    f.write_text("import pytest\npytestmark = pytest.mark.skip(reason='wip')\n")
    hits = scan(f)
    assert len(hits) == 1, f"Expected one module-level skip flagged, got {hits!r}."
    assert hits[0][0] == 2  # the pytestmark assignment line


def test_scan_ignores_skipif(tmp_path: Path) -> None:
    """``@pytest.mark.skipif`` is a real environmental gate and must stay green."""
    f = tmp_path / "test_sample.py"
    f.write_text(
        "import pytest, sys\n"
        '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n'
        "def test_thing() -> None: ...\n"
    )
    assert scan(f) == [], "skipif must not be flagged — it is a conditional gate, not a skip."


def test_main_exit_codes(tmp_path: Path) -> None:
    """``main`` returns 1 on an unconditional skip, 0 on a skipif."""
    dirty = tmp_path / "test_dirty.py"
    dirty.write_text("import pytest\n@pytest.mark.skip\ndef test_x() -> None: ...\n")
    clean = tmp_path / "test_clean.py"
    clean.write_text(
        "import pytest, sys\n"
        '@pytest.mark.skipif(sys.platform == "win32", reason="x")\n'
        "def test_x() -> None: ...\n"
    )
    assert main(["lint_no_skipped_tests.py", str(dirty)]) == 1
    assert main(["lint_no_skipped_tests.py", str(clean)]) == 0
