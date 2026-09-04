"""Tests for the health lines ``omnigent diagnose`` prints."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from omnigent import cli as cli_module


def _invoke(monkeypatch: pytest.MonkeyPatch, health: dict[str, object]) -> str:
    snap = {
        "cli_version": "9.9.9",
        "server_version": None,
        "server_url": None,
        "auth_source": "header",
        "auth_source_origin": "local-env",
        "os": "test-os",
        "python": "3.12.0",
        "health": health,
    }
    monkeypatch.setattr("omnigent.diagnostics.collect_snapshot", lambda **_kw: snap)
    result = CliRunner().invoke(cli_module.cli, ["diagnose"])
    assert result.exit_code == 0, result.output
    return result.output


def test_credential_line_reports_a_count_not_a_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(
        monkeypatch,
        {
            "credential": {
                "backend": "databricks",
                "profiles_matching_host": 2,
                "probe": "ok",
            },
            "host_log": None,
            "runner_log": None,
        },
    )
    assert "cred    2 profile(s) match the host" in out
    # Two matches is not itself a failure, so no failure word may appear.
    assert "ambiguous" not in out
    assert "unauthenticated" not in out


def test_credential_line_omitted_for_a_non_databricks_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _invoke(
        monkeypatch,
        {
            "credential": {
                "backend": "other",
                "profiles_matching_host": None,
                "probe": "not-applicable",
            },
            "host_log": None,
            "runner_log": None,
        },
    )
    assert "cred" not in out


def test_host_line_never_claims_the_tunnel_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(
        monkeypatch,
        {
            "credential": None,
            "host_log": {
                "size_bytes": 498,
                "window_bytes": 498,
                "idle_seconds": 900,
                "stalled_on_connect": False,
                "service_restarts": 3,
            },
            "runner_log": None,
        },
    )
    assert "host    idle 900s, 3 server-initiated restart(s)" in out
    # A log that does not end on the connect line proves nothing about the tunnel.
    assert "connected" not in out


def test_host_line_pairs_stalled_with_the_idle_time(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(
        monkeypatch,
        {
            "credential": None,
            "host_log": {
                "size_bytes": 498,
                "window_bytes": 498,
                "idle_seconds": 1200,
                "stalled_on_connect": True,
                "service_restarts": 0,
            },
            "runner_log": None,
        },
    )
    assert "idle 1200s" in out
    assert "last line is the connect attempt" in out


def test_runner_line_states_the_window_for_its_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(
        monkeypatch,
        {
            "credential": None,
            "host_log": None,
            "runner_log": {
                "size_bytes": 3_500_000,
                "window_bytes": 272 * 1024,
                "idle_seconds": 5,
                "bearer_handoff": True,
                "bearer_rejected": 0,
                "sdk_fallback": 0,
                "refresh_empty": 216,
            },
        },
    )
    assert "runner  idle 5s, bearer from host" in out
    assert "216 empty refresh(es)" in out
    assert "in the last 272 KiB" in out  # the counts are windowed, not lifetime


def test_missing_health_section_prints_nothing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _invoke(monkeypatch, {})
    for prefix in ("cred", "host  ", "runner"):
        assert prefix not in out


def test_json_output_carries_the_health_section(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = {
        "cli_version": "9.9.9",
        "server_version": None,
        "server_url": None,
        "auth_source": "header",
        "auth_source_origin": "local-env",
        "os": "test-os",
        "python": "3.12.0",
        "health": {"credential": None, "host_log": None, "runner_log": None},
    }
    monkeypatch.setattr("omnigent.diagnostics.collect_snapshot", lambda **_kw: snap)
    result = CliRunner().invoke(cli_module.cli, ["diagnose", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["health"] == snap["health"]
