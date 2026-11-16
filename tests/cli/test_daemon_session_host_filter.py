"""The daemon's session lists ask for its own host."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent import cli


@pytest.mark.parametrize("connected_only", [False, True])
def test_sessions_for_daemon_sends_its_host_id_on_both_lists(
    monkeypatch: pytest.MonkeyPatch, connected_only: bool
) -> None:
    requests: list[dict[str, str | int]] = []

    def _fake_http(**kwargs: Any) -> cli._HostHttpResult:
        requests.append(kwargs["params"])
        return cli._HostHttpResult(status_code=200, body={"data": [], "has_more": False})

    monkeypatch.setattr(cli, "_host_http_json", _fake_http)
    monkeypatch.setattr(cli, "_daemon_base_url", lambda record: "https://server.example.com")
    record = cli._HostDaemonRecord(
        pid=42,
        target="https://server.example.com",
        mode="server",
        server_url="https://server.example.com",
        log_path=None,
        started_at=1,
        host_id="host_a",
    )

    result = cli._sessions_for_daemon(
        record, connected_only=connected_only, include_sub_agents=True
    )

    assert result.error is None
    assert [(r.get("kind", "default"), r["host_id"]) for r in requests] == [
        ("default", "host_a"),
        ("sub_agent", "host_a"),
    ]
