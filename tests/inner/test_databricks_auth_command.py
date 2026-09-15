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
    bearer: str | None = None,
) -> str:
    """
    Run *command* against a fake ``databricks`` CLI and return its stdout.

    :param command: The generated helper command.
    :param tmp_path: Directory the fake CLI is written into and prepended to
        ``PATH``.
    :param force_refresh_works: Whether ``--force-refresh`` succeeds. ``False``
        is the stale-refresh-token case.
    :param cached_token_works: Whether plain ``auth token`` succeeds.
    :param bearer: Value for ``DATABRICKS_BEARER``, or ``None`` to unset it.
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
    # The command's last-resort fallback shells to the databricks-sdk mint
    # (``python -m omnigent.inner.databricks_token``). Strip ambient Databricks
    # resolution and point the config file at a nonexistent path so that mint is
    # deterministic — no host/creds/profile ⇒ it prints nothing — keeping these
    # assertions hermetic on any machine (incl. a Databricks-credentialed CI).
    for key in list(env):
        if key.startswith("DATABRICKS_"):
            env.pop(key, None)
    env["DATABRICKS_CONFIG_FILE"] = str(tmp_path / "nonexistent-databrickscfg")
    if bearer is not None:
        env["DATABRICKS_BEARER"] = bearer
    result = subprocess.run(
        ["sh", "-c", command], env=env, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def _recorded_fallback(tmp_path: Path) -> str:
    """
    Write a stub standing in for the token command ucode recorded.

    :param tmp_path: Directory on the helper's ``PATH``.
    :returns: The command to pass as ``fallback_command``; running it prints
        ``"recorded"`` and touches ``tmp_path / "fallback-ran"``.
    """
    stub = tmp_path / "ucode-token"
    stub.write_text(f"#!/bin/sh\n: > {tmp_path / 'fallback-ran'}\necho recorded\n")
    stub.chmod(0o755)
    # Quoting the stub exercises the eval path against a command that carries
    # its own quotes, the shape ucode actually records.
    return 'ucode-token --host "https://example.databricks.com"'


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
    # Scope to the CLI mint's selector: it selects by profile (host lookup is
    # ambiguous when two profiles share a workspace). The sdk fallback separately
    # passes --host for its own workspace guard, so assert on the mint, not the
    # whole command.
    assert 'databricks auth token --profile "agent"' in named
    assert "databricks auth token --host" not in named

    unnamed = databricks_bearer_token_command("https://example.databricks.com/", None)
    assert 'databricks auth token --host "https://example.databricks.com"' in unnamed
    assert "databricks auth token --profile" not in unnamed


@pytest.mark.posix_only
def test_the_named_profile_wins_and_the_recorded_command_stays_unused(
    tmp_path: Path,
) -> None:
    """The pinned profile is the identity we want whenever it can mint a token."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command(
        "https://example.databricks.com", "agent", fallback_command=_recorded_fallback(tmp_path)
    )

    assert (
        _run_generated_helper(command, tmp_path, force_refresh_works=True, cached_token_works=True)
        == "fresh"
    )
    assert not (tmp_path / "fallback-ran").exists()


@pytest.mark.posix_only
def test_the_recorded_command_runs_when_the_named_profile_yields_nothing(
    tmp_path: Path,
) -> None:
    """
    A config naming a credential-less profile must not 401 a working session.

    The user may have authenticated under a different profile on the same host,
    and the command ucode recorded is the one known to have worked.
    """
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command(
        "https://example.databricks.com", "DEFAULT", fallback_command=_recorded_fallback(tmp_path)
    )

    assert (
        _run_generated_helper(
            command, tmp_path, force_refresh_works=False, cached_token_works=False
        )
        == "recorded"
    )
    assert (tmp_path / "fallback-ran").exists()


@pytest.mark.posix_only
def test_a_working_profile_outranks_an_ambient_bearer(
    tmp_path: Path,
) -> None:
    """
    A stale shell bearer must not poison auth when the profile can mint.

    Ambient ``DATABRICKS_BEARER`` exports linger past their ~1h TTL, and when
    one shadowed the configured profile every turn failed 403 and re-login
    didn't help — the mint was never consulted. The profile is the configured
    identity, so its token wins whenever it can produce one.
    """
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command(
        "https://example.databricks.com", "agent", fallback_command=_recorded_fallback(tmp_path)
    )

    assert (
        _run_generated_helper(
            command,
            tmp_path,
            force_refresh_works=False,
            cached_token_works=True,
            bearer="stale-shell-export",
        )
        == "cached"
    )
    assert not (tmp_path / "fallback-ran").exists()


@pytest.mark.posix_only
def test_the_ambient_bearer_backstops_a_profile_that_cannot_mint(
    tmp_path: Path,
) -> None:
    """An injected bearer still carries an environment with no mintable profile.

    CI and runner launches export a resolved token precisely because the
    profile there has no OAuth state; when the mint yields nothing the bearer
    must be served, and the recorded fallback stays out of it.
    """
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command(
        "https://example.databricks.com", "agent", fallback_command=_recorded_fallback(tmp_path)
    )

    assert (
        _run_generated_helper(
            command,
            tmp_path,
            force_refresh_works=False,
            cached_token_works=False,
            bearer="injected",
        )
        == "injected"
    )
    assert not (tmp_path / "fallback-ran").exists()


