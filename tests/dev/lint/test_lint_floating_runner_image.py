"""Tests for ``dev/lint/lint_floating_runner_image.py``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import dev.lint.lint_floating_runner_image as lint
from dev.lint import custom_lint

_WORKFLOW = """\
# Runs on ubuntu-latest today.  (prose in a comment is not a label)
on:
  workflow_call:
    inputs:
      runner:
        default: ubuntu-latest
jobs:
  build:
    runs-on: ubuntu-latest
  matrix:
    strategy:
      matrix:
        os: [windows-latest, ubuntu-24.04]
    runs-on: ${{ matrix.os }}
  pinned:
    runs-on: macos-15
    steps:
      - run: echo "ubuntu-latest-ish names are not labels"
"""


def test_flags_labels_in_runs_on_matrix_and_input_defaults() -> None:
    assert lint.floating_labels(_WORKFLOW) == [
        (6, "ubuntu-latest"),
        (9, "ubuntu-latest"),
        (13, "windows-latest"),
    ]


def test_pinned_images_pass() -> None:
    assert lint.floating_labels("jobs:\n  a:\n    runs-on: ubuntu-24.04\n") == []


@pytest.mark.parametrize("suffix", [".yml", ".yaml"])
def test_check_scans_tracked_workflow_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    workflow = tmp_path / ".github" / "workflows" / f"ci{suffix}"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs:\n  a:\n    runs-on: ubuntu-latest\n")
    (tmp_path / ".github" / "notes.md").write_text("runs-on: ubuntu-latest\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    monkeypatch.chdir(tmp_path)
    assert lint.check() == [
        f".github/workflows/ci{suffix}:3: floating runner image `ubuntu-latest`"
    ]


def test_repository_workflows_are_pinned() -> None:
    assert lint.check() == []


def test_rule_is_registered() -> None:
    assert any(rule.name == lint.RULE_NAME for rule in custom_lint.RULES)
