"""Compaction tests for Codex forwarder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    codex_home_for_bridge_dir,
    write_bridge_state,
)
from omnigent.harnesses.codex_native.forwarder import _persist_codex_compaction_item
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


@pytest.mark.asyncio
async def test_compaction_status_posts_and_dedupes_consecutive() -> None:
    """
    Compaction status mirrors as external_compaction_status, deduped (#1255).

    Codex may signal completion via both a ``contextCompaction`` item and a
    ``thread/compacted`` notification; consecutive identical statuses must
    not double-post (the spinner would flicker).
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()

    await fwd._post_compaction_status(client, "conv_x", "in_progress", forwarder_state=state)
    await fwd._post_compaction_status(client, "conv_x", "completed", forwarder_state=state)
    # Duplicate completion (e.g. item then notification) is suppressed.
    await fwd._post_compaction_status(client, "conv_x", "completed", forwarder_state=state)

    assert [post[1] for post in client.posts] == [
        {"type": "external_compaction_status", "data": {"status": "in_progress"}},
        {"type": "external_compaction_status", "data": {"status": "completed"}},
    ]
    assert state.compaction_status_posted == "completed"


@pytest.mark.asyncio
async def test_completed_context_compaction_item_clears_spinner() -> None:
    """
    A completed ``contextCompaction`` item posts compaction-completed.

    It is a status edge, not transcript history, so it must clear the
    spinner without being appended as a conversation item.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState()
    state.compaction_status_posted = "in_progress"

    await fwd._handle_completed_item(
        client,
        "conv_x",
        {
            "threadId": "thread_1",
            "turnId": "turn_1",
            "item": {"type": "contextCompaction", "id": "item_c"},
        },
        forwarder_state=state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {"type": "external_compaction_status", "data": {"status": "completed"}},
        )
    ]


# ---------------------------------------------------------------------------
# _persist_codex_compaction_item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_codex_compaction_item_posts_uuid_window_id(tmp_path: Path) -> None:
    """Codex's UUID window id is posted with the compaction checkpoint."""
    import json as _json

    codex_home = codex_home_for_bridge_dir(tmp_path)
    rollout = codex_home / "sessions" / "2026" / "09" / "05" / "rollout-thread_1.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        _json.dumps(
            {
                "type": "compacted",
                "payload": {
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}],
                        }
                    ],
                    "window_id": "01a070e2-2665-7d62-9b74-973decf239b7",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_codex",
            socket_path="ws://127.0.0.1:9999",
            thread_id="thread_1",
            codex_home=str(codex_home),
            cwd="/tmp/workspace",
        ),
    )
    get_resp = MagicMock()
    get_resp.json.return_value = {"data": [{"id": "item_codex"}]}
    get_resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get = AsyncMock(return_value=get_resp)

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    await _persist_codex_compaction_item(
        client,
        session_id="conv_codex",
        bridge_dir=tmp_path,
    )

    client.post.assert_called_once()
    _url, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"] == "item_codex"
    assert "Codex" in body["data"]["summary"]
    assert body["data"]["window_id"] == "01a070e2-2665-7d62-9b74-973decf239b7"
    assert body["data"]["compacted_messages"][0]["role"] == "user"


def test_compaction_persist_failure_reason_includes_server_body() -> None:
    """A rejected compaction persist must name the server's reason.

    ``raise_for_status`` reports only the status and URL, so a 400 on this POST
    left no way to tell which field the server objected to — the payload is
    assembled from Codex's rollout, so the answer is only in the response body.
    """
    request = httpx.Request("POST", "https://example.invalid/v1/sessions/conv_x/events")
    response = httpx.Response(
        400,
        request=request,
        text='{"error_code":"INVALID_PARAMETER_VALUE","message":"last_item_id not found"}',
    )
    exc = httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    reason = fwd._compaction_persist_failure_reason(exc)

    assert "400" in reason
    assert "last_item_id not found" in reason


def test_compaction_persist_failure_reason_handles_non_http_errors() -> None:
    """A non-HTTP failure still gets a one-line reason rather than an empty string."""
    assert fwd._compaction_persist_failure_reason(RuntimeError("boom")) == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_persist_codex_compaction_item_empty_items_fallback() -> None:
    """When no items exist, last_item_id falls back to compact_boundary_ prefix."""
    empty_resp = MagicMock()
    empty_resp.json.return_value = {"data": []}
    empty_resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get = AsyncMock(return_value=empty_resp)

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    await _persist_codex_compaction_item(client, session_id="conv_codex")

    client.post.assert_called_once()
    _url, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["data"]["last_item_id"].startswith("compact_boundary_")
    assert "compacted_messages" not in body["data"]


@pytest.mark.parametrize("window_id", [2, "01a070e2-2665-7d62-9b74-973decf239b7"])
def test_read_compacted_history_extracts_replacement_history_and_window_id(
    tmp_path: Path,
    window_id: int | str,
) -> None:
    """_read_compacted_history returns replacement_history and window_id."""
    import json as _json

    rollout = tmp_path / "rollout.jsonl"
    lines = [
        _json.dumps({"type": "session_meta", "payload": {"id": "abc"}}),
        _json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user"}}),
        _json.dumps(
            {
                "type": "compacted",
                "payload": {
                    "message": "summary",
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}],
                        },
                        {
                            "type": "compaction",
                            "encrypted_content": "gAAAA_test_token",
                        },
                    ],
                    "window_id": window_id,
                },
            }
        ),
    ]
    rollout.write_text("\n".join(lines) + "\n")

    result = fwd._read_compacted_history(rollout)

    assert result is not None
    assert result["window_id"] == window_id
    assert len(result["replacement_history"]) == 2
    assert result["replacement_history"][0]["type"] == "message"
    assert result["replacement_history"][0]["role"] == "user"
    assert result["replacement_history"][1]["type"] == "compaction"
    assert result["replacement_history"][1]["encrypted_content"] == "gAAAA_test_token"


def test_read_compacted_history_returns_none_for_no_compacted_entry(
    tmp_path: Path,
) -> None:
    """_read_compacted_history returns None when no Compacted entry exists."""
    import json as _json

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_json.dumps({"type": "session_meta", "payload": {"id": "abc"}}) + "\n")

    assert fwd._read_compacted_history(rollout) is None
