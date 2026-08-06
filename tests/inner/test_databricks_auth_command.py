"""Tests for the generated Databricks bearer-token helper command.

The one command every harness installs to mint a workspace token
(``apiKeyHelper`` for Claude Code, ``auth.command`` for Codex), so its profile
selection and its refresh behaviour are asserted in one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _run_generated_helper(
    command: str,
    tmp_path: Path,
    *,
    force_refresh_works: bool,
    cached_token_works: bool,
) -> str:
    """
    Run *command* against a fake ``databricks`` CLI and return its stdout.

    :param command: The generated helper command.
    :param tmp_path: Directory the fake CLI is written into and prepended to
        ``PATH``.
    :param force_refresh_works: Whether ``--force-refresh`` succeeds. ``False``
        is the stale-refresh-token case.
    :param cached_token_works: Whether plain ``auth token`` succeeds.
    :returns: The helper's stdout, stripped.
    """
    import os
    import subprocess

    fake = tmp_path / "databricks"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in *--help*) echo "  --force-refresh"; exit 0;; esac\n'
        'case "$*" in *--force-refresh*)\n'
        '  [ "$FORCE_OK" = 1 ] && { echo \'{"access_token":"fresh"}\'; exit 0; }\n'
        "  exit 1;;\n"
        "*)\n"
        '  [ "$CACHED_OK" = 1 ] && { echo \'{"access_token":"cached"}\'; exit 0; }\n'
        "  exit 1;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "FORCE_OK": "1" if force_refresh_works else "0",
        "CACHED_OK": "1" if cached_token_works else "0",
    }
    env.pop("DATABRICKS_BEARER", None)
    result = subprocess.run(
        ["sh", "-c", command], env=env, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


@pytest.mark.posix_only
def test_the_generated_helper_serves_the_cached_token_when_a_refresh_fails(
    tmp_path: Path,
) -> None:
    """
    A stale refresh token must not cost a perfectly valid cached access token.

    ``--force-refresh`` is worth attempting — it renews a still-valid token and
    keeps a long gateway session off a mid-session 401 — but it fails outright
    once the refresh token has gone stale. Forcing it unconditionally turned
    that into a hard auth failure with a usable credential sitting right there.
    """
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command("https://example.databricks.com", "agent")

    # Healthy: the forced refresh answers, so the token is the fresh one.
    assert (
        _run_generated_helper(command, tmp_path, force_refresh_works=True, cached_token_works=True)
        == "fresh"
    )
    # The live incident: the refresh token is stale, the cached one is fine.
    assert (
        _run_generated_helper(
            command, tmp_path, force_refresh_works=False, cached_token_works=True
        )
        == "cached"
    )
    # Genuinely unauthenticated: nothing to print, and no crash.
    assert (
        _run_generated_helper(
            command, tmp_path, force_refresh_works=False, cached_token_works=False
        )
        == ""
    )


def test_the_generated_helper_selects_by_profile_when_one_is_named() -> None:
    """
    A named profile is unambiguous; host lookup is only the fallback.

    Two profiles can share a workspace host, and selecting by host then either
    fails outright or resolves to whichever the CLI picks — which is how a pane
    and the server end up authenticating as different identities.
    """
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    named = databricks_bearer_token_command("https://example.databricks.com", "agent")
    assert '--profile "agent"' in named
    assert "--host" not in named

    unnamed = databricks_bearer_token_command("https://example.databricks.com/", None)
    assert '--host "https://example.databricks.com"' in unnamed
    assert "--profile" not in unnamed
