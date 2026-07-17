"""Race and lifecycle coverage for atomic session-event admission."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from tests.runner.helpers import NullServerClient


class _BlockingHarnessClient:
    """Harness stream that remains live until the test releases it."""

    def __init__(self) -> None:
        self.allow_created = asyncio.Event()
        self.allow_created.set()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.turn_bodies: list[dict[str, Any]] = []
        self.injected_bodies: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Capture the turn and return a controllable SSE stream."""
        del method, url, timeout
        self.turn_bodies.append(json)
        outer = self
        response_id = f"resp_{len(self.turn_bodies)}"

        class _StreamContext:
            async def __aenter__(self) -> _BlockingHarnessClient._StreamHandle:
                return _BlockingHarnessClient._StreamHandle(outer, response_id)

            async def __aexit__(self, *_args: Any) -> None:
                return None

        return _StreamContext()

    class _StreamHandle:
        status_code = 200

        def __init__(self, owner: _BlockingHarnessClient, response_id: str) -> None:
            self._owner = owner
            self._response_id = response_id

        async def aiter_text(self) -> AsyncIterator[str]:
            await self._owner.allow_created.wait()
            self._owner.started.set()
            yield _sse(
                {
                    "type": "response.created",
                    "response": {"id": self._response_id},
                }
            )
            await self._owner.release.wait()
            yield _sse(
                {
                    "type": "response.completed",
                    "response": {"id": self._response_id},
                }
            )

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        """Record a live injection and acknowledge it."""
        del url, timeout
        self.injected_bodies.append(json)
        return httpx.Response(200, json={"ok": True})


class _FakeProcessManager:
    """Small process-manager surface used by the runner turn path."""

    handles_tool_dispatch = True

    def __init__(self, client: _BlockingHarnessClient) -> None:
        self.client = client
        self.in_flight: set[str] = set()

    async def get_client(
        self,
        conversation_id: str,
        harness: str,
        env: Any = None,
    ) -> _BlockingHarnessClient:
        del conversation_id, harness, env
        return self.client

    def has_active_turn(self, conversation_id: str) -> bool:
        return conversation_id in self.in_flight

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        del response_id
        self.in_flight.add(conversation_id)

    def clear_in_flight(self, conversation_id: str) -> None:
        self.in_flight.discard(conversation_id)

    async def forward_cancel(self, conversation_id: str) -> bool:
        self.in_flight.discard(conversation_id)
        return True

    async def release(self, conversation_id: str) -> None:
        self.in_flight.discard(conversation_id)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _build_app(
    *,
    ttl_seconds: float = 30.0,
) -> tuple[FastAPI, _BlockingHarnessClient]:
    harness = _BlockingHarnessClient()
    manager = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        admission_reservation_ttl_seconds=ttl_seconds,
    )
    return app, harness


@contextlib.asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


async def _reserve(client: httpx.AsyncClient, session_id: str) -> httpx.Response:
    return await client.post(
        f"/v1/sessions/{session_id}/admission-reservations",
        json={"source": "ap", "kind": "user_message"},
    )


async def _consume(
    client: httpx.AsyncClient,
    session_id: str,
    admission_id: str,
    text: str,
) -> httpx.Response:
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "admissionId": admission_id,
        },
    )


async def _cancel(
    client: httpx.AsyncClient,
    session_id: str,
    admission_id: str,
) -> httpx.Response:
    return await client.delete(f"/v1/sessions/{session_id}/admission-reservations/{admission_id}")


@pytest.mark.asyncio
async def test_s7_1_concurrent_idle_posts_get_one_new_turn_and_fifo() -> None:
    app, harness = _build_app()
    async with _client(app) as client:
        first = (await _reserve(client, "conv_race")).json()
        second_task = asyncio.create_task(_reserve(client, "conv_race"))
        await asyncio.sleep(0)
        assert not second_task.done()

        accepted = await _consume(client, "conv_race", first["admissionId"], "first")
        second_response = await asyncio.wait_for(second_task, timeout=1)
        second = second_response.json()

        assert accepted.json()["status"] == "accepted"
        assert first["disposition"] == "new_turn"
        assert second["disposition"] in {"active_steer", "next_turn_buffer"}
        assert [first["inputSeq"], second["inputSeq"]] == [0, 1]
        assert second["lineageId"] == first["lineageId"]
        buffered = await _consume(client, "conv_race", second["admissionId"], "second")
        assert buffered.json()["status"] == "buffered"
        duplicate = await _consume(client, "conv_race", second["admissionId"], "duplicate")
        assert duplicate.status_code == 409
        assert duplicate.json()["error"] == "admission_already_consumed"
        harness.release.set()


@pytest.mark.asyncio
async def test_s7_2_live_tool_loop_steer_keeps_active_lineage() -> None:
    app, harness = _build_app()
    async with _client(app) as client:
        first = (await _reserve(client, "conv_tool_loop")).json()
        await _consume(client, "conv_tool_loop", first["admissionId"], "start")
        await asyncio.wait_for(harness.started.wait(), timeout=1)

        steer = (await _reserve(client, "conv_tool_loop")).json()
        assert steer["disposition"] == "active_steer"
        assert steer["lineageId"] == first["lineageId"]
        assert steer["activeResponseId"] == "resp_1"
        response = await _consume(client, "conv_tool_loop", steer["admissionId"], "steer")
        assert response.json()["status"] == "buffered"
        assert harness.injected_bodies[-1]["content"][0]["text"] == "steer"
        harness.release.set()


