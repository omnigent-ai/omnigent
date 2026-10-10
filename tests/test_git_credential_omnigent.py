"""Tests for the host-aware sandbox git credential helper shell script.

The script (deploy/docker/git-credential-omnigent) is installed as git's
--system `credential.helper` in the managed-host image. It is pure POSIX sh, so
these tests invoke it as a subprocess with a fixture environment and the git
`get` request on stdin, matching exactly how git calls it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "docker" / "git-credential-omnigent"


def _run(host: str, env: dict[str, str], operation: str = "get") -> str:
    """Invoke the helper the way git does: operation as argv, request on stdin."""
    proc = subprocess.run(
        ["/bin/sh", str(SCRIPT), operation],
        input=f"protocol=https\nhost={host}\n\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _parse(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_host_specific_token_wins_over_shared() -> None:
    # A Forgejo host with its own GIT_TOKEN_<HOST>/GIT_USERNAME_<HOST> pair gets
    # its own credential, not the shared (GitHub) GIT_TOKEN.
    out = _run(
        "git.joyful.house",
        {
            "GIT_TOKEN": "gho_github",
            "GIT_USERNAME": "x-access-token",
            "GIT_TOKEN_GIT_JOYFUL_HOUSE": "forgejo_pat",
            "GIT_USERNAME_GIT_JOYFUL_HOUSE": "bryan",
        },
    )
    assert _parse(out) == {"username": "bryan", "password": "forgejo_pat"}


def test_falls_back_to_shared_token() -> None:
    # A host with no host-specific pair falls back to the shared GIT_TOKEN — the
    # original single-host behaviour, unchanged.
    out = _run("github.com", {"GIT_TOKEN": "gho_github", "GIT_USERNAME": "x-access-token"})
    assert _parse(out) == {"username": "x-access-token", "password": "gho_github"}


def test_username_defaults_to_x_access_token() -> None:
    out = _run("github.com", {"GIT_TOKEN": "gho_github"})
    assert _parse(out) == {"username": "x-access-token", "password": "gho_github"}


def test_host_specific_token_with_default_username() -> None:
    # Host-specific token but no host-specific username → x-access-token default,
    # not the shared GIT_USERNAME.
    out = _run(
        "git.joyful.house",
        {
            "GIT_USERNAME": "someone-else",
            "GIT_TOKEN_GIT_JOYFUL_HOUSE": "forgejo_pat",
        },
    )
    assert _parse(out) == {"username": "x-access-token", "password": "forgejo_pat"}


def test_declines_when_no_token() -> None:
    # No token for the host and no shared token → decline (empty), so git falls
    # through to the next helper or clones a public repo anonymously.
    assert _run("git.joyful.house", {}) == ""


def test_empty_host_specific_token_falls_back() -> None:
    # A host-specific var set to empty is treated as unset → shared fallback.
    out = _run(
        "git.joyful.house",
        {"GIT_TOKEN_GIT_JOYFUL_HOUSE": "", "GIT_TOKEN": "shared"},
    )
    assert _parse(out) == {"username": "x-access-token", "password": "shared"}


def test_host_normalization_ports_and_dashes() -> None:
    # Non-alphanumeric bytes (dots, dashes) all normalise to `_`.
    out = _run("code.my-lab.internal", {"GIT_TOKEN_CODE_MY_LAB_INTERNAL": "tok"})
    assert _parse(out) == {"username": "x-access-token", "password": "tok"}


@pytest.mark.parametrize("operation", ["store", "erase"])
def test_non_get_operations_are_noops(operation: str) -> None:
    assert _run("git.joyful.house", {"GIT_TOKEN": "shared"}, operation=operation) == ""
