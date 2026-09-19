"""A wrapper-injected managed ``--server`` must not veto an explicit ``--all``.

Deployments that wrap the CLI (e.g. ``isaac omni``) rewrite every invocation to
add their managed ``--server``. When the user also passes ``--all``, the
conflict is injection, not intent: record selection must let ``--all`` win
instead of dying on the ``--server``/``--all`` mutual-exclusion guard. Direct
(unwrapped) invocations keep the guard: passing both flags by hand stays an
error.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from omnigent import cli as cli_module
from omnigent.cli import _selected_daemon_records, cli

_MANAGED_SERVER = "https://managed.example.databricksapps.com"

_WRAPPER_ENV_KEYS = (
    "OMNIGENT_WRAPPER_COMMAND",
    "OMNIGENT_REQUIRE_WRAPPER",
    "OMNIGENT_WRAPPER_BYPASS",
)


def _clear_wrapper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ambient wrapper markers so the test controls the deployment shape."""
    for key in _WRAPPER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _fake_record(target: str) -> cli_module._HostDaemonRecord:
    """A registry record for *target*, e.g. ``"https://a.example.com"``."""
    return cli_module._HostDaemonRecord(
        pid=4242,
        target=target,
        mode="server",
        server_url=target,
        log_path=None,
        started_at=int(time.time()),
        config_sig="test-config-signature",
    )


def test_wrapper_managed_deployment_reads_wrapper_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either wrapper marker (command name or require flag) flags the deployment."""
    from omnigent.cli import _wrapper_managed_deployment

    _clear_wrapper_env(monkeypatch)
    assert not _wrapper_managed_deployment()

    monkeypatch.setenv("OMNIGENT_WRAPPER_COMMAND", "isaac omni")
    assert _wrapper_managed_deployment()

    monkeypatch.delenv("OMNIGENT_WRAPPER_COMMAND")
    monkeypatch.setenv("OMNIGENT_REQUIRE_WRAPPER", "1")
    assert _wrapper_managed_deployment()


def test_all_wins_over_injected_server_under_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a wrapper, ``--all`` + injected ``--server`` selects every record."""
    _clear_wrapper_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_WRAPPER_COMMAND", "isaac omni")
    monkeypatch.setenv("OMNIGENT_REQUIRE_WRAPPER", "1")
    monkeypatch.setenv("OMNIGENT_WRAPPER_BYPASS", "1")
    records = [_fake_record("https://a.example.com"), _fake_record("https://b.example.com")]

    with patch("omnigent.cli._list_daemon_records", lambda: list(records)):
        selected = _selected_daemon_records(
            server=_MANAGED_SERVER,
            all_targets=True,
            default_all=True,
        )

    assert selected == records, (
        "the wrapper-injected --server must not narrow or veto --all selection"
    )


def test_direct_invocation_keeps_server_all_conflict_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a wrapper, passing both flags by hand is still rejected."""
    _clear_wrapper_env(monkeypatch)

    with pytest.raises(click.ClickException, match="not both"):
        _selected_daemon_records(
            server=_MANAGED_SERVER,
            all_targets=True,
            default_all=True,
        )


def test_host_status_all_with_injected_server_renders_under_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full ``host status --all --server <url>`` command succeeds when wrapped."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.cli._HOST_PID_PATH", tmp_path / "host.pid")
    _clear_wrapper_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_WRAPPER_COMMAND", "isaac omni")
    monkeypatch.setenv("OMNIGENT_REQUIRE_WRAPPER", "1")
    monkeypatch.setenv("OMNIGENT_WRAPPER_BYPASS", "1")

    with patch("omnigent.cli._list_daemon_records", list):
        result = CliRunner().invoke(
            cli,
            ["host", "status", "--all", "--server", _MANAGED_SERVER],
        )

    assert result.exit_code == 0, (
        f"wrapped `host status --all --server <url>` must reach the status "
        f"operation, got exit {result.exit_code}. Output: {result.output}"
    )
    assert "No host daemons found." in result.output, (
        f"expected the all-targets listing for an empty registry, got: {result.output}"
    )
