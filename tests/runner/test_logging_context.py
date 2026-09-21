"""Runner debug rows retain the child session, including non-raising init failures."""

import logging

import httpx
import pytest

from omnigent import debug_logging as dl
from omnigent.runner import create_runner_app
from tests.debug_log_helpers import capture_debug_rows
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_runner_request_overrides_primary_and_logs_failed_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "parent")
    monkeypatch.setenv(dl.RUNNER_ID_ENV_VAR, "runner_parent")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    @app.get("/v1/sessions/{session_id}/logging-probe")
    async def probe(session_id: str) -> dict[str, str]:
        logging.getLogger("omnigent.runner.test").info("child probe")
        return {"id": session_id}

    with capture_debug_rows("runner") as rows:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            response = await client.post(
                "/v1/sessions", json={"session_id": "child", "agent_id": "agent"}
            )
            assert response.status_code == 501
            probe_response = await client.get("/v1/sessions/child/logging-probe")
            assert probe_response.status_code == 200
        logging.getLogger("omnigent.runner.test").info("process probe")
    failure = next(row for row in rows if row["event_name"] == "runner_session_init_failed")
    assert failure["session_id"] == "child"
    assert failure["attributes"]["runner_id"] == "runner_parent"
    assert failure["attributes"]["status_code"] == "501"
    assert next(row for row in rows if row["message"] == "child probe")["session_id"] == "child"
    assert next(row for row in rows if row["message"] == "process probe")["session_id"] == "parent"
    assert not any(row["event_name"] == "runner_session_initialized" for row in rows)
