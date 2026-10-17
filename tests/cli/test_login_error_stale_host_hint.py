"""Login and credential failures must not carry the stale-host recovery hint."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import SimpleNamespace

import click
import httpx
import pytest

from omnigent import cli as cli_module
from omnigent.cli_common import AuthFailure
from omnigent.cli_diagnostics import suppresses_recovery_hint


def _run_main_with_failure(
    monkeypatch: pytest.MonkeyPatch,
    exc: click.ClickException,
    argv: list[str] | None = None,
) -> int:
    """Run ``main()`` for *argv* (default `omnigent host`) raising *exc*."""

    def raising_cli(*, args: list[str], standalone_mode: bool = True) -> None:
        raise exc

    monkeypatch.setattr("omnigent.cli_diagnostics.setup_cli_logging", lambda argv: None)
    monkeypatch.setattr(cli_module, "_maybe_fast_backfill_install_ledger", lambda argv: None)
    monkeypatch.setattr(cli_module, "cli", raising_cli)
    monkeypatch.setattr(sys, "argv", argv or ["omnigent", "host", "--server", "https://x"])
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()
    code = exc_info.value.code
    return code if isinstance(code, int) else 1


def test_auth_failure_omits_stale_host_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A login-time rejection is an access failure, not a tunnel rejection."""
    exc = AuthFailure(
        "https://ws.example accepted the login, but the app rejected the "
        "token (HTTP 403). Check that your user has access to this app."
    )
    assert isinstance(exc, click.ClickException)
    assert suppresses_recovery_hint(exc)
    code = _run_main_with_failure(monkeypatch, exc)

    err = capsys.readouterr().err
    assert code == 1
    assert "rejected the token (HTTP 403)" in err
    assert "runner tunnel rejection" not in err, (
        f"auth failure must not print the stale-host recovery hint:\n{err}"
    )


def test_generic_host_error_keeps_stale_host_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-auth `host` errors keep the stale-host recovery hint."""
    code = _run_main_with_failure(
        monkeypatch, click.ClickException("runner tunnel handshake failed")
    )

    err = capsys.readouterr().err
    assert code == 1
    assert "runner tunnel rejection (HTTP 401)" in err
    assert "stale host processes" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["omnigent", "login", "https://x"],
        ["omnigent", "--profiling", "login", "https://x"],
    ],
    ids=["login", "profiling-login"],
)
def test_login_command_never_gets_stale_host_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """`omnigent login` errors are auth-flow errors; no hint, even behind group options."""
    code = _run_main_with_failure(
        monkeypatch,
        click.ClickException("Could not reach https://x/v1/me: timeout"),
        argv=argv,
    )

    err = capsys.readouterr().err
    assert code == 1
    assert "Could not reach" in err
    assert "runner tunnel rejection" not in err


SERVER = "https://srv.example"
WORKSPACE = "https://ws.example"


def _auth_info(*_args: object, **_kwargs: object) -> cli_module._DatabricksWorkspaceAuthInfo:
    return cli_module._DatabricksWorkspaceAuthInfo(token="workspace-token", profile_name="ws")


def _unreachable(*_args: object, **_kwargs: object) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def _sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed", lambda: False
    )


def _token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed", lambda: True
    )
    monkeypatch.setattr(cli_module, "_databricks_workspace_auth_info", _auth_info)
    monkeypatch.setattr(cli_module, "_login_and_mint_workspace_auth_info", _auth_info)
    monkeypatch.setattr(
        cli_module, "_verify_databricks_server_token", lambda *a, **k: httpx.Response(403)
    )


def _no_token_after_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_run_databricks_browser_login", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "_databricks_workspace_auth_info", lambda *a, **k: None)


def _cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: None)


def _cli_login_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: "/usr/bin/databricks")
    monkeypatch.setattr(
        cli_module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1)
    )


def _verify_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "get", _unreachable)


def _headless_not_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **kwargs: {})
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(401))
    monkeypatch.setattr(
        cli_module, "_databricks_workspace_login_target", lambda *a, **k: WORKSPACE
    )
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda server: None)
    monkeypatch.setattr(cli_module, "_databricks_workspace_auth_info", lambda *a, **k: None)


@pytest.mark.parametrize(
    ("prepare", "call"),
    [
        (_sdk_missing, lambda: cli_module._databricks_login(SERVER, WORKSPACE, "123")),
        (_token_rejected, lambda: cli_module._databricks_login(SERVER, WORKSPACE, "123")),
        (_no_token_after_login, lambda: cli_module._login_and_mint_workspace_auth_info(WORKSPACE)),
        (_cli_missing, lambda: cli_module._run_databricks_browser_login(WORKSPACE)),
        (_cli_login_failed, lambda: cli_module._run_databricks_browser_login(WORKSPACE)),
        (_verify_unreachable, lambda: cli_module._verify_databricks_server_token(SERVER, "tok")),
        (
            _headless_not_signed_in,
            lambda: cli_module._ensure_databricks_server_auth(SERVER, non_interactive=True),
        ),
    ],
    ids=[
        "sdk-missing",
        "token-rejected",
        "no-token-after-login",
        "cli-missing",
        "cli-login-failed",
        "verify-unreachable",
        "headless-not-signed-in",
    ],
)
def test_login_flow_failures_are_auth_failures(
    monkeypatch: pytest.MonkeyPatch,
    prepare: Callable[[pytest.MonkeyPatch], None],
    call: Callable[[], object],
) -> None:
    """Every production login/verify raise site classifies its failure as AuthFailure."""
    prepare(monkeypatch)
    with pytest.raises(AuthFailure) as exc_info:
        call()
    assert suppresses_recovery_hint(exc_info.value)
