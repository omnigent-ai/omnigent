"""Tests for the no-hardcoded-models hook."""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml

from dev.lint.lint_no_hardcoded_models import (
    ALLOWLIST_PATH,
    SCAN_ROOTS,
    SOURCE_EXTENSIONS,
    Hit,
    _find_new_hits,
    _find_stale_allowances,
    _iter_scannable_paths,
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


def test_load_allowlist_reports_malformed_count_location(tmp_path: Path) -> None:
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("omnigent/example.py databricks-gpt-5-5 many\n")

    with pytest.raises(ValueError) as error:
        _load_allowlist(allowlist)

    assert str(error.value) == f"{allowlist}:1: count must be an integer, got 'many'"


def test_precommit_trigger_matches_scan_surface() -> None:
    config = yaml.safe_load(Path(".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "no-hardcoded-models"
    )
    files_pattern = re.compile(hook["files"])
    exclude_pattern = re.compile(config["exclude"])
    tracked_paths = {
        Path(path) for path in subprocess.check_output(["git", "ls-files"]).decode().splitlines()
    }
    triggered_paths = {
        path
        for path in tracked_paths
        if files_pattern.search(path.as_posix()) and not exclude_pattern.search(path.as_posix())
    }
    scanned_paths = set(_iter_scannable_paths()) | {ALLOWLIST_PATH}

    assert triggered_paths == scanned_paths
    for root in SCAN_ROOTS:
        for extension in SOURCE_EXTENSIONS:
            assert files_pattern.fullmatch(f"{root.as_posix()}/lint_probe{extension}")
