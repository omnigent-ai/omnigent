"""Tests for the no-hardcoded-models hook."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from dev.lint.lint_no_hardcoded_models import (
    OWNED_FALLBACK_PATH,
    SOURCE_EXTENSIONS,
    _iter_scannable_paths,
    scan,
)


def test_scan_flags_python_model_assignment(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty.py"
    dirty.write_text('DEFAULT_MODEL = "databricks-claude-sonnet-4-6"\n')

    assert [(hit.line, hit.model) for hit in scan(dirty)] == [
        (1, "databricks-claude-sonnet-4-6"),
    ]


def test_scan_flags_python_positional_argument(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty.py"
    dirty.write_text('resolve("databricks-claude-sonnet-4-6")\n')

    assert [(hit.line, hit.model) for hit in scan(dirty)] == [
        (1, "databricks-claude-sonnet-4-6"),
    ]


def test_scan_flags_python_bare_collection(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty.py"
    dirty.write_text('("databricks-claude-sonnet-4-6", "gpt-oss-120b")\n')

    assert [(hit.line, hit.model) for hit in scan(dirty)] == [
        (1, "databricks-claude-sonnet-4-6"),
        (1, "gpt-oss-120b"),
    ]


def test_scan_ignores_model_family_fragments(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text(
        'prefixes = ("databricks-claude-opus-", "databricks-claude-fable-")\n'
        'families = ("gpt-oss",)\n'
    )

    assert scan(clean) == []


def test_scan_ignores_python_docstrings(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text(
        '"""For example, databricks-claude-sonnet-4-6."""\n'
        "class Example:\n"
        '    """For example, gpt-5.5."""\n'
        "    def sync(self):\n"
        '        """For example, claude-opus-4-1."""\n'
        "    async def async_(self):\n"
        '        """For example, gemini-3-pro."""\n'
    )

    assert scan(clean) == []


def test_scan_allows_complete_central_fallback_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fallback_module = Path("omnigent/model_fallbacks.py")
    fallback_module.parent.mkdir()
    fallback_module.write_text(
        '_MODELS = ("gpt-5.5", "gpt-5.4")\n'
        'FIRST = StaticModelFallback(model_ids=_MODELS, owner="one", '
        'provenance="release", discovery_gap="no API")\n'
        'SECOND = StaticModelFallback(model_ids=_MODELS, owner="two", '
        'provenance="release", discovery_gap="no API")\n'
    )

    assert scan(fallback_module) == []


def test_owned_fallback_registry_satisfies_structural_boundary() -> None:
    """The production registry needs no count-based model allowances."""
    assert OWNED_FALLBACK_PATH.is_file()
    assert scan(OWNED_FALLBACK_PATH) == []


def test_scan_flags_incomplete_central_fallback_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fallback_module = Path("omnigent/model_fallbacks.py")
    fallback_module.parent.mkdir()
    fallback_module.write_text(
        '_MODELS = ("gpt-5.5",)\n'
        'FALLBACK = StaticModelFallback(model_ids=_MODELS, owner="one", '
        'provenance="release", discovery_gap="")\n'
    )

    assert [(hit.line, hit.model) for hit in scan(fallback_module)] == [(1, "gpt-5.5")]


def test_scan_flags_fallback_model_tuple_used_outside_owned_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fallback_module = Path("omnigent/model_fallbacks.py")
    fallback_module.parent.mkdir()
    fallback_module.write_text(
        '_MODELS = ("gpt-5.5",)\n'
        'FALLBACK = StaticModelFallback(model_ids=_MODELS, owner="one", '
        'provenance="release", discovery_gap="no API")\n'
        "DEFAULT_MODELS = _MODELS\n"
    )

    assert [(hit.line, hit.model) for hit in scan(fallback_module)] == [(1, "gpt-5.5")]


def test_scan_flags_fallback_shape_outside_central_module(tmp_path: Path) -> None:
    dirty = tmp_path / "fallbacks.py"
    dirty.write_text(
        'FALLBACK = StaticModelFallback(model_ids=("gpt-5.5",), owner="one", '
        'provenance="release", discovery_gap="no API")\n'
    )

    assert [(hit.line, hit.model) for hit in scan(dirty)] == [(1, "gpt-5.5")]


def test_scan_flags_yaml_model_key(tmp_path: Path) -> None:
    dirty = tmp_path / "agent.yaml"
    dirty.write_text(
        "# model example: databricks-gpt-5-4\n"
        "model: databricks-gpt-5-5\n"
        "notes: databricks-gpt-5-5\n"
    )

    assert [(hit.line, hit.model) for hit in scan(dirty)] == [
        (2, "databricks-gpt-5-5"),
        (3, "databricks-gpt-5-5"),
    ]


def test_scan_ignores_tests(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_models.py"
    test_file.parent.mkdir()
    test_file.write_text('MODEL = "databricks-gpt-5-5"\n')

    assert scan(test_file) == []


def test_precommit_trigger_matches_scan_surface() -> None:
    config = yaml.safe_load(Path(".pre-commit-config.yaml").read_text())
    hook = next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "no-hardcoded-models"
    )
    files_pattern = re.compile(hook["files"])
    global_exclude_pattern = re.compile(config["exclude"])
    hook_exclude_pattern = re.compile(hook["exclude"])
    tracked_paths = {
        Path(path) for path in subprocess.check_output(["git", "ls-files"]).decode().splitlines()
    }
    triggered_paths = {
        path
        for path in tracked_paths
        if files_pattern.search(path.as_posix())
        and not global_exclude_pattern.search(path.as_posix())
        and not hook_exclude_pattern.search(path.as_posix())
    }
    scanned_paths = set(_iter_scannable_paths())

    assert triggered_paths == scanned_paths
    for extension in SOURCE_EXTENSIONS:
        assert files_pattern.search(Path(f"lint_probe{extension}").as_posix())
