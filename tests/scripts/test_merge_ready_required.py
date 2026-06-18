from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github/scripts/merge-ready/required.sh"


def _is_allow_skip(
    check_name: str,
    *,
    is_fork: str = "false",
) -> bool:
    """Source required.sh and test is_allow_skip for *check_name*.

    :param is_fork: passed as IS_FORK env var ("true" or "false").
    :returns: True if the check is allowed to skip.
    """
    env = os.environ.copy()
    env["IS_FORK"] = is_fork
    # Source required.sh then call is_allow_skip; exit code is the result.
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{SCRIPT}" && is_allow_skip "{check_name}"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.returncode == 0


# --- Same-repo (IS_FORK=false): ALLOW_SKIP works normally ---


def test_e2e_shard_skippable_for_same_repo() -> None:
    """Same-repo PRs can skip e2e shards (path-filtered, e.g. ap-web-only)."""
    assert _is_allow_skip("E2E Tests (shard 0/4)", is_fork="false") is True


def test_integration_skippable_for_same_repo() -> None:
    """Same-repo PRs can skip integration checks."""
    assert _is_allow_skip("Integration (codex)", is_fork="false") is True


def test_pytest_skippable_for_same_repo() -> None:
    """Same-repo PRs can skip Pytest shards."""
    assert _is_allow_skip("Pytest (runtime-core)", is_fork="false") is True


def test_precommit_not_skippable() -> None:
    """Pre-commit checks are never skippable (not in ALLOW_SKIP)."""
    assert _is_allow_skip("Pre-commit checks", is_fork="false") is False


# --- Fork PRs (IS_FORK=true): e2e/integration NOT skippable ---


def test_e2e_shard_not_skippable_for_fork() -> None:
    """Fork PRs must NOT skip e2e shards -- FORK_NEVER_SKIP overrides."""
    assert _is_allow_skip("E2E Tests (shard 0/4)", is_fork="true") is False
    assert _is_allow_skip("E2E Tests (shard 3/4)", is_fork="true") is False


def test_e2e_ui_shard_not_skippable_for_fork() -> None:
    """Fork PRs must NOT skip e2e UI shards."""
    assert _is_allow_skip("E2E UI Tests (shard 0/3)", is_fork="true") is False


def test_integration_not_skippable_for_fork() -> None:
    """Fork PRs must NOT skip integration checks."""
    assert _is_allow_skip("Integration (codex)", is_fork="true") is False
    assert _is_allow_skip("Integration (claude-sdk)", is_fork="true") is False
    assert _is_allow_skip("Integration (openai-agents)", is_fork="true") is False


def test_pytest_still_skippable_for_fork() -> None:
    """Fork PRs CAN still skip Pytest shards (not in FORK_NEVER_SKIP)."""
    assert _is_allow_skip("Pytest (runtime-core)", is_fork="true") is True


def test_is_fork_unset_defaults_to_skippable() -> None:
    """When IS_FORK is unset, the :-false default keeps ALLOW_SKIP intact."""
    env = os.environ.copy()
    env.pop("IS_FORK", None)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{SCRIPT}" && is_allow_skip "E2E Tests (shard 0/4)"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, "E2E should be skippable when IS_FORK is unset"
