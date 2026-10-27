"""Tests for ``dev/lint/lint_floating_runner_image.py``."""

from __future__ import annotations

import dev.lint.lint_floating_runner_image as lint

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
      - name: Report ubuntu-latest drift
        run: echo "ubuntu-latest is prose here"
  expression:
    runs-on: ${{ inputs.os || 'ubuntu-latest' }}
  large:
    runs-on: macos-latest-xlarge
  lookalike:
    runs-on: ubuntu-latest-ish
"""


def test_flags_floating_labels_in_values_and_expressions_but_not_prose() -> None:
    assert lint.floating_labels(_WORKFLOW) == [
        (6, "ubuntu-latest"),
        (9, "ubuntu-latest"),
        (13, "windows-latest"),
        (21, "ubuntu-latest"),
        (23, "macos-latest-xlarge"),
    ]
