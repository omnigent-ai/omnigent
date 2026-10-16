"""Real server schema rejections reach the forwarder's failed status as bounded evidence."""

import json
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import forwarder
from tests._helpers.live_server import isolated_local_server
from tests._helpers.native_session import create_native_session


@pytest.mark.asyncio
async def test_real_server_schema_rejection_reaches_failed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript = bridge_dir / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "poison-record",
                "isSidechain": False,
                "message": {"role": "user", "content": "private transcript input"},
            }
        )
        + "\n"
    )
    requests: list[dict] = []
    rejected: list[httpx.Response] = []

    async def capture(request: httpx.Request) -> None:
        if request.method == "POST":
            requests.append(json.loads(request.content))

    async def poison(client: httpx.AsyncClient, *, session_id: str, item: object) -> None:
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_conversation_item", "data": "private transcript input"},
        )
        rejected.append(response)
        response.raise_for_status()

    monkeypatch.setattr(forwarder, "_post_external_conversation_item", poison)
    with isolated_local_server(tmp_path / "server") as base_url:
        with httpx.Client(trust_env=False) as setup:
            session_id = create_native_session(setup, base_url, harness="claude")["session_id"]
        async with httpx.AsyncClient(
            base_url=base_url, trust_env=False, event_hooks={"request": [capture]}
        ) as client:
            state = forwarder.TranscriptForwardState(
                transcript_path=transcript,
                line_cursor=0,
                byte_offset=0,
                cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript, 0),
            )
            tracker = forwarder._PostRetryTracker(
                max_permanent_attempts=2, base_delay_s=0, max_delay_s=0
            )
            dedupe = forwarder._ForwardDedupeState()
            for _ in range(2):
                state = await forwarder._forward_available_items(
                    client=client,
                    session_id=session_id,
                    bridge_dir=bridge_dir,
                    agent_name="claude-native-ui",
                    state=state,
                    retry_tracker=tracker,
                    dedupe=dedupe,
                )
            snapshot = await client.get(f"/v1/sessions/{session_id}")
            snapshot.raise_for_status()

        assert [r.status_code for r in rejected] == [422, 422]
        assert requests[-1]["type"] == "external_session_status"
        context = requests[-1]["data"]["failure_context"]
        assert context["http_status"] == 422
        assert context["forwarder_http_request_id"] == rejected[-1].headers["x-request-id"]
        assert context["forwarder_validation_error_count"] == len(rejected[-1].json()["detail"])
        summary = json.loads(context["forwarder_validation_errors"])
        assert any(e["type"] == "dict_type" and e["loc"][-1] == "data" for e in summary)
        assert "private transcript input" not in json.dumps(context)
        assert snapshot.json()["status"] == "failed"
        assert snapshot.json()["last_task_error"]["code"] == "native_turn_error"
        assert state.byte_offset == transcript.stat().st_size
