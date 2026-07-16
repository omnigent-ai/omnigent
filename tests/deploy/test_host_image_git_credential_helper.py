"""Tests for the managed host image's host-scoped git credential helper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HELPER = _ROOT / "deploy/docker/git-credential-omnigent"


def _run(operation: str, stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the helper script under ``sh`` with a scrubbed environment.

    :param operation: The git credential operation argument, e.g. ``"get"``.
    :param stdin: The credential attributes git would feed the helper.
    :param env: Environment variables to expose to the helper.
    :returns: The completed subprocess.
    """
    return subprocess.run(
        ["sh", str(_HELPER), operation],
        input=stdin,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_helper_script_is_posix_sh_and_executable() -> None:
    """The helper ships as an executable POSIX ``sh`` script.

    The images' ``/bin/sh`` is dash (Debian slim) or bash-as-sh (UBI9), and the
    OpenShell non-root user must be able to exec it.
    """
    assert _HELPER.read_text().startswith("#!/bin/sh\n")
    assert os.access(_HELPER, os.X_OK)


def test_host_scoped_token_wins_over_default_and_default_never_leaks() -> None:
    """A host-scoped token answers that host — the default must not be offered.

    The reported bug: with only ``GIT_TOKEN`` set, a GitHub PAT is handed
    verbatim to an unrelated self-hosted remote.
    """
    result = _run(
        "get",
        "protocol=https\nhost=git.example.com\n\n",
        {"GIT_TOKEN": "github-pat", "GIT_TOKEN_GIT_EXAMPLE_COM": "forgejo-token"},
    )

    assert result.returncode == 0
    assert "password=forgejo-token" in result.stdout
    assert "github-pat" not in result.stdout


def test_default_token_still_answers_unscoped_hosts() -> None:
    """Hosts without a scoped var keep falling back to GIT_TOKEN / GIT_USERNAME."""
    result = _run(
        "get",
        "protocol=https\nhost=github.com\n\n",
        {"GIT_TOKEN": "github-pat", "GIT_TOKEN_GIT_EXAMPLE_COM": "forgejo-token"},
    )

    assert result.returncode == 0
    assert "username=x-access-token" in result.stdout
    assert "password=github-pat" in result.stdout
    assert "forgejo-token" not in result.stdout


def test_host_scoped_username_overrides_the_default_username() -> None:
    """GitLab-style per-host usernames override the global one."""
    result = _run(
        "get",
        "protocol=https\nhost=git.example.com\n\n",
        {
            "GIT_TOKEN": "github-pat",
            "GIT_USERNAME": "octocat",
            "GIT_TOKEN_GIT_EXAMPLE_COM": "forgejo-token",
            "GIT_USERNAME_GIT_EXAMPLE_COM": "oauth2",
        },
    )

    assert result.returncode == 0
    assert "username=oauth2" in result.stdout
    assert "password=forgejo-token" in result.stdout


def test_host_scoped_token_without_scoped_username_uses_the_default_username() -> None:
    """A scoped token pairs with GIT_USERNAME when no scoped username is set."""
    result = _run(
        "get",
        "protocol=https\nhost=git.example.com\n\n",
        {
            "GIT_TOKEN": "github-pat",
            "GIT_USERNAME": "octocat",
            "GIT_TOKEN_GIT_EXAMPLE_COM": "forgejo-token",
        },
    )

    assert result.returncode == 0
    assert "username=octocat" in result.stdout
    assert "password=forgejo-token" in result.stdout


def test_port_in_host_becomes_part_of_the_env_suffix() -> None:
    """git's ``host=`` carries ``:port``; the suffix encodes it too."""
    result = _run(
        "get",
        "protocol=https\nhost=git.example.com:8443\n\n",
        {"GIT_TOKEN_GIT_EXAMPLE_COM_8443": "ported-token"},
    )

    assert result.returncode == 0
    assert "password=ported-token" in result.stdout


def test_no_token_at_all_emits_nothing() -> None:
    """Anonymous clones of public repositories stay untouched."""
    result = _run("get", "protocol=https\nhost=github.com\n\n", {})

    assert result.returncode == 0
    assert result.stdout == ""


def test_scoped_token_for_another_host_does_not_answer() -> None:
    """A scoped var must not answer a host it was not scoped to."""
    result = _run(
        "get",
        "protocol=https\nhost=github.com\n\n",
        {"GIT_TOKEN_GIT_EXAMPLE_COM": "forgejo-token"},
    )

    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("operation", ["store", "erase"])
def test_non_get_operations_are_no_ops(operation: str) -> None:
    """store/erase stay silent no-ops, as the inline helper was."""
    result = _run(
        operation,
        "protocol=https\nhost=github.com\n\n",
        {"GIT_TOKEN": "github-pat"},
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_helper_terminates_on_eof_without_a_trailing_blank_line() -> None:
    """A caller that closes stdin without a blank line must not hang the helper."""
    result = _run("get", "protocol=https\nhost=github.com\n", {"GIT_TOKEN": "github-pat"})

    assert result.returncode == 0
    assert "password=github-pat" in result.stdout


@pytest.mark.parametrize(
    "dockerfile",
    [
        _ROOT / "deploy/docker/Dockerfile",
        _ROOT / "deploy/docker/Dockerfile.ubi",
    ],
)
def test_host_images_install_the_helper_on_path(dockerfile: Path) -> None:
    """Both host images ship the script and point git's system config at it.

    ``credential.helper omnigent`` resolves to ``git-credential-omnigent`` on
    PATH; the old inline one-liner ignored git's ``host=`` attribute.
    """
    text = dockerfile.read_text()

    assert "deploy/docker/git-credential-omnigent" in text
    assert "/usr/local/bin/git-credential-omnigent" in text
    assert "git config --system credential.helper omnigent" in text
    assert "!f() {" not in text