@pytest.mark.asyncio
async def test_s7_3_reserved_buffered_continuation_keeps_sequence_and_lineage() -> None:
    app, harness = _build_app()
    harness.allow_created.clear()
    async with _client(app) as client:
        first = (await _reserve(client, "conv_buffered")).json()
        await _consume(client, "conv_buffered", first["admissionId"], "first")
        continuation = (await _reserve(client, "conv_buffered")).json()
        assert continuation["disposition"] == "next_turn_buffer"
        assert continuation["lineageId"] == first["lineageId"]
        assert continuation["inputSeq"] == first["inputSeq"] + 1
        buffered = await _consume(
            client,
            "conv_buffered",
            continuation["admissionId"],
            "continued",
        )
        assert buffered.json()["status"] == "buffered"
        harness.allow_created.set()
        harness.release.set()
        for _ in range(50):
            if len(harness.turn_bodies) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(harness.turn_bodies) == 2


@pytest.mark.asyncio
async def test_s7_4_cancel_releases_slot_and_next_decision_is_truthful() -> None:
    app, _harness = _build_app()
    async with _client(app) as client:
        denied = (await _reserve(client, "conv_cancel")).json()
        assert (await _cancel(client, "conv_cancel", denied["admissionId"])).status_code == 204
        next_admission = (await _reserve(client, "conv_cancel")).json()
        assert next_admission["inputSeq"] == denied["inputSeq"] + 1
        assert next_admission["disposition"] == "new_turn"
        await _cancel(client, "conv_cancel", next_admission["admissionId"])


@pytest.mark.asyncio
async def test_s7_5_park_consumes_before_ttl_and_fails_after_ttl() -> None:
    app, harness = _build_app(ttl_seconds=0.05)
    async with _client(app) as client:
        approved = (await _reserve(client, "conv_ask_ok")).json()
        await asyncio.sleep(0.01)
        assert (
            await _consume(client, "conv_ask_ok", approved["admissionId"], "approved")
        ).status_code == 202
        harness.release.set()
        await asyncio.sleep(0.08)
        consumed_again = await _consume(
            client,
            "conv_ask_ok",
            approved["admissionId"],
            "approved again",
        )
        assert consumed_again.status_code == 409
        assert consumed_again.json()["error"] == "admission_already_consumed"

        expired = (await _reserve(client, "conv_ask_expired")).json()
        await asyncio.sleep(0.08)
        response = await _consume(
            client,
            "conv_ask_expired",
            expired["admissionId"],
            "late approval",
        )
        assert response.status_code == 410
        assert response.json()["error"] == "admission_expired"


@pytest.mark.asyncio
async def test_s7_6_ttl_expiry_under_load_preserves_fifo_order() -> None:
    app, _harness = _build_app(ttl_seconds=0.03)
    async with _client(app) as client:
        tasks = [asyncio.create_task(_reserve(client, "conv_load")) for _ in range(4)]
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        payloads = [response.json() for response in responses]
        assert [payload["inputSeq"] for payload in payloads] == [0, 1, 2, 3]
        assert all(payload["disposition"] == "new_turn" for payload in payloads)
        await _cancel(client, "conv_load", payloads[-1]["admissionId"])


@pytest.mark.asyncio
async def test_s7_7_double_foreign_and_restart_consumes_are_named_errors() -> None:
    app, harness = _build_app()
    async with _client(app) as client:
        admission = (await _reserve(client, "conv_owner")).json()
        foreign = await _consume(client, "conv_foreign", admission["admissionId"], "foreign")
        assert foreign.status_code == 409
        assert foreign.json()["error"] == "admission_session_mismatch"
        await _consume(client, "conv_owner", admission["admissionId"], "owner")
        consumed_foreign = await _consume(
            client,
            "conv_foreign",
            admission["admissionId"],
            "foreign after consume",
        )
        assert consumed_foreign.status_code == 409
        assert consumed_foreign.json()["error"] == "admission_session_mismatch"
        double = await _consume(client, "conv_owner", admission["admissionId"], "again")
        assert double.status_code == 409
        assert double.json()["error"] == "admission_already_consumed"
        harness.release.set()

    restarted_app, _ = _build_app()
    async with _client(restarted_app) as restarted_client:
        missing = await _consume(
            restarted_client,
            "conv_owner",
            admission["admissionId"],
            "after restart",
        )
        assert missing.status_code == 404
        assert missing.json()["error"] == "admission_not_found"


@pytest.mark.asyncio
async def test_s7_8_fork_session_reservation_is_isolated_from_source() -> None:
    app, _harness = _build_app()
    async with _client(app) as client:
        source = (await _reserve(client, "conv_source")).json()
        fork = (await _reserve(client, "conv_fork")).json()
        assert source["disposition"] == fork["disposition"] == "new_turn"
        assert source["inputSeq"] == fork["inputSeq"] == 0
        assert source["lineageId"] != fork["lineageId"]
        assert set(app.state.admission_reservations) == {
            source["admissionId"],
            fork["admissionId"],
        }
        await _cancel(client, "conv_source", source["admissionId"])
        await _cancel(client, "conv_fork", fork["admissionId"])


@pytest.mark.asyncio
async def test_s7_9_first_input_after_resume_gets_fresh_truthful_lineage() -> None:
    app, harness = _build_app()
    async with _client(app) as client:
        before = (await _reserve(client, "conv_resume")).json()
        await _consume(client, "conv_resume", before["admissionId"], "before resume")
        harness.release.set()
        await asyncio.sleep(0.05)
        resumed = (await _reserve(client, "conv_resume")).json()
        assert resumed["disposition"] == "new_turn"
        assert resumed["lineageId"] != before["lineageId"]
        await _cancel(client, "conv_resume", resumed["admissionId"])
