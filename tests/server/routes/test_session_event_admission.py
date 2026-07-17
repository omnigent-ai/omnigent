"""Public AP seam coverage for opt-in session-event admission."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.admission import AdmissionInfo, SessionInfo
from omnigent.entities import Agent, Conversation, ConversationItem, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.runtime import set_runner_client
from omnigent.server.routes.sessions import create_sessions_router


class _AgentStore:
    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    def get(self, agent_id: str) -> Agent | None:
        return self.agent if agent_id == self.agent.id else None


class _ConversationStore:
    def __init__(self, conversation: Conversation) -> None:
        self.conversation = conversation
        self.items: list[ConversationItem] = []

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self.conversation if session_id == self.conversation.id else None

    def append(
        self,
        session_id: str,
        items: list[NewConversationItem],
    ) -> list[ConversationItem]:
        if session_id != self.conversation.id:
            return []
        persisted: list[ConversationItem] = []
        for item in items:
            stored = ConversationItem(
                id=f"item_{len(self.items) + 1}",
                type=item.type,
                status="completed",
                response_id=item.response_id,
                created_at=len(self.items) + 1,
                data=item.data,
                created_by=item.created_by,
            )
            self.items.append(stored)
            persisted.append(stored)
        return persisted

    def update_conversation(self, session_id: str, **updates: Any) -> Conversation | None:
        if session_id != self.conversation.id:
            return None
        for key, value in updates.items():
            setattr(self.conversation, key, value)
        return self.conversation


class _Admitter:
    def __init__(self, wanted: bool) -> None:
        self.wanted = wanted
        self.sessions: list[SessionInfo] = []

    async def wants(self, session: SessionInfo) -> bool:
        self.sessions.append(session)
        return self.wanted


class _RunnerClient:
    """Capture reserve, consume, and cancel calls with httpx responses."""

    def __init__(self, *, reservation_status: int = 201) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.reservation_status = reservation_status

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float,
    ) -> httpx.Response:
        del timeout
        self.calls.append(("POST", url, json))
        request = httpx.Request("POST", f"http://runner{url}")
        if url.endswith("/admission-reservations"):
            if self.reservation_status != 201:
                return httpx.Response(
                    self.reservation_status,
                    json={"error": "unavailable"},
                    request=request,
                )
            return httpx.Response(
                201,
                json={
                    "admissionId": "adm_public",
                    "inputSeq": 41,
                    "disposition": "new_turn",
                    "lineageId": "lin_public",
                    "activeResponseId": None,
                    "expiresAt": 9_999_999_999_999,
                },
                request=request,
            )
        return httpx.Response(
            202,
            json={"status": "accepted"},
            request=request,
        )

    async def delete(self, url: str, *, timeout: float) -> httpx.Response:
        del timeout
        self.calls.append(("DELETE", url, None))
        return httpx.Response(
            204,
            request=httpx.Request("DELETE", f"http://runner{url}"),
        )


@pytest.fixture
def route_factory() -> Any:
    """Return a factory for isolated admission-enabled route clients."""

    def _build(
        *,
        admitter: _Admitter | None,
        runner: _RunnerClient,
        callback: Any = None,
    ) -> tuple[FastAPI, str]:
        agent_id = "087b7cb7ac30abf4debfaa578d052ec6"
        conv = Conversation(
            id="conv_admission",
            created_at=1,
            updated_at=1,
            root_conversation_id="conv_admission",
            title="admission session",
            agent_id=agent_id,
        )
        conversation_store = _ConversationStore(conv)
        agent_store = _AgentStore(
            Agent(
                id=agent_id,
                created_at=1,
                name="test-agent",
                bundle_location=f"{agent_id}/bundle",
            )
        )
        app = FastAPI()

        @app.exception_handler(OmnigentError)
        async def _handle_omnigent_error(
            request: Request,
            exc: OmnigentError,
        ) -> JSONResponse:
            del request
            return JSONResponse(
                status_code=exc.http_status,
                content={"error": {"code": exc.code, "message": exc.message}},
            )

        app.include_router(
            create_sessions_router(
                conversation_store=conversation_store,
                agent_store=agent_store,
                session_event_admitter=admitter,
                on_event_admitted=callback,
            ),
            prefix="/v1",
        )
        set_runner_client(runner)  # type: ignore[arg-type]
        return app, conv.id

    return _build


@pytest.fixture(autouse=True)
def _reset_runner_client() -> Any:
    async def _inline_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    route_asyncio = SimpleNamespace(**vars(asyncio))
    route_asyncio.to_thread = _inline_to_thread
    with (
        patch("omnigent.server.routes.sessions.asyncio", route_asyncio),
        patch("omnigent.server.routes._auth_helpers.asyncio", route_asyncio),
    ):
        yield
    set_runner_client(None)


@contextlib.asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        yield client


def _event() -> dict[str, Any]:
    return {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    }


@pytest.mark.asyncio
async def test_s7_10_wants_false_has_zero_admission_calls_or_ack_delta(
    route_factory: Any,
) -> None:
    admitter = _Admitter(False)
    runner = _RunnerClient()
    app, session_id = route_factory(admitter=admitter, runner=runner)

    async def _allow(*_args: Any, **_kwargs: Any) -> None:
        pass

    admission_ids: list[str | None] = []

    async def _dispatch(*_args: Any, **kwargs: Any) -> Any:
        admission_ids.append(kwargs.get("admission_id"))
        return type("Dispatch", (), {"item_id": "item_control", "pending_id": None})()

    async with _client(app) as client:
        with (
            patch(
                "omnigent.server.routes.sessions._evaluate_input_policy",
                new=_allow,
            ),
            patch(
                "omnigent.server.routes.sessions._dispatch_session_event_to_runner",
                new=_dispatch,
            ),
        ):
            response = await client.post(f"/v1/sessions/{session_id}/events", json=_event())

    assert response.status_code == 202
    assert set(response.json()) == {"queued", "item_id"}
    assert response.json()["queued"] is True
    assert runner.calls == []
    assert admission_ids == [None]
    assert [session.id for session in admitter.sessions] == [session_id]


@pytest.mark.asyncio
async def test_public_policy_ack_and_callback_share_one_admission(
    route_factory: Any,
) -> None:
    admitter = _Admitter(True)
    runner = _RunnerClient()
    policy_admissions: list[AdmissionInfo | None] = []
    dispatched_admission_ids: list[str | None] = []
    callbacks: list[tuple[str, str | None, AdmissionInfo]] = []

    async def _allow(*_args: Any, **kwargs: Any) -> None:
        policy_admissions.append(kwargs.get("admission"))

    async def _callback(
        session_id: str,
        item_id: str | None,
        admission: AdmissionInfo,
    ) -> None:
        callbacks.append((session_id, item_id, admission))

    async def _dispatch(*_args: Any, **kwargs: Any) -> Any:
        dispatched_admission_ids.append(kwargs.get("admission_id"))
        return type("Dispatch", (), {"item_id": "item_public", "pending_id": None})()

    app, session_id = route_factory(
        admitter=admitter,
        runner=runner,
        callback=_callback,
    )
    async with _client(app) as client:
        with (
            patch(
                "omnigent.server.routes.sessions._evaluate_input_policy",
                new=_allow,
            ),
            patch(
                "omnigent.server.routes.sessions._dispatch_session_event_to_runner",
                new=_dispatch,
            ),
        ):
            response = await client.post(f"/v1/sessions/{session_id}/events", json=_event())

    assert response.status_code == 202
    payload = response.json()
    assert payload["admission"] == {
        "admission_id": "adm_public",
        "input_seq": 41,
        "disposition": "new_turn",
        "lineage_id": "lin_public",
        "active_response_id": None,
    }
    assert policy_admissions == [
        AdmissionInfo.from_runner_payload(
            {
                "admissionId": "adm_public",
                "inputSeq": 41,
                "disposition": "new_turn",
                "lineageId": "lin_public",
                "activeResponseId": None,
            }
        )
    ]
    assert callbacks == [(session_id, payload["item_id"], policy_admissions[0])]
    assert [call[0] for call in runner.calls] == ["POST"]
    assert dispatched_admission_ids == ["adm_public"]


@pytest.mark.parametrize("reason", ["not allowed", "approval declined"])
@pytest.mark.asyncio
async def test_policy_deny_or_ask_decline_cancels_reservation_before_return(
    route_factory: Any,
    reason: str,
) -> None:
    admitter = _Admitter(True)
    runner = _RunnerClient()
    app, session_id = route_factory(admitter=admitter, runner=runner)

    async def _deny(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"verdict": "deny", "reason": reason}

    async with _client(app) as client:
        with patch(
            "omnigent.server.routes.sessions._evaluate_input_policy",
            new=_deny,
        ):
            response = await client.post(f"/v1/sessions/{session_id}/events", json=_event())

    assert response.status_code == 202
    assert response.json() == {
        "queued": False,
        "denied": True,
        "reason": reason,
    }
    assert [call[:2] for call in runner.calls] == [
        ("POST", f"/v1/sessions/{session_id}/admission-reservations"),
        (
            "DELETE",
            f"/v1/sessions/{session_id}/admission-reservations/adm_public",
        ),
    ]


@pytest.mark.asyncio
async def test_client_disconnect_during_dispatch_cancels_reservation(
    route_factory: Any,
) -> None:
    runner = _RunnerClient()
    app, session_id = route_factory(admitter=_Admitter(True), runner=runner)

    async def _allow(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _disconnect(*_args: Any, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError

    async with _client(app) as client:
        with (
            patch("omnigent.server.routes.sessions._evaluate_input_policy", new=_allow),
            patch(
                "omnigent.server.routes.sessions._dispatch_session_event_to_runner",
                new=_disconnect,
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                await client.post(f"/v1/sessions/{session_id}/events", json=_event())

    assert [call[:2] for call in runner.calls] == [
        ("POST", f"/v1/sessions/{session_id}/admission-reservations"),
        (
            "DELETE",
            f"/v1/sessions/{session_id}/admission-reservations/adm_public",
        ),
    ]


@pytest.mark.asyncio
async def test_wanted_session_fails_closed_when_reservation_is_unavailable(
    route_factory: Any,
) -> None:
    runner = _RunnerClient(reservation_status=503)
    app, session_id = route_factory(admitter=_Admitter(True), runner=runner)

    async with _client(app) as client:
        response = await client.post(f"/v1/sessions/{session_id}/events", json=_event())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "admission_unavailable"
    assert [call[1] for call in runner.calls] == [
        f"/v1/sessions/{session_id}/admission-reservations"
    ]
