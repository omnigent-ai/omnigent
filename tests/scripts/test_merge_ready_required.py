"""``required.sh``'s ``is_allow_skip`` under evaluate-checks.sh's shell options."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REQUIRED = Path(__file__).resolve().parents[2] / ".github/scripts/merge-ready/required.sh"


def _is_allow_skip(name: str, *, filler_entries: int = 0) -> int:
    """Return ``is_allow_skip``'s exit status, optionally with entries appended to the list."""
    script = (
        "set -euo pipefail\n"
        f'source "{REQUIRED}"\n'
        f'for i in $(seq {filler_entries}); do ALLOW_SKIP+=("filler check $i"); done\n'
        'is_allow_skip "$1"\n'
    )
    return subprocess.run(["bash", "-c", script, "bash", name], timeout=60).returncode


@pytest.mark.parametrize("name", ["Docker build", "E2E Tests (shard 1/4)", "Integration (codex)"])
def test_listed_check_is_skippable(name: str) -> None:
    assert _is_allow_skip(name) == 0


def test_listed_check_stays_skippable_when_more_entries_follow_it() -> None:
    """A listed name is found even when the list outlasts a pipe a reader stops draining."""
    # About 400 KB of entries after the match: far more than a pipe buffer holds.
    assert _is_allow_skip("E2E Tests (shard 1/4)", filler_entries=20_000) == 0


@pytest.mark.parametrize(
    "name",
    [
        "DCO",
        "Not a check",
        "E2E Tests (shard 1/4",
        "E2E Tests (shard */4)",
        "e2e tests (shard 1/4)",
        "",
    ],
)
def test_only_an_exact_listed_name_is_skippable(name: str) -> None:
    assert _is_allow_skip(name) == 1
