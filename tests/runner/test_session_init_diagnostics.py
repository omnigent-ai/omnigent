"""Uncaught init failures retain correlation and bounded, content-free evidence."""

from __future__ import annotations

import asyncio
import errno
import itertools
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent import debug_logging as dl
from omnigent.entities import Conversation
from omnigent.runner import app as runner_app
from omnigent.runner.session_init_diagnostics import session_init_exception_fields
from omnigent.runner.session_init_protocol import (
    SESSION_INIT_ATTEMPT_HEADER,
    build_runner_session_init_payload,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.debug_log_helpers import capture_debug_rows
from tests.runner.conftest import _build_app_for_spec, _runner_client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "phase"),
    [
        ("context", "load_context"),
        ("policy", "agent_start_policy"),
        ("spawn_env", "spawn_environment"),
        ("harness", "harness_spawn"),
        ("terminal", "native_terminal"),
    ],
)
async def test_init_exception_has_serialized_phase_cause_and_correlation(
    monkeypatch: pytest.MonkeyPatch, target: str, phase: str
) -> None:
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "primary")
    monkeypatch.setenv(dl.RUNNER_ID_ENV_VAR, "runner_test")
    spec = AgentSpec(
        spec_version=1,
        name="test",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )
    app, manager = await _build_app_for_spec(spec)
    failure = OSError(errno.EIO, "credential-message-sentinel", "/private-path-sentinel")
    request = httpx.Request("GET", "https://example.invalid/private-url-sentinel")
    failure.__cause__ = httpx.HTTPStatusError(
        "provider-response-sentinel", request=request, response=httpx.Response(502)
    )
    if target == "context":
        monkeypatch.setattr(
            "omnigent.runner.session_init_protocol.validate_runner_inference_config",
            Mock(side_effect=failure),
        )
    elif target == "policy":
        monkeypatch.setattr(
            runner_app, "_evaluate_agent_start_gate", AsyncMock(side_effect=failure)
        )
    elif target == "spawn_env":
        monkeypatch.setattr(runner_app, "_build_spawn_env_from_spec", Mock(side_effect=failure))
    elif target == "harness":
        monkeypatch.setattr(manager, "get_client", AsyncMock(side_effect=failure))
    else:
        monkeypatch.setattr(runner_app, "_launch_native_terminal", AsyncMock(side_effect=failure))
    payload = build_runner_session_init_payload(
        Conversation(
            id="child",
            root_conversation_id="child",
            agent_id="agent",
            created_at=1,
            updated_at=1,
            labels={"private": "label-sentinel"},
        ),
        server_version="0.6.0",
    )
    with capture_debug_rows("runner") as rows:
        async with _runner_client(app) as client:
            for attempt in ("a" * 32, "b" * 32):
                failure.__traceback__ = None
                with pytest.raises(OSError) as raised:
                    await client.post(
                        "/v1/sessions",
                        json=payload,
                        headers={SESSION_INIT_ATTEMPT_HEADER: attempt},
                    )
                assert raised.value is failure
    failures = [row for row in rows if row["event_name"] == "runner_session_init_failed"]
    assert len(failures) == 2
    for row, attempt in zip(failures, ("a" * 32, "b" * 32), strict=True):
        attrs = row["attributes"]
        assert row["session_id"] == "child"
        assert attrs["runner_id"] == "runner_test"
        assert attrs["init_attempt_id"] == attempt
        assert attrs["init_phase"] == phase
        assert attrs["exception_type"] == "OSError"
        assert attrs["exception_errno"] == str(errno.EIO)
        assert attrs["exception_cause_type"] == "HTTPStatusError"
        assert attrs["exception_cause_http_status"] == "502"
        assert attrs["exception_origin_module"] == "omnigent.runner.app"
        assert attrs["exception_origin_function"] == (
            "_load_envelope_session_init_context"
            if target == "context"
            else "_initialize_session_impl"
        )
        assert int(attrs["exception_origin_line"]) > 0
        assert int(attrs["duration_ms"]) >= 0
        assert not row["stack_trace"]
        assert "sentinel" not in json.dumps(row)
    assert not any(row["event_name"] == "runner_session_initialized" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["logging_failure", "cancelled", "invalid_id", "handled"])
