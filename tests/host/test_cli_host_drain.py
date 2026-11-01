"""``omnigent host drain`` / ``drain-status`` / ``undrain``."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from omnigent import cli as cli_module
from omnigent.cli import _HostHttpResult, cli

_SERVER = "https://omnigent.example.com"
_HOST_ID = "5d1f0b8e0d6a4f6c9a3e2b1c0d9e8f7a"


def _status(*, safe: bool, state: str = "draining", pending: int | None = 1) -> dict[str, Any]:
    return {
        "host_id": _HOST_ID,
        "state": state,
        "safe_to_restart": safe,
        "draining": state != "active",
        "generation": 1,
        "acked_generation": 1 if safe else None,
        "drain_support": "supported",
        "host_online": True,
        "reason": "test",
        "host_report": None
        if pending is None
        else {"pending_launches": pending, "running_runners": 3},
    }


class _FakeServer:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(self, **kwargs: Any) -> _HostHttpResult:
        self.calls.append((kwargs["method"], kwargs["path"]))
        body = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return _HostHttpResult(status_code=200, body=body)


@pytest.fixture()
def fake_server(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    server = _FakeServer([_status(safe=False)])
    monkeypatch.setattr(cli_module, "_host_http_json", server)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)
    return server


def test_drain_posts_and_reports_not_safe(fake_server: _FakeServer) -> None:
    result = CliRunner().invoke(cli, ["host", "drain", _HOST_ID, "--server", _SERVER])

    assert result.exit_code == 0, result.output
    assert fake_server.calls == [("POST", f"/v1/hosts/{_HOST_ID}/drain")]
    assert "NOT safe to restart" in result.output
    assert "pending:    1" in result.output


def test_drain_wait_polls_until_safe(fake_server: _FakeServer) -> None:
    fake_server.responses = [
        _status(safe=False),
        _status(safe=False),
        _status(safe=True, pending=0),
    ]

    result = CliRunner().invoke(
        cli, ["host", "drain", _HOST_ID, "--server", _SERVER, "--wait", "30", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert [method for method, _ in fake_server.calls] == ["POST", "GET", "GET"]
    assert json.loads(result.output)["safe_to_restart"] is True


def test_drain_status_exit_code_gates_on_safety(fake_server: _FakeServer) -> None:
    not_safe = CliRunner().invoke(cli, ["host", "drain-status", _HOST_ID, "--server", _SERVER])
    assert not_safe.exit_code == 1
    assert fake_server.calls[-1] == ("GET", f"/v1/hosts/{_HOST_ID}/drain")

    fake_server.responses = [_status(safe=True, state="drained", pending=0)]
    safe = CliRunner().invoke(cli, ["host", "drain-status", _HOST_ID, "--server", _SERVER])
    assert safe.exit_code == 0, safe.output
    assert "SAFE to restart" in safe.output


def test_undrain_deletes(fake_server: _FakeServer) -> None:
    fake_server.responses = [_status(safe=False, state="active", pending=None)]
    result = CliRunner().invoke(cli, ["host", "undrain", _HOST_ID, "--server", _SERVER])

    assert result.exit_code == 0, result.output
    assert fake_server.calls == [("DELETE", f"/v1/hosts/{_HOST_ID}/drain")]
    assert "unknown (host tunnel not on this replica)" in result.output


def test_drain_http_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "_host_http_json",
        lambda **_kw: _HostHttpResult(status_code=403, body={"detail": "not your host"}),
    )
    result = CliRunner().invoke(cli, ["host", "drain", _HOST_ID, "--server", _SERVER])

    assert result.exit_code != 0
    assert "not your host" in result.output


def test_drain_defaults_to_this_machines_host_id(
    fake_server: _FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.host.identity import HostIdentity

    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda: HostIdentity(host_id=_HOST_ID, name="laptop"),
    )
    result = CliRunner().invoke(cli, ["host", "drain-status", "--server", _SERVER])

    assert fake_server.calls == [("GET", f"/v1/hosts/{_HOST_ID}/drain")]
    assert result.exit_code == 1
