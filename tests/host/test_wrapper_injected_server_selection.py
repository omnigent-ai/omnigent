"""A wrapper-injected managed ``--server`` must not veto an explicit ``--all``.

Deployments that wrap the CLI (e.g. ``isaac omni``) rewrite every invocation to
add their managed ``--server``. When the user also passes ``--all``, the
conflict is injection, not intent: record selection must let ``--all`` win
instead of dying on the ``--server``/``--all`` mutual-exclusion guard. Direct
(unwrapped) invocations keep the guard: passing both flags by hand stays an
error. The ``host status`` command itself is covered beside the other host CLI
tests in ``test_cli_host.py``.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import click
import pytest

from omnigent import cli as cli_module
from omnigent.cli import _selected_daemon_records

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


def test_wrapper_managed_deployment_requires_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the enforcement flag marks a wrapper-managed deployment."""
    from omnigent.cli import _wrapper_managed_deployment

    _clear_wrapper_env(monkeypatch)
    assert not _wrapper_managed_deployment()

    # A configured command spells hints but does not block direct calls.
    monkeypatch.setenv("OMNIGENT_WRAPPER_COMMAND", "isaac omni")
    assert not _wrapper_managed_deployment()

    monkeypatch.setenv("OMNIGENT_REQUIRE_WRAPPER", "1")
    assert _wrapper_managed_deployment()

    monkeypatch.delenv("OMNIGENT_WRAPPER_COMMAND")
    assert _wrapper_managed_deployment()


def test_all_wins_over_injected_server_under_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    assert "Ignoring --server: --all selects every known target." in capsys.readouterr().err, (
        "overriding a --server must be reported on stderr, not silently applied"
    )


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="no-markers"),
        pytest.param({"OMNIGENT_WRAPPER_COMMAND": "isaac omni"}, id="command-only"),
    ],
)
def test_direct_invocation_keeps_server_all_conflict_error(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    """Without wrapper enforcement, passing both flags by hand is still rejected."""
    _clear_wrapper_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(click.ClickException, match="not both"):
        _selected_daemon_records(
            server=_MANAGED_SERVER,
            all_targets=True,
            default_all=True,
        )
