"""Host-side import: read faults are classified and skipped harnesses reported."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.host.frames import HostImportLocalFrame
from omnigent.session_import import local as local_module
from omnigent.session_import.models import SessionImportNotFoundError
from tests.server.import_tunnel_harness import (
    RecordingWs,
    local_session,
    make_host,
    serve_local_sessions,
)


def _rec(kind: str, payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {"timestamp": "2026-10-03T00:00:00.000Z", "type": kind, "payload": payload}
    ).encode()


def _codex_lines(session_id: str) -> list[bytes]:
    lines = [_rec("session_meta", {"id": session_id, "cwd": "/repo", "source": "cli"})]
    lines.append(
        _rec(
            "response_item",
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "p0"}]},
        )
    )
    return lines


class TestHostReadFaults:
    """Host-side import read faults."""

    async def test_host_reports_failed_harness_for_single_source(self) -> None:
        """Host reports a broken single-source listing as a done error."""
        with patch.object(
            local_module,
            "list_recent_local_session_ids",
            side_effect=RuntimeError("Codex sessions could not be listed on this machine."),
        ):
            ws = RecordingWs()
            await make_host()._handle_import_local(
                ws.as_ws(), HostImportLocalFrame(request_id="r", source="codex", limit=5)
            )

        # The host sends a done frame with an error reason.
        sent_text = ws.sent[0]
        frame = json.loads(sent_text)
        assert frame["kind"] == "host.import_local_done"
        assert frame["status"] == "failed"
        # error can be a dict or string; check that the message is preserved
        error_msg = (
            frame["error"]["reason"] if isinstance(frame["error"], dict) else frame["error"]
        )
        assert "Codex sessions could not be listed on this machine." in error_msg

    async def test_not_installed_harness_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A harness that isn't installed is not reported as an error."""
        serve_local_sessions(monkeypatch, {"s1": local_session("s1")})
        real = local_module._recent_local_sessions_with_recency

        def _listing(source: str, *, limit: int) -> list[tuple[str, float]]:
            if source == "kimi":
                raise SessionImportNotFoundError("not installed")
            return real(source, limit=limit)  # type: ignore[arg-type]

        with patch.object(local_module, "_recent_local_sessions_with_recency", _listing):
            ws = RecordingWs()
            await make_host()._handle_import_local(
                ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
            )

        # The host only sends the working session and done, not an error.
        kinds = [json.loads(text)["kind"] for text in ws.sent]
        assert "host.import_local_session" in kinds
        assert "host.import_local_done" in kinds
        # No error frame.
        for text in ws.sent:
            frame = json.loads(text)
            if frame["kind"] == "host.import_local_session":
                assert "error" not in frame
