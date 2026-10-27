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
      - run: echo "ubuntu-latest-ish names are not labels"
"""


def test_flags_floating_labels_but_not_pinned_images_or_prose() -> None:
    assert lint.floating_labels(_WORKFLOW) == [
        (6, "ubuntu-latest"),
        (9, "ubuntu-latest"),
        (13, "windows-latest"),
    ]
