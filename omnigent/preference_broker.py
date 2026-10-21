"""Preference-only transport over an owner-authorized host control request."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from pydantic import Field, TypeAdapter

from omnigent.preference_sync import (
    PREFERENCE_PAGE_SIZE,
    PreferenceMutation,
    PreferencePage,
    PreferenceResult,
    ProtocolModel,
)


class PreferenceRead(ProtocolModel):
    action: Literal["read"] = "read"
    limit: int = Field(default=PREFERENCE_PAGE_SIZE, ge=1, le=PREFERENCE_PAGE_SIZE)
    cursor: str | None = Field(default=None, max_length=4096)


class PreferenceWrite(ProtocolModel):
    action: Literal["write"] = "write"
    operation: PreferenceMutation


PreferenceRequest = Annotated[PreferenceRead | PreferenceWrite, Field(discriminator="action")]
PREFERENCE_REQUEST = TypeAdapter(PreferenceRequest)


class PreferenceReply(ProtocolModel):
    status: int = Field(ge=200, le=599)
    page: PreferencePage | None = None
    result: PreferenceResult | None = None


class PreferenceBrokerTransport(httpx.AsyncBaseTransport):
    """Reuse the durable client's paging, sync lock and acknowledgements unchanged."""

    def __init__(self, send: Callable[[str, PreferenceRequest], Awaitable[None]]) -> None:
        self._send = send
        self._pending: dict[str, asyncio.Future[PreferenceReply]] = {}

    def deliver(self, request_id: str, reply: PreferenceReply) -> None:
        pending = self._pending.get(request_id)
        if pending is not None and not pending.done():
            pending.set_result(reply)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/preferences/shared":
            action: PreferenceRequest = PreferenceRead.model_validate(
                {"action": "read", **dict(request.url.params)}
            )
        elif request.method == "POST" and request.url.path == "/v1/preferences/shared/operations":
            action = PreferenceWrite(
                operation=PreferenceMutation.model_validate_json(request.content)
            )
        else:
            raise ValueError("Only preference reads and edits may use the owner tunnel")
        if self._pending:
            raise httpx.ConnectError("A preference request is already pending")
        request_id = uuid4().hex
        future: asyncio.Future[PreferenceReply] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(request_id, action)
            reply = await asyncio.wait_for(future, 5)
        except TimeoutError as exc:
            raise httpx.ReadTimeout("Preference broker did not respond") from exc
        finally:
            self._pending.pop(request_id, None)
        body = reply.page if isinstance(action, PreferenceRead) else reply.result
        if reply.status < 400 and body is None:
            raise ValueError("Preference broker returned an incomplete reply")
        return httpx.Response(reply.status, json=body.model_dump(mode="json") if body else {})

    async def aclose(self) -> None:
        for pending in self._pending.values():
            if not pending.done():
                pending.cancel()
        self._pending.clear()
