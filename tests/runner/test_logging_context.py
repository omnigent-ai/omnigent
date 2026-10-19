"""Runner debug rows retain the child session, including non-raising init failures."""

import errno
import logging

import httpx
import pytest

from omnigent import debug_logging as dl
from omnigent.runner import create_runner_app
from omnigent.runner.app_support import _client_safe_error_detail
from tests.debug_log_helpers import capture_debug_rows
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["agent", None])
async def test_runner_request_overrides_primary_and_logs_failed_init(
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str | None,
) -> None:
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "parent")
    monkeypatch.setenv(dl.RUNNER_ID_ENV_VAR, "runner_parent")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    @app.get("/v1/sessions/{session_id}/logging-probe")
    async def probe(session_id: str) -> dict[str, str]:
        logging.getLogger("omnigent.runner.test").info("child probe")
        _client_safe_error_detail(RuntimeError("synthetic failure"), context="handled probe")
        return {"id": session_id}

    with capture_debug_rows("runner") as rows:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            response = await client.post(
                "/v1/sessions", json={"session_id": "child", "agent_id": agent_id}
            )
            assert response.status_code == 501
            probe_response = await client.get("/v1/sessions/child/logging-probe")
            assert probe_response.status_code == 200
        logging.getLogger("omnigent.runner.test").info("process probe")
    failure = next(row for row in rows if row["event_name"] == "runner_session_init_failed")
    assert failure["session_id"] == "child"
    assert failure["attributes"]["runner_id"] == "runner_parent"
    assert failure["attributes"]["status_code"] == "501"
    assert failure["attributes"]["error_code"] == "not_implemented"
    assert next(row for row in rows if row["message"] == "child probe")["session_id"] == "child"
    assert (
        next(row for row in rows if row["message"].startswith("handled probe failed:"))[
            "session_id"
        ]
        == "child"
    )
    assert next(row for row in rows if row["message"] == "process probe")["session_id"] == "parent"
    assert not any(row["event_name"] == "runner_session_initialized" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped_os_error", [False, True])
async def test_unhandled_request_failure_carries_safe_correlated_context(
    monkeypatch: pytest.MonkeyPatch, wrapped_os_error: bool
) -> None:
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "parent")
    monkeypatch.setenv(dl.RUNNER_ID_ENV_VAR, "runner_parent")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    @app.get("/v1/sessions/{conversation_id}/failure-probe")
    async def failure_probe(conversation_id: str) -> None:
        if wrapped_os_error:
            try:
                raise FileNotFoundError(errno.ENOENT, "private-runner-data", "/private/file")
            except OSError as cause:
                raise RuntimeError("private-runner-data") from cause
        raise RuntimeError("private-runner-data")

    with capture_debug_rows("runner") as rows:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://runner",
        ) as client:
            response = await client.get("/v1/sessions/child/failure-probe?private=query")
        logging.getLogger("omnigent.runner.test").info("after failure probe")

    assert response.status_code == 500
    context = response.json()["failure_context"]
    assert context["failure_id"].startswith("err_")
    assert context["failure_source"] == "runner_request"
    assert context["runner_request_exception_type"] == "RuntimeError"
    assert "failure_probe" in context["runner_request_origin"]
    assert context["runner_request_route"] == "/v1/sessions/{conversation_id}/failure-probe"
    if wrapped_os_error:
        assert context["runner_request_cause_type"] == "FileNotFoundError"
        assert context["runner_request_os_errno"] == errno.ENOENT
    else:
        assert "runner_request_cause_type" not in context
        assert "runner_request_os_errno" not in context
    assert "private-runner-data" not in response.text
    assert "/private/file" not in response.text
    assert "private=query" not in response.text
    [failure] = [row for row in rows if row["event_name"] == "runner_request_failed"]
    assert failure["session_id"] == "child"
    assert failure["attributes"]["runner_id"] == "runner_parent"
    assert failure["attributes"]["failure_id"] == context["failure_id"]
    assert failure["attributes"]["runner_request_exception_type"] == "RuntimeError"
    assert (
        next(row for row in rows if row["message"] == "after failure probe")["session_id"]
        == "parent"
    )
