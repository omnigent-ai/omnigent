"""Server-side import: host read faults are classified, kept, and not retryable."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import httpx

from omnigent.session_import import local as local_module
from omnigent.session_import.errors import ImportErrorCode, import_code_is_retryable
from tests.server.import_tunnel_harness import (
    HOST_ID,
    FakeConversationStore,
    TunnelPair,
    client,
    host_record,
    imports_app,
    ndjson,
)


class TestHostReadFailure:
    """Host read failures are classified as host_read_failed, kept, and not retryable."""

    def _post(self, path: str, store: FakeConversationStore, **body: Any) -> httpx.Response:
        async def scenario() -> httpx.Response:
            pair = TunnelPair()
            app = imports_app(store, host_registry=pair.registry, host=host_record())
            async with pair:
                async with client(app) as http:
                    return await http.post(path, json={"host_id": f"host_{HOST_ID}", **body})

        return asyncio.run(scenario())

    def test_stream_error_keeps_the_hosts_message(self) -> None:
        """Stream endpoint keeps the host's error message when listing fails."""
        store = FakeConversationStore()
        with patch.object(
            local_module,
            "list_recent_local_session_ids",
            side_effect=RuntimeError("Codex sessions could not be listed on this machine."),
        ):
            response = self._post("/v1/imports/local/stream", store, source="codex", limit=5)
        assert response.status_code == 200, response.text
        events = ndjson(response)
        (error,) = [e for e in events if e["event"] == "error"]
        assert error["code"] == "host_read_failed"
        assert error["retryable"] is False
        assert import_code_is_retryable(ImportErrorCode.HOST_READ_FAILED) is False
        assert error["message"] == "Codex sessions could not be listed on this machine."
        assert "stopped unexpectedly" not in json.dumps(events)
        assert events[-1]["event"] == "done" and events[-1]["complete"] is False

    def test_buffered_error_keeps_the_hosts_message(self) -> None:
        """Buffered endpoint keeps the host's error message when listing fails."""
        store = FakeConversationStore()
        with patch.object(
            local_module,
            "list_recent_local_session_ids",
            side_effect=RuntimeError("Codex sessions could not be listed on this machine."),
        ):
            response = self._post("/v1/imports/local", store, source="codex", limit=5)
        assert response.status_code >= 400, response.text
        body = response.json()["error"]
        assert body["import_code"] == "host_read_failed"
        assert body["retryable"] is False
        assert body["message"] == "Codex sessions could not be listed on this machine."
