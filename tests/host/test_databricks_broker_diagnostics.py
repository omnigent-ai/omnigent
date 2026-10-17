"""Malformed broker replies cannot become credentials or crash the helper."""

import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from omnigent.debug_logging import record_to_row
from omnigent.host import databricks_credential as broker


@pytest.mark.parametrize(
    "payload",
    [
        ["synthetic-private-response"],
        "synthetic-private-response",
        True,
        17,
        {"connected": "yes", "token": "t", "workspace_host": "https://workspace.example"},
        {"connected": 1, "token": "t", "workspace_host": "https://workspace.example"},
        {"connected": True, "token": "  ", "workspace_host": "https://workspace.example"},
        {"connected": True, "token": "t"},
        {"connected": True, "token": 17, "workspace_host": "https://workspace.example"},
        {"connected": True, "token": True, "workspace_host": "https://workspace.example"},
        {
            "connected": True,
            "token": {"private": "secret"},
            "workspace_host": "https://workspace.example",
        },
        {"connected": True, "token": ["t"], "workspace_host": "https://workspace.example"},
        {"connected": True, "token": "t", "workspace_host": ["https://workspace.example"]},
    ],
)
def test_invalid_response_is_declined(payload: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker.httpx, "get", lambda *a, **kw: httpx.Response(200, json=payload))
    assert (
        broker.fetch_broker_bearer("https://server.example", "host-test", "synthetic-token")
        is None
    )


@pytest.mark.parametrize("outcome", ["http_error", "transport_error", "invalid_json", "success"])
def test_outcome_is_correlated_without_recording_credentials(
    outcome: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "synthetic-private-credential"

    def get(*args: object, **kwargs: object) -> httpx.Response:
        if outcome == "transport_error":
            raise httpx.ReadTimeout(secret)
        if outcome == "invalid_json":
            return httpx.Response(200, text=secret)
        return httpx.Response(
            503 if outcome == "http_error" else 200,
            json={
                "connected": True,
                "token": secret,
                "workspace_host": "https://workspace.example",
            },
        )

    monkeypatch.setattr(broker.httpx, "get", get)
    with caplog.at_level(logging.DEBUG, logger=broker.__name__):
        result = broker.fetch_broker_bearer("https://server.example", "host-test", secret)
    assert result == (("https://workspace.example", secret) if outcome == "success" else None)
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "databricks_broker_request"
    ]
    assert len(records) == 1
    row = record_to_row(records[0], source="host")
    attrs = row["attributes"]
    assert attrs["host_id"] == "host-test"
    assert attrs["credential_scope"] == "host_inference_broker"
    assert attrs["outcome"] == outcome
    assert attrs["broker_attempt_id"]
    assert float(attrs["elapsed_ms"]) >= 0
    assert secret not in json.dumps(row)


def test_diagnostic_sink_failure_does_not_break_valid_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        broker.httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200,
            json={
                "connected": True,
                "token": "synthetic-token",
                "workspace_host": "https://workspace.example",
            },
        ),
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic-sink-failure")

    monkeypatch.setattr(broker._logger, "log", fail)
    assert broker.fetch_broker_bearer(
        "https://server.example", "host-test", "synthetic-token"
    ) == ("https://workspace.example", "synthetic-token")


@pytest.mark.parametrize("mode", ["success", "declined", "http_error", "invalid_response"])
def test_real_token_helper_keeps_stdout_as_token_protocol(mode: str, tmp_path: Path) -> None:
    payload = (
        {
            "connected": mode != "declined",
            "token": "synthetic-vended-token",
            "workspace_host": "https://workspace.example",
        }
        if mode != "invalid_response"
        else ["synthetic-private-response"]
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(503 if mode == "http_error" else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    coords = tmp_path / "coords.json"
    coords.write_text(
        json.dumps(
            {
                "server": f"http://127.0.0.1:{server.server_port}",
                "host_id": "host-test",
                "host_token": "synthetic-host-token",
                "workspace_host": "https://workspace.example",
            }
        )
    )
    # SYSTEMROOT/WINDIR keep Python subprocess startup working on Windows.
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SYSTEMROOT", "WINDIR"}
    }
    env.update(
        OMNIGENT_CONFIG_HOME=str(tmp_path / "config"),
        OMNIGENT_DATA_DIR=str(tmp_path / "data"),
        # The worktree is not necessarily pip-installed; import omnigent from it.
        PYTHONPATH=str(Path(__file__).resolve().parents[2]),
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnigent.host.databricks_credential",
                "token",
                "--coords",
                str(coords),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.returncode == 0
    assert result.stdout.strip() == ("synthetic-vended-token" if mode == "success" else "")
    assert "synthetic-host-token" not in result.stderr
    assert "synthetic-vended-token" not in result.stderr
    if mode != "success":
        diagnostic = json.loads(result.stderr.strip().splitlines()[-1])
        assert diagnostic["event_name"] == "databricks_broker_request"
        assert diagnostic["outcome"] == {"declined": "not_connected"}.get(mode, mode)
