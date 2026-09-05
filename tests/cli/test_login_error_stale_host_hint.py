"""Auth failures must not carry the stale-host recovery hint.

The top-level CLI handler appends "If this is a runner tunnel rejection
(HTTP 401), stale host processes may be the cause. Run `omnigent stop` ..."
to command errors, because stale host processes can hold invalid server
credentials. Login/token failures (e.g. a Databricks mount rejecting the
freshly minted token with HTTP 403) are access problems, not stale-host
problems, so they must not print that hint.
"""

from __future__ import annotations

import sys

import click
import pytest

from omnigent import cli as cli_module
from omnigent.cli_common import AuthFailure


def _run_main_with_failure(
    monkeypatch: pytest.MonkeyPatch,
    exc: click.ClickException,
    argv: list[str] | None = None,
) -> int:
    """Run ``main()`` for *argv* (default `omnigent host`) raising *exc*."""

    def raising_cli(*, args: list[str], standalone_mode: bool = True) -> None:
        raise exc

    monkeypatch.setattr(cli_module, "cli", raising_cli)
    monkeypatch.setattr(
        sys, "argv", argv or ["omnigent", "host", "--server", "https://x"]
    )
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()
    code = exc_info.value.code
    return code if isinstance(code, int) else 1


def test_auth_failure_omits_stale_host_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A login-time rejection is an access failure, not a tunnel rejection."""
    code = _run_main_with_failure(
        monkeypatch,
        AuthFailure(
            "https://ws.example accepted the login, but the app rejected the "
            "token (HTTP 403). Check that your user has access to this app."
        ),
    )

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


def test_login_command_never_gets_stale_host_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`omnigent login` errors are auth-flow errors; no stale-host hint."""
    code = _run_main_with_failure(
        monkeypatch,
        click.ClickException("Could not reach https://x/v1/me: timeout"),
        argv=["omnigent", "login", "https://x"],
    )

    err = capsys.readouterr().err
    assert code == 1
    assert "Could not reach" in err
    assert "runner tunnel rejection" not in err


def test_auth_failure_is_a_click_exception() -> None:
    """AuthFailure keeps ClickException semantics (show/exit-code contract)."""
    exc = AuthFailure("boom")
    assert isinstance(exc, click.ClickException)
    assert exc.exit_code == 1