@pytest.mark.posix_only
def test_without_a_recorded_command_an_unauthenticated_profile_stays_empty(
    tmp_path: Path,
) -> None:
    """No fallback to reach for: print nothing rather than crash."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    command = databricks_bearer_token_command("https://example.databricks.com", "agent")

    assert (
        _run_generated_helper(
            command, tmp_path, force_refresh_works=False, cached_token_works=False
        )
        == ""
    )


def _write_broker_sidecar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace_host: str):
    """Point the config file at *tmp_path* and drop a broker sidecar for *workspace_host*."""
    from omnigent.host import databricks_credential as dc

    cfg_path = tmp_path / ".databrickscfg"
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    assert dc._write_sidecar(
        cfg_path, "https://omni.example", "host-1", "host-tok", workspace_host
    )


def test_the_broker_sidecar_becomes_the_fallback_for_the_connected_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In a connect sandbox, the host-only profile delegates its mint to the broker."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    _write_broker_sidecar(monkeypatch, tmp_path, "https://example.databricks.com")
    command = databricks_bearer_token_command("https://example.databricks.com", "omnigent")
    assert "python3 -m omnigent.host.databricks_credential token" in command


def test_the_broker_sidecar_is_ignored_for_a_different_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway pinned to a spec's own workspace must not borrow the broker token."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    _write_broker_sidecar(monkeypatch, tmp_path, "https://connected.databricks.com")
    command = databricks_bearer_token_command("https://other.databricks.com", "myprofile")
    assert "omnigent.host.databricks_credential" not in command


def test_the_broker_is_ignored_for_a_different_profile_on_the_connected_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identity boundary: only the host-only connect profile (HOST_DATABRICKS_PROFILE)
    borrows the owner's broker bearer. A different, credential-less profile that
    happens to share the connected workspace host must NOT silently mint as the
    owner — distinct profiles on one host can be different users/service principals."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    _write_broker_sidecar(monkeypatch, tmp_path, "https://example.databricks.com")
    # Same connected workspace host as the sidecar, but NOT the connect profile.
    command = databricks_bearer_token_command("https://example.databricks.com", "someone-else")
    assert "omnigent.host.databricks_credential" not in command


def test_an_explicit_fallback_is_not_overridden_by_the_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller-supplied fallback (e.g. ucode's) wins over the broker default."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    _write_broker_sidecar(monkeypatch, tmp_path, "https://example.databricks.com")
    command = databricks_bearer_token_command(
        "https://example.databricks.com", "omnigent", fallback_command="ucode-token"
    )
    assert "omnigent.host.databricks_credential" not in command
    assert "ucode-token" in command


def test_no_sidecar_falls_back_to_the_sdk_mint_not_the_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A normal profile (no managed-connect broker sidecar) still carries a
    fallback: the databricks-sdk mint. That is the only path that resolves an
    OAuth M2M / service-principal (or OIDC) bearer once ``databricks auth token``
    (U2M-only) yields nothing, so a workload-identity host is no longer stuck.
    The broker is not referenced — that stays reserved for the connect profile."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / ".databrickscfg"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    cmd = databricks_bearer_token_command("https://example.databricks.com", "myprofile")
    assert "omnigent.host.databricks_credential" not in cmd
    assert (
        "python3 -m omnigent.inner.databricks_token "
        "--host https://example.databricks.com --profile myprofile" in cmd
    )
    # The eval-fallback clause now carries the sdk mint.
    assert "eval" in cmd


def test_the_sdk_fallback_selects_by_host_when_no_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no named profile the sdk fallback selects the workspace by host, so
    env / OIDC service-principal credentials mint against the pinned workspace."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / ".databrickscfg"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    cmd = databricks_bearer_token_command("https://example.databricks.com/", None)
    assert (
        "python3 -m omnigent.inner.databricks_token --host https://example.databricks.com" in cmd
    )


def test_the_connect_profile_tries_the_broker_then_the_sdk_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The connect profile mints via the broker first, then the sdk as a last
    resort. The broker is preferred but no longer *excludes* the sdk mint."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    _write_broker_sidecar(monkeypatch, tmp_path, "https://example.databricks.com")
    command = databricks_bearer_token_command("https://example.databricks.com", "omnigent")
    broker = command.index("omnigent.host.databricks_credential token")
    sdk = command.index("omnigent.inner.databricks_token")
    assert broker < sdk  # broker is tried before the sdk fallback


def test_an_explicit_fallback_still_chains_the_sdk_mint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for the native-Claude ``apiKeyHelper`` path: a caller-supplied
    fallback (ucode's recorded ``databricks auth token`` command) must not
    *exclude* the sdk mint — otherwise an M2M profile on that harness would never
    reach the SDK. Both run, recorded first."""
    from omnigent.inner.databricks_executor import databricks_bearer_token_command

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / ".databrickscfg"))
    command = databricks_bearer_token_command(
        "https://example.databricks.com", "agent", fallback_command="ucode-token"
    )
    recorded = command.index("ucode-token")
    sdk = command.index("omnigent.inner.databricks_token")
    assert recorded < sdk


