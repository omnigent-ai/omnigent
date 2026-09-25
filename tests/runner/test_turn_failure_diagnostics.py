"""A failed harness turn retains searchable source dimensions after normalization."""

import json
import logging

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {"code": "synthetic_failure", "type": "ProviderError", "status": 503},
            {
                "source_code": "synthetic_failure",
                "source_type": "ProviderError",
                "source_status": "503",
            },
        ),
        (
            {"code": "private text with spaces", "type": "x" * 129, "status": True},
            {},
        ),
        (
            {"code": {"private": "payload"}, "type": ["private"], "status": None},
            {},
        ),
    ],
)
async def test_failed_turn_log_keeps_source_code_and_harness(
    caplog: pytest.LogCaptureFixture,
    metadata: dict[str, object],
    expected: dict[str, str],
) -> None:
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp-test"}}),
            _sse(
                {
                    "type": "response.failed",
                    "error": {
                        **metadata,
                        "message": "private failure detail",
                    },
                }
            ),
        ]
    )

    async def resolve(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return AgentSpec(
            spec_version=1,
            name="plain-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": "runner-test-default"}),
        )

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolve,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    session_id = "b4f6a4f0f2f74d76a2e4c0c9a8e0f9aa"
    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions",
                json={
                    "session_id": session_id,
                    "agent_id": "965906f5d9fb596610dda599a80faaee",
                },
            )
            assert created.status_code == 201
            response = await client.post(
                f"/v1/sessions/{session_id}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "965906f5d9fb596610dda599a80faaee",
                    "model": "plain-agent",
                    "content": [{"type": "input_text", "text": "private prompt"}],
                    "harness": "runner-test-default",
                },
            )
            assert response.status_code == 200

    records = [
        record
        for record in caplog.records
        if record.name == "omnigent.runner.app"
        and record.getMessage().startswith("turn surfaced to UI as failed")
    ]
    assert len(records) == 1
    row = record_to_row(records[0], source="runner")
    assert row["session_id"] == session_id
    assert row["event_name"] == "runner_turn_failed"
    attrs = row["attributes"]
    assert attrs["harness"] == "runner-test-default"
    assert {key: value for key, value in attrs.items() if key.startswith("source_")} == expected
    assert "private prompt" not in json.dumps(attrs)
    assert "private failure detail" not in json.dumps(attrs)
