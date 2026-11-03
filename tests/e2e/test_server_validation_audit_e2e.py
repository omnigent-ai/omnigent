"""Real HTTP validation rejections ship bounded diagnostics through the log sink."""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.server_runner import server_runner
from tests.e2e.test_native_terminal_start_error_log_correlation_e2e import _ZeroBusCapture


@pytest.mark.timeout(90)
def test_validation_rejections_reach_the_debug_sink_without_rejected_input(tmp_path: Path) -> None:
    with (
        contextlib.closing(_ZeroBusCapture()) as capture,
        server_runner(
            tmp_path / "stack",
            server_env={
                "OMNIGENT_DEBUG_LOG_CLIENT_ID": "e2e-client",
                "OMNIGENT_DEBUG_LOG_CLIENT_SECRET": "e2e-secret",
                "OMNIGENT_DEBUG_LOG_WORKSPACE_URL": capture.base_url,
                "OMNIGENT_DEBUG_LOG_ENDPOINT": capture.insert_url,
            },
        ) as stack,
    ):
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            event = client.post(
                "/v1/sessions/validation-session/events",
                json={"type": 42, "data": "rejected-private-content"},
            )
            assert event.status_code == 422, event.text
            assert len(event.json()["detail"]) == 3
            malformed = client.post(
                "/v1/sessions",
                content="{private-json-payload",
                headers={"Content-Type": "application/json"},
            )
            assert malformed.status_code == 422, malformed.text
            assert malformed.json()["detail"][0]["type"] == "json_invalid"
            missing_logo = client.get("/v1/branding/logo/e2e-missing-variant")
            assert missing_logo.status_code == 404
            assert missing_logo.json() == {"detail": "Branding logo not found"}

        deadline = time.monotonic() + 15
        while True:
            rows = capture.rows()
            summaries = [
                row["attributes"]
                for row in rows
                if "validation_error_count" in row.get("attributes", {})
            ]
            if len(summaries) >= 2:
                break
            assert time.monotonic() < deadline, stack.log_tail()
            time.sleep(0.05)

        assert len(summaries) == 2
        by_count = {attrs["validation_error_count"]: attrs for attrs in summaries}
        assert set(by_count) == {"1", "3"}
        assert json.loads(by_count["3"]["validation_error_types"]) == [
            "dict_type",
            "list_type",
            "string_type",
        ]
        assert json.loads(by_count["1"]["validation_error_types"]) == ["json_invalid"]
        assert json.loads(by_count["1"]["validation_error_locations"]) == [["body"]]
        assert all(attrs["status"] == "422" for attrs in summaries)
        wire = json.dumps(rows)
        assert "rejected-private-content" not in wire
        assert "private-json-payload" not in wire