def test_the_sdk_entrypoint_prints_the_resolved_bearer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The entrypoint prints the sdk-resolved bearer when the resolved workspace
    matches the requested gateway host (for an M2M profile this is the
    client-credentials token the CLI cannot mint)."""
    from omnigent.inner import databricks_token

    monkeypatch.setattr(
        databricks_token,
        "_sdk_bearer",
        lambda profile, host: ("https://example.databricks.com", "m2m-bearer"),
    )
    rc = databricks_token.main(["--host", "https://example.databricks.com", "--profile", "sp"])
    assert rc == 0
    assert capsys.readouterr().out == "m2m-bearer\n"


def test_the_sdk_entrypoint_fails_closed_on_a_workspace_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Identity guard: if the named profile resolves to a *different* workspace
    than the gateway host, print nothing rather than hand its bearer to this
    gateway."""
    from omnigent.inner import databricks_token

    monkeypatch.setattr(
        databricks_token,
        "_sdk_bearer",
        lambda profile, host: ("https://other.databricks.com", "someone-elses-token"),
    )
    rc = databricks_token.main(["--host", "https://example.databricks.com", "--profile", "sp"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_the_sdk_entrypoint_prints_nothing_when_unresolved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No credential ⇒ print nothing (exit 0) so the caller's shell falls through."""
    from omnigent.inner import databricks_token

    monkeypatch.setattr(databricks_token, "_sdk_bearer", lambda profile, host: None)
    rc = databricks_token.main(["--host", "https://example.databricks.com", "--profile", "sp"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_the_sdk_entrypoint_is_quiet_on_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A token-endpoint failure during the M2M exchange must not crash the mint;
    it prints nothing and the harness retries at the next refresh."""
    from omnigent.inner import databricks_token

    def _boom(profile: str | None, host: str | None) -> None:
        raise RuntimeError("token endpoint unreachable")

    monkeypatch.setattr(databricks_token, "_sdk_bearer", _boom)
    rc = databricks_token.main(["--host", "https://example.databricks.com", "--profile", "sp"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_the_sdk_entrypoint_hints_on_stderr_when_the_sdk_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base install without the ``databricks`` extra can't mint M2M; the
    entrypoint stays silent on stdout but hints on stderr so it is debuggable.
    The hint is scoped to the SDK import (not authenticate()) so an optional-dep
    ImportError is not misattributed."""
    import sys

    from omnigent.inner import databricks_token

    # Make ``import databricks.sdk.config`` raise ImportError.
    monkeypatch.setitem(sys.modules, "databricks.sdk.config", None)
    assert databricks_token.main(["--host", "https://example.databricks.com"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "databricks-sdk" in captured.err


def test_the_sdk_entrypoint_withholds_when_no_host_is_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workspace guard fails closed on an empty --host rather than releasing
    an unchecked token."""
    from omnigent.inner import databricks_token

    monkeypatch.setattr(
        databricks_token,
        "_sdk_bearer",
        lambda profile, host: ("https://example.databricks.com", "tok"),
    )
    assert databricks_token.main(["--profile", "sp"]) == 0  # no --host
    assert capsys.readouterr().out == ""


def test_profile_pinning_scrubs_the_ambient_credential_env_vars() -> None:
    """Named-profile identity guarantee: the SDK resolves env above the profile
    section, so the scrub set (built from the real SDK attribute table) must
    cover the ambient creds that would otherwise outrank a named profile — host,
    PAT, and M2M client id/secret — while keeping the config-file locator so the
    profile can still be found. This exercises the real SDK, not a mock."""
    from omnigent.inner import databricks_token

    names = databricks_token._ambient_credential_env_vars()
    assert {
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CONFIG_PROFILE",
    } <= names
    assert "DATABRICKS_CONFIG_FILE" not in names


def test_the_sdk_path_bounds_and_restores_the_network_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK metadata probe + token exchange are otherwise unbounded; bound
    them to parity with the CLI/broker mints (~15s) and restore the prior default
    so the bound doesn't leak into the caller."""
    import socket

    import databricks.sdk.config as sdk_config

    from omnigent.inner import databricks_token

    seen: dict[str, float | None] = {}

    class _StubConfig:
        host = "https://example.databricks.com"

        def __init__(self, **kwargs: object) -> None:
            pass

        def authenticate(self) -> dict[str, str]:
            seen["during"] = socket.getdefaulttimeout()
            return {"Authorization": "Bearer tok"}

    monkeypatch.setattr(sdk_config, "Config", _StubConfig)
    before = socket.getdefaulttimeout()
    databricks_token._sdk_bearer(None, "https://example.databricks.com")
    assert seen["during"] == databricks_token._SDK_NETWORK_TIMEOUT_S
    assert socket.getdefaulttimeout() == before