async def test_init_diagnostics_preserve_failure_and_handler_behavior(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    app, manager = await _build_app_for_spec(
        AgentSpec(spec_version=1, name="test", executor=ExecutorSpec(type="omnigent"))
    )
    failure = (
        asyncio.CancelledError()
        if mode == "cancelled"
        else RuntimeError("spawn failed")
        if mode == "handled"
        else OSError(errno.EIO, "private-sentinel")
    )
    monkeypatch.setattr(manager, "get_client", AsyncMock(side_effect=failure))
    if mode == "logging_failure":
        monkeypatch.setattr(
            runner_app._logger, "error", Mock(side_effect=RuntimeError("log failed"))
        )
    with capture_debug_rows("runner") as rows:
        async with _runner_client(app) as client:
            if mode == "handled":
                response = await client.post(
                    "/v1/sessions", json={"session_id": "child", "agent_id": "agent"}
                )
                assert response.status_code == 503
                assert response.json()["error"] == "harness_spawn_failed"
            else:
                with pytest.raises(type(failure)) as raised:
                    await client.post(
                        "/v1/sessions",
                        json={"session_id": "child", "agent_id": "agent"},
                        headers={SESSION_INIT_ATTEMPT_HEADER: "private-header-sentinel"},
                    )
                if mode != "cancelled":
                    assert raised.value is failure
    failures = [row for row in rows if row["event_name"] == "runner_session_init_failed"]
    if mode in {"logging_failure", "cancelled"}:
        assert failures == []
    elif mode == "invalid_id":
        assert len(failures) == 1
        assert "init_attempt_id" not in failures[0]["attributes"]
        assert "sentinel" not in json.dumps(failures)
    else:
        assert len(failures) == 1
        assert failures[0]["attributes"]["error_code"] == "harness_spawn_failed"
        assert "init_phase" not in failures[0]["attributes"]


@pytest.mark.parametrize("cycle", [False, True])
def test_exception_chain_is_bounded_and_ignores_untyped_metadata(cycle: bool) -> None:
    failures = [OSError("private-sentinel") for _ in range(6)]
    for outer, inner in itertools.pairwise(failures):
        outer.__context__ = inner
    failures[0].errno = "private-errno-sentinel"  # type: ignore[assignment]
    if cycle:
        failures[1].__context__ = failures[0]
    fields = session_init_exception_fields(failures[0])
    assert fields["exception_chain_truncated"] is True
    assert "exception_cause_4_type" not in fields
    assert "exception_errno" not in fields
    assert "sentinel" not in json.dumps(fields)


@pytest.mark.asyncio
async def test_shared_init_failure_keeps_the_creating_requests_attempt_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, manager = await _build_app_for_spec(AgentSpec(spec_version=1, name="test"))
    entered, release = asyncio.Event(), asyncio.Event()
    failure = OSError(errno.EIO, "private-sentinel")

    async def fail_spawn(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()
        raise failure

    spawn = AsyncMock(side_effect=fail_spawn)
    monkeypatch.setattr(manager, "get_client", spawn)
    with capture_debug_rows("runner") as rows:
        async with _runner_client(app) as client:
            first = asyncio.create_task(
                client.post(
                    "/v1/sessions",
                    json={"session_id": "child", "agent_id": "agent"},
                    headers={SESSION_INIT_ATTEMPT_HEADER: "a" * 32},
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = asyncio.create_task(
                client.post(
                    "/v1/sessions",
                    json={"session_id": "child", "agent_id": "agent"},
                    headers={SESSION_INIT_ATTEMPT_HEADER: "b" * 32},
                )
            )
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
    assert results == [failure, failure]
    spawn.assert_awaited_once()
    [row] = [row for row in rows if row["event_name"] == "runner_session_init_failed"]
    assert row["attributes"]["init_attempt_id"] == "a" * 32
