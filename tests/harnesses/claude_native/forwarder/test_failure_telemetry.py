"""Native error evidence survives hooks, transcript parsing, retries and forwarding."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge, forwarder
from omnigent.harnesses.claude_native.failure_telemetry import claude_failure_context
from omnigent.native.failure_telemetry import FailureContext


@pytest.mark.asyncio
async def test_hook_category_and_identity_survive_retry(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "native-session",
            "error": "server_error",
            "error_details": "API Error: 500 Overloaded",
            "last_assistant_message": "I am waiting for a background task.",
        },
    )
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(503 if len(requests) == 1 else 204)

    initial = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        for _ in range(2):
            # Recreate the tracker to simulate retry after a forwarder restart.
            state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_synthetic",
                bridge_dir=bridge_dir,
                state=initial,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id="resp_synthetic",
                diagnostic_health=lambda: {
                    "diagnostic_capture_state": "disabled",
                    "diagnostic_capture_enabled": False,
                },
            )
    assert state.event_cursor == 1
    assert len(requests) == 2
    first, second = [request["data"] for request in requests]
    assert first == second
    assert second["failure_context"]["native_error_category"] == "server_error"
    assert second["failure_context"]["detail_source"] == "hook_error_details"
    assert second["failure_context"]["failure_id"]
    assert second["failure_context"]["native_hook_recorded_at"] > 0
    assert second["failure_context"]["native_session_id"] == "native-session"
    assert second["failure_context"]["diagnostic_capture_enabled"] is False
    assert second["response_id"] == "resp_synthetic"
    assert second["failure_detail"] == "API Error: 500 Overloaded"
    assert "native_api_error_message" not in second["failure_context"]


@pytest.mark.asyncio
async def test_hook_telemetry_failure_does_not_block_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "native-session",
            "error": "server_error",
            "error_details": "Generation failed.",
        },
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise TypeError("synthetic-private-telemetry-data")

    monkeypatch.setattr(forwarder, "_stop_failure_context", fail)
    caplog.set_level(logging.DEBUG, logger=forwarder.__name__)
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(204)

    state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        for _ in range(2):
            state = await forwarder._forward_available_status_events(
                client=client,
                session_id="conv_synthetic",
                bridge_dir=bridge_dir,
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
                response_id="resp_synthetic",
            )
    assert state.event_cursor == 1
    assert state.byte_offset == (bridge_dir / "hooks.jsonl").stat().st_size
    assert requests == [
        {
            "type": "external_session_status",
            "data": {
                "status": "failed",
                "response_id": "resp_synthetic",
                "failure_detail": "Generation failed.",
            },
        }
    ]
    assert "Claude hook failure telemetry failed: TypeError" in caplog.text
    assert "synthetic-private-telemetry-data" not in caplog.text


@pytest.mark.parametrize("marker_location", ["entry", "message"])
@pytest.mark.parametrize("block_count", [1, 2], ids=["string", "multiple-text-blocks"])
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("retry_after_rejection", [False, True], ids=["accepted", "retried"])
@pytest.mark.asyncio
async def test_explicit_transcript_api_error_is_observed_without_failing_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    marker_location: str,
    block_count: int,
    batch: bool,
    retry_after_rejection: bool,
) -> None:
    error_text = (
        'API Error: 400 {"error":{"type":"invalid_request_error","code":"invalid_user",'
        '"param":"user","message":"User field is too long"},"request_id":"error-body-id"}'
    )
    entry = {
        "type": "assistant",
        "uuid": "native-record",
        "sessionId": "native-session",
        "requestId": "native-request",
        "request_id": "unqualified-record-request",
        "version": "2.0.0-test",
        "message": {"role": "assistant", "model": "synthetic-model", "content": error_text},
    }
    if block_count == 2:
        entry["message"]["content"] = [
            {"type": "text", "text": error_text},
            {"type": "text", "text": "Provider failed after retrying."},
        ]
    if marker_location == "entry":
        entry["isApiErrorMessage"] = True
    else:
        entry["message"]["isApiErrorMessage"] = True
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps(entry) + "\n")
    _, _, items = bridge.read_transcript_items_since(path, 0, agent_name="test-agent")
    assert len(items) == block_count
    posted = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(
            422 if retry_after_rejection and len(posted) == 1 else 202,
            json=[{"item_id": f"item-{index}"} for index in range(block_count)] if batch else {},
            headers={"x-request-id": "omnigent-event-post-id"},
        )

    caplog.set_level(logging.INFO, logger=forwarder.__name__)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:

        async def post() -> None:
            if batch:
                await forwarder._post_external_conversation_item_batch(
                    client,
                    session_id="conv_synthetic",
                    items=[forwarder._PendingSubagentItem(item=item) for item in items],
                )
            else:
                for item in items:
                    await forwarder._post_external_conversation_item(
                        client, session_id="conv_synthetic", item=item
                    )

        if retry_after_rejection:
            with pytest.raises(httpx.HTTPStatusError):
                await post()
            assert any(
                getattr(r, "event_name", None) == "native_failure_observed" for r in caplog.records
            )
        await post()
    observations = block_count
    if retry_after_rejection:
        observations += block_count if batch else 1
    events = [event for payload in posted for event in (payload if batch else [payload])]
    assert [event["type"] for event in events] == ["external_conversation_item"] * observations
    assert events[0]["data"]["item_data"]["content"][0]["text"] == error_text
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "native_failure_observed"
    ]
    assert len(records) == observations
    assert len({record.attributes["failure_id"] for record in records}) == block_count
    assert {record.attributes["native_record_id"] for record in records} == {"native-record"}
    attrs = records[0].attributes
    assert attrs["native_api_error_message"] == "True"
    assert attrs["detail_source"] == "explicit_api_error"
    assert attrs["provider_error_type"] == "invalid_request_error"
    assert attrs["provider_error_code"] == "invalid_user"
    assert attrs["provider_error_param"] == "user"
    assert attrs["native_error_message"] == "User field is too long"
    assert attrs["http_status"] == "400"
    assert attrs["native_request_id"] == "native-request"
    assert attrs["native_error_request_id"] == "error-body-id"
    assert attrs["native_record_id"] == "native-record"
    assert attrs["native_cli_version"] == "2.0.0-test"
    assert attrs["native_model"] == "synthetic-model"
    assert attrs["failure_decision"] == "observed"
    assert "provider_request_id" in attrs["failure_context_missing_fields"]
    assert "gateway_request_id" in attrs["failure_context_missing_fields"]
    assert "omnigent-event-post-id" not in str(attrs)


def test_model_prose_does_not_become_explicit_error_evidence(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "prose",
                "message": {
                    "role": "assistant",
                    "content": "API Error: 400 is an example of the response.",
                },
            }
        )
        + "\n"
    )
    _, _, items = bridge.read_transcript_items_since(path, 0, agent_name="test-agent")
    assert items[0].failure_context is None


def test_api_error_keeps_original_message_before_display_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "api-error",
                "isApiErrorMessage": True,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Prompt is too long"}],
                },
            }
        )
        + "\n"
    )
    _, _, items = bridge.read_transcript_items_since(path, 0, agent_name="test-agent")
    assert items[0].data["content"][0]["text"].startswith("Context limit reached")
    assert items[0].failure_context["native_error_message"] == "Prompt is too long"


@pytest.mark.parametrize(
    ("entry", "error_text", "expected", "absent"),
    [
        pytest.param(
            {"error": {"code": 400}},
            None,
            {"provider_error_code": "400"},
            (),
            id="numeric-provider-code",
        ),
        pytest.param(
            {"error": {"code": None, "error_code": "stream_lost"}},
            None,
            {"provider_error_code": "stream_lost"},
            (),
            id="null-code-falls-back",
        ),
        pytest.param(
            {"error": {"code": 0, "error_code": "ignored"}},
            None,
            {"provider_error_code": "0"},
            (),
            id="zero-code-is-preserved",
        ),
        pytest.param(
            {
                "requestId": "native-request",
                "request_id": "unqualified-record-request",
                "error": {"code": "stream_lost"},
            },
            None,
            {"native_request_id": "native-request", "provider_error_code": "stream_lost"},
            ("native_error_request_id", "gateway_request_id", "provider_request_id"),
            id="record-request-id-is-not-error-body-evidence",
        ),
        pytest.param(
            {
                "error": None,
                "message": {"error": {"code": "stream_lost", "message": "Lost stream"}},
            },
            None,
            {
                "provider_error_code": "stream_lost",
                "native_error_message": "Lost stream",
                "inference_detail_source": "structured_error",
            },
            ("native_error_category",),
            id="null-entry-error-preserves-message-error",
        ),
        pytest.param(
            {"isApiErrorMessage": True, "error": {"code": "stream_lost"}},
            "Connection lost mid-response",
            {
                "native_error_message": "Connection lost mid-response",
                "provider_error_code": "stream_lost",
            },
            (),
            id="partial-structured-error-retains-explicit-text",
        ),
        pytest.param(
            {"message": {"model": "<synthetic>"}},
            None,
            {},
            ("native_model",),
            id="synthetic-model-is-unavailable",
        ),
        pytest.param(
            {
                "error": {"type": "server_error", "code": "overloaded", "param": None},
                "status_code": 503,
                "gateway_request_id": "gateway-id",
                "provider_request_id": "provider-id",
            },
            None,
            {
                "http_status": 503,
                "provider_error_code": "overloaded",
                "inference_detail_source": "structured_error",
                "gateway_request_id": "gateway-id",
                "provider_request_id": "provider-id",
            },
            ("native_error_category", "provider_error_param"),
            id="structured-error-and-request-ownership",
        ),
    ],
)
def test_native_error_extraction(
    entry: dict[str, object],
    error_text: str | None,
    expected: FailureContext,
    absent: tuple[str, ...],
) -> None:
    context = claude_failure_context(entry, error_text=error_text)
    assert {key: context[key] for key in expected} == expected
    assert not (set(absent) & context.keys())


def test_hook_inference_fields_are_parsed_before_display_truncation(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    error_text = "API Error: 400 " + json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "param": "user",
                "message": "x" * 6000,
            }
        }
    )
    bridge.record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "native-session",
            "error": "invalid_request",
            "error_details": error_text,
        },
    )
    (record,) = bridge.read_hook_events_since_with_position(bridge_dir, 0).records
    assert len(record.failure_message) == 4000
    assert record.failure_context["provider_error_type"] == "invalid_request_error"
    assert record.failure_context["provider_error_param"] == "user"
    assert len(record.failure_context["native_error_message"]) == 1024
