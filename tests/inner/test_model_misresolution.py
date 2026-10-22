"""Databricks profile-to-executor checks using a local HTTP catalog and mocked SDK startup."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor


class _FakeWorkspace(BaseHTTPRequestHandler):
    """A local HTTP server answering the two real Databricks listings."""

    def do_GET(self) -> None:
        if self.path.startswith("/api/2.1/unity-catalog/model-services"):
            body = json.dumps(
                {
                    "model_services": [
                        {"name": "model-services/system.ai.claude-sonnet-4-6"},
                        {"name": "model-services/system.ai.claude-opus-4-8"},
                        {"name": "model-services/system.ai.claude-haiku-4-5"},
                    ]
                }
            ).encode()
            self.send_response(200)
        elif self.path.startswith("/ai-gateway/anthropic/v1/models"):
            body = json.dumps(
                {
                    "data": [
                        {"id": "databricks-claude-sonnet-4-6"},
                        {"id": "databricks-claude-opus-4-8"},
                        {"id": "databricks-claude-haiku-4-5"},
                    ]
                }
            ).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture()
def fake_databricks_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[HTTPServer]:
    """Resolve a temporary profile against a local HTTP catalog."""
    server = HTTPServer(("127.0.0.1", 0), _FakeWorkspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"http://127.0.0.1:{server.server_address[1]}"
    cfg = tmp_path / "databrickscfg"
    cfg.write_text(f"[repro]\nhost = {host}\ntoken = dapi-fake-token\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    # Keep ambient credentials and proxies from redirecting the local requests.
    for ambient in (
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_ACCOUNT_ID",
        "DATABRICKS_AUTH_TYPE",
    ):
        monkeypatch.delenv(ambient, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class _StopAfterModelCapture(Exception):
    """Stop before starting the Claude SDK process."""


@pytest.mark.parametrize("selected", [None, "databricks-claude-sonnet-4-6"])
async def test_databricks_profile_model_selection(
    fake_databricks_workspace: HTTPServer,
    caplog: pytest.LogCaptureFixture,
    selected: str | None,
) -> None:
    executor = ClaudeSDKExecutor(gateway=True, databricks_profile="repro", model=selected)
    captured: list[str | None] = []

    async def capture_model(sdk: Any, *, session_key: Any, options: Any, model: Any) -> Any:
        captured.append(model)
        raise _StopAfterModelCapture()

    caplog.set_level(logging.WARNING, logger="omnigent.inner.claude_sdk_executor")
    try:
        with patch.object(executor, "_get_or_create_client", side_effect=capture_model):
            for _ in range(2):
                with pytest.raises(_StopAfterModelCapture):
                    async for _ in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "model-test"}], [], ""
                    ):
                        pass
    finally:
        await executor.close()

    expected = selected or "databricks-claude-opus-4-8"
    assert captured == [expected, expected]
    substitutions = [
        record.getMessage()
        for record in caplog.records
        if "no model pinned" in record.getMessage()
    ]
    if selected is None:
        assert len(substitutions) == 1
        assert expected in substitutions[0]
    else:
        assert substitutions == []
