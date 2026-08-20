"""Tests for :mod:`omnigent.onboarding.setup`.

Cover the onboarding helpers used by ``omnigent setup``: env-var hygiene
(``detect_conflicting_env_vars``), profile-host discovery
(``_existing_profile_hosts``), CLI lookup (``find_databricks_cli``), the
``maybe_run_onboarding`` skip guards, and profile-name derivation in
``login_databricks_workspace``.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from click import ClickException

from omnigent.onboarding import setup as setup_mod
from omnigent.onboarding.setup import (
    _CONFLICTING_ENV_VARS,
    SKIP_ENV_VAR,
    ProfileSpec,
    _existing_profile_hosts,
    detect_conflicting_env_vars,
    find_databricks_cli,
    login_databricks_workspace,
    maybe_run_onboarding,
)

# ── env-var hygiene ────────────────────────────────────


def test_detect_returns_set_vars_in_catalog_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog order is contract — the user-facing notice depends on it."""
    for v in _CONFLICTING_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DATABRICKS_TOKEN", "stale")
    monkeypatch.setenv("DATABRICKS_HOST", "https://old")
    assert detect_conflicting_env_vars() == ["DATABRICKS_HOST", "DATABRICKS_TOKEN"]


def test_detect_ignores_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string is not "set" — some shells use it to unset."""
    for v in _CONFLICTING_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DATABRICKS_TOKEN", "")
    assert detect_conflicting_env_vars() == []


# ── _existing_profile_hosts ────────────────────────────


class _RecordedRun:
    """Stand-in for ``subprocess.run`` that records argv and returns a canned result."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=self.returncode, stdout=self.stdout, stderr=""
        )


def test_existing_profile_hosts_parses_cli_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal payload parses; malformed entries skip without crashing."""
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: "/usr/bin/databricks")
    payload = json.dumps(
        {
            "profiles": [
                {"name": "oss", "host": "https://oss.example.com"},
                {"name": "broken"},  # missing host — must be skipped
            ]
        }
    )
    recorder = _RecordedRun(returncode=0, stdout=payload)
    monkeypatch.setattr(setup_mod.subprocess, "run", recorder)
    assert _existing_profile_hosts() == {"oss": "https://oss.example.com"}
    assert recorder.calls == [["/usr/bin/databricks", "auth", "profiles", "--output", "json"]]


def test_existing_profile_hosts_empty_when_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No CLI on PATH → no shell-out, empty mapping."""
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: None)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("subprocess.run reached despite missing CLI")

    monkeypatch.setattr(setup_mod.subprocess, "run", _explode)
    assert _existing_profile_hosts() == {}


def test_find_databricks_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trivial delegation to ``shutil.which`` — guard against regression."""
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: f"/fake/{name}")
    assert find_databricks_cli() == "/fake/databricks"


# ── maybe_run_onboarding skip paths ────────────────────


def test_maybe_run_skips_when_skip_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_SKIP_ONBOARD=1`` short-circuits before any side effects."""
    monkeypatch.setenv(SKIP_ENV_VAR, "1")
    monkeypatch.setattr(setup_mod.sys.stdin, "isatty", lambda: True)

    def _explode() -> dict[str, str]:
        raise AssertionError("CLI was consulted despite skip env var")

    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", _explode)
    maybe_run_onboarding()  # must not raise


def test_maybe_run_skips_when_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piped stdin → skip (can't safely launch interactive OAuth in CI)."""
    monkeypatch.delenv(SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(setup_mod.sys.stdin, "isatty", lambda: False)

    def _explode() -> dict[str, str]:
        raise AssertionError("CLI was consulted despite non-TTY stdin")

    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", _explode)
    maybe_run_onboarding()


# ── login_databricks_workspace ─────────────────────────


class _LoginRecorder:
    """Stand-in for ``setup._login_profile`` that records the ProfileSpec it's
    handed and reports a fixed success/failure, so tests assert what login was
    attempted without launching a real OAuth browser flow (or its 1s sleep)."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.specs: list[ProfileSpec] = []

    def __call__(self, cli: str, spec: ProfileSpec, console: Any) -> bool:
        self.specs.append(spec)
        return self.ok


def test_login_databricks_workspace_reuses_existing_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a profile already points at the workspace host, reuse it — no login.

    The OAuth token cache is host-keyed, so re-authenticating would be a
    pointless extra browser window. If this regressed to always logging in,
    ``login.specs`` would be non-empty and the returned name would be derived
    rather than the existing one.
    """
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: "/usr/bin/databricks")
    monkeypatch.setattr(
        setup_mod,
        "_existing_profile_hosts",
        lambda: {"my-team": "https://example-my-ws.cloud.databricks.com"},
    )
    login = _LoginRecorder()
    monkeypatch.setattr(setup_mod, "_login_profile", login)

    # Trailing slash on input must still match the cached host.
    profile = login_databricks_workspace("https://example-my-ws.cloud.databricks.com/")

    assert profile == "my-team"  # reused the existing profile name verbatim
    assert login.specs == []  # no `databricks auth login` was triggered


def test_login_databricks_workspace_logs_in_with_derived_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no reusable profile, it runs ``databricks auth login`` for the derived
    profile name pointed at the supplied workspace URL, and returns that name.

    A novel host with no existing or bundled match derives the profile name
    from the first DNS label. The ``spec.host == workspace_url`` assertion is
    the key one: if the login targeted a hardcoded host instead of the user's
    workspace, routing would resolve the wrong gateway and the assertion would
    fail.
    """
    workspace_url = "https://example-acme-prod.cloud.databricks.com"
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: "/usr/bin/databricks")
    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", dict)
    login = _LoginRecorder(ok=True)
    monkeypatch.setattr(setup_mod, "_login_profile", login)

    profile = login_databricks_workspace(workspace_url)

    assert profile == "example-acme-prod"  # derivation picked the first DNS label
    assert len(login.specs) == 1  # exactly one login attempt, for the one workspace
    assert login.specs[0].name == "example-acme-prod"
    assert login.specs[0].host == workspace_url


def test_login_databricks_workspace_drops_stale_same_name_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the derived name already exists but points elsewhere, drop that stale
    section before re-logging in — the CLI refuses ``--host`` against a name
    already bound to a different host, so skipping the removal would fail login.
    """
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: "/usr/bin/databricks")
    monkeypatch.setattr(
        setup_mod,
        "_existing_profile_hosts",
        lambda: {"example-acme-prod": "https://example-stale.cloud.databricks.com"},
    )
    removed: list[str] = []
    monkeypatch.setattr(
        setup_mod, "_remove_profile_section", lambda name: bool(removed.append(name))
    )
    login = _LoginRecorder(ok=True)
    monkeypatch.setattr(setup_mod, "_login_profile", login)

    profile = login_databricks_workspace("https://example-acme-prod.cloud.databricks.com")

    assert profile == "example-acme-prod"
    assert removed == ["example-acme-prod"]  # stale section dropped before the re-login
    assert login.specs[0].host == "https://example-acme-prod.cloud.databricks.com"


def test_login_databricks_workspace_raises_without_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``databricks`` CLI on PATH → a clear ClickException with install help,
    not a confusing crash deep in the login subprocess."""
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: None)
    with pytest.raises(ClickException, match=r"`databricks` CLI not on PATH"):
        login_databricks_workspace("https://example-my-ws.cloud.databricks.com")
