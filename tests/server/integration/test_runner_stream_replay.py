"""Runner replay ordering must preserve the server's final session state."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.routing import APIRoute

from omnigent.runner import create_runner_app
from omnigent.server.routes._sessions import orchestration


async def test_interrupted_status_replay_does_not_create_a_false_mid_turn_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "synthetic-replay-final-status"

    async def persist_labels(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(orchestration, "_persist_session_status_error_labels", persist_labels)
    orchestration._session_status_cache.pop(session_id, None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as upstream:
        app = create_runner_app(server_client=upstream)
        route = next(
            route
            for route in app.routes
            if isinstance(route, APIRoute) and route.path == "/v1/sessions/{session_id}/stream"
        )
        old_response = await route.endpoint(session_id)
        old_stream = old_response.body_iterator
        try:
            await anext(old_stream)
            queue = app.state.session_event_queues[session_id]
            queue.put_nowait({"type": "session.status", "status": "running"})
            queue.put_nowait({"type": "session.status", "status": "idle"})
            await anext(old_stream)
            await old_stream.aclose()
            queue.put_nowait(None)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as runner_client:
                await asyncio.wait_for(
                    orchestration._relay_runner_stream_once(
                        session_id,
                        runner_client,
                        None,
                        runner_id="synthetic-runner",  # type: ignore[arg-type]
                    ),
                    timeout=5.0,
                )

            would_fail_on_disconnect = await orchestration._runner_disconnect_requires_failure(
                session_id,
                None,
                origin="runner_disconnected_mid_turn",  # type: ignore[arg-type]
            )
            assert (
                orchestration._session_status_cache.get(session_id),
                would_fail_on_disconnect,
            ) == ("idle", False)
        finally:
            await old_stream.aclose()
            app.state.session_event_queues.pop(session_id, None)
            orchestration._session_status_cache.pop(session_id, None)
