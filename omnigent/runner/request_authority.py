"""Runner-owned consumed-input state, separate from attribution and model arguments."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import httpx

if TYPE_CHECKING:
    from omnigent.harnesses.codex_native.app_server import CodexAppServerClient

from omnigent.session_authority import (
    MAX_AUTHORITY_INPUTS,
    REQUEST_AUTHORITY_HEADER,
    SOURCE_SESSION_HEADER,
    RequestAuthority,
    current_request_authority,
)

_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")


def bind_tool_authority(
    function: Callable[_Parameters, Coroutine[Any, Any, _Result]],
) -> Callable[_Parameters, Coroutine[Any, Any, _Result]]:
    """Keep the invocation snapshot through nested and background tool calls."""

    @wraps(function)
    async def scoped(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Result:
        explicit = kwargs.get("request_authority")
        authority = (
            explicit if isinstance(explicit, RequestAuthority) else current_request_authority.get()
        )
        token = current_request_authority.set(authority)
        try:
            return await function(*args, **kwargs)
        finally:
            current_request_authority.reset(token)

    return scoped


@dataclass
class TurnAuthority:
    """One turn's inputs; queued inputs do not affect it until consumed."""

    authority: RequestAuthority
    pending: dict[str, RequestAuthority] = field(default_factory=dict)
    native_turn_id: str | None = None

    def snapshot(self) -> RequestAuthority:
        # The executor may have consumed an injection before its receipt arrives.
        # Refuse new operations during that window instead of using the old actor.
        return RequestAuthority((), verified=False) if self.pending else self.authority

    def begin_injection(self, injection_id: str, authority: RequestAuthority) -> None:
        self.pending[injection_id] = authority

    def consume_injection(self, injection_id: str) -> None:
        authority = self.pending.pop(injection_id, None)
        self.authority = self.authority.consume(authority or RequestAuthority((), verified=False))

    def refuse_injection(self, injection_id: str) -> None:
        self.pending.pop(injection_id, None)

    def invalidate(self) -> None:
        self.authority = RequestAuthority((), verified=False)
        self.native_turn_id = None


class _RequesterTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, client: httpx.AsyncClient, source_id: str, authority: RequestAuthority
    ) -> None:
        self._delegate = client
        self._authority_headers = {
            SOURCE_SESSION_HEADER: source_id,
            REQUEST_AUTHORITY_HEADER: json.dumps(authority.to_wire(), separators=(",", ":")),
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers.update(self._authority_headers)
        return await self._delegate.send(request, stream=True)


class RequesterClient(httpx.AsyncClient):
    """Per-call authority; borrows the shared transport without owning its pool."""

    def __init__(
        self, client: httpx.AsyncClient, source_id: str, authority: RequestAuthority
    ) -> None:
        while isinstance(client, RequesterClient):
            client = client._borrowed_client
        self._borrowed_client = client
        super().__init__(
            base_url=client.base_url,
            headers=client.headers,
            cookies=client.cookies,
            params=client.params,
            timeout=client.timeout,
            transport=_RequesterTransport(client, source_id, authority),
        )


def _native_page(response: dict[str, object]) -> tuple[list[dict[str, object]], str | None]:
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ValueError("Native protocol did not return a page of consumed inputs")
    data = result["data"]
    if any(not isinstance(item, dict) for item in data):
        raise ValueError("Invalid native input page")
    cursor = result.get("nextCursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("Invalid native input cursor")
    return data, cursor


async def native_snapshot(
    client: CodexAppServerClient, thread_id: str, state: TurnAuthority
) -> RequestAuthority:
    """Verify the live turn's consumed client IDs against native RPC receipts.

    Older native protocols and unverified terminal inputs fail closed. Bounded
    pages prevent a long-running native turn from producing an unbounded read.
    """
    from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError

    invalid = RequestAuthority((), verified=False)
    if not state.native_turn_id or not state.snapshot().verified:
        return invalid
    receipt = state.authority
    turn_id = state.native_turn_id
    response = await client.request(
        "thread/turns/list",
        {
            "threadId": thread_id,
            "limit": 1,
            "sortDirection": "desc",
            "itemsView": "notLoaded",
        },
    )
    turns, _ = _native_page(response)
    if not turns or turns[0].get("id") != turn_id or turns[0].get("status") != "inProgress":
        return invalid
    cursor = None
    input_ids: list[str] = []
    for _ in range(16):
        params: dict[str, object] = {"threadId": thread_id, "turnId": turn_id, "limit": 256}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            response = await client.request("thread/items/list", params)
        except CodexAppServerResponseError as exc:
            # Codex 0.142 exposes complete live-turn receipts without item pagination.
            missing_method = exc.code == -32601 or (
                exc.code == -32600 and "unknown variant `thread/items/list`" in (exc.message or "")
            )
            if cursor is not None or not missing_method:
                raise
            response = await client.request(
                "thread/turns/list",
                {"threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "full"},
            )
            turns, _ = _native_page(response)
            if (
                len(turns) != 1
                or turns[0].get("id") != turn_id
                or turns[0].get("status") != "inProgress"
                or turns[0].get("itemsView") != "full"
            ):
                return invalid
            items = turns[0].get("items")
            if not isinstance(items, list) or len(items) > 16 * 256:
                return invalid
            entries = [{"turnId": turn_id, "item": item} for item in items]
            cursor = None
        else:
            entries, cursor = _native_page(response)
        for entry in entries:
            if entry.get("turnId") != turn_id:
                return invalid
            item = entry.get("item", {})
            if not isinstance(item, dict):
                return invalid
            if item.get("type") != "userMessage":
                continue
            identifier = item.get("clientId")
            if (
                not isinstance(identifier, str)
                or identifier not in receipt.input_ids
                or identifier in input_ids
            ):
                return invalid
            input_ids.append(identifier)
            if len(input_ids) > MAX_AUTHORITY_INPUTS:
                return invalid
        if cursor is None:
            break
    else:
        return invalid
    # Re-read metadata so an intervening turn replacement cannot borrow a receipt.
    response = await client.request(
        "thread/turns/list",
        {
            "threadId": thread_id,
            "limit": 1,
            "sortDirection": "desc",
            "itemsView": "notLoaded",
        },
    )
    turns, _ = _native_page(response)
    if (
        not input_ids
        or not turns
        or turns[0].get("id") != turn_id
        or turns[0].get("status") != "inProgress"
    ):
        return invalid
    return RequestAuthority(tuple(input_ids))
