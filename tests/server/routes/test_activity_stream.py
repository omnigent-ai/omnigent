"""Focused tests for the Glitchy activity SSE route helper."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.runtime import activity_stream
from omnigent.server.routes import sessions as sessions_mod


class _Request:
    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _clean_activity_stream_state() -> None:
    activity_stream.reset_for_tests()
    yield
    activity_stream.close_all()
    activity_stream.reset_for_tests()


@pytest.mark.asyncio
async def test_stream_activity_events_formats_live_sse_and_done() -> None:
    """The inspection stream emits live classified records as SSE frames."""
    gen = sessions_mod._stream_activity_events(_Request())
    first_frame_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)

    activity_stream.record_activity_event(
        {
            "route": "attention_librarian",
            "kind": "attention_event",
            "session_id": "conv_control",
            "session_title": "Control Room",
            "source": "test",
        },
        generated_at="2026-07-07T15:00:00Z",
    )

    frame = await asyncio.wait_for(first_frame_task, timeout=2.0)
    assert frame.startswith("event: glitchy.activity\n")
    assert '"session_id": "conv_control"' in frame

    done_frame_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)
    activity_stream.close_all()

    done_frame = await asyncio.wait_for(done_frame_task, timeout=2.0)
    assert done_frame == "data: [DONE]\n\n"
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
