"""Tests for the no-hardcoded-models hook."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from dev.lint.lint_no_hardcoded_models import (
    Hit,
    _find_new_hits,
    _find_stale_allowances,
    _load_allowlist,
    scan,
)


def test_scan_flags_python_model_assignment(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty.py"
    dirty.write_text('DEFAULT_MODEL = "databricks-claude-sonnet-4-6"\n')

    assert [(hit.line, hit.model) for hit in scan(dirty)] == [
        (1, "databricks-claude-sonnet-4-6"),
    ]


def test_scan_ignores_python_prose_mentions(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text('"""For example, databricks-claude-sonnet-4-6."""\n')

    assert scan(clean) == []


def test_scan_flags_yaml_model_key(tmp_path: Path) -> None:
    dirty = tmp_path / "agent.yaml"
    dirty.write_text(
        "# model example: databricks-gpt-5-4\n"
        "model: databricks-gpt-5-5\n"
        "notes: databricks-gpt-5-5\n"
    )

    assert [(hit.line, hit.model) for hit in scan(dirty)] == [(2, "databricks-gpt-5-5")]


def test_scan_ignores_tests(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_models.py"
    test_file.parent.mkdir()
    test_file.write_text('MODEL = "databricks-gpt-5-5"\n')

    assert scan(test_file) == []


def test_find_new_hits_allows_only_curated_count() -> None:
    path = Path("omnigent/example.py")
    hits = [
        Hit(path, 1, "databricks-gpt-5-5"),
        Hit(path, 2, "databricks-gpt-5-5"),
    ]
    allowed = Counter({("omnigent/example.py", "databricks-gpt-5-5"): 1})

    assert _find_new_hits(hits, allowed) == [hits[1]]


def test_find_stale_allowances_requires_ratchet_down() -> None:
    path = Path("omnigent/example.py")
    hits = [Hit(path, 1, "databricks-gpt-5-5")]
    allowed = Counter({("omnigent/example.py", "databricks-gpt-5-5"): 2})

    assert _find_stale_allowances(hits, allowed) == Counter(
        {("omnigent/example.py", "databricks-gpt-5-5"): 1}
    )


def test_load_allowlist_rejects_duplicate_path_model(tmp_path: Path) -> None:
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text(
        "omnigent/example.py databricks-gpt-5-5 1\nomnigent/example.py databricks-gpt-5-5 1\n"
    )

    with pytest.raises(ValueError, match="duplicate baseline entry"):
        _load_allowlist(allowlist)
