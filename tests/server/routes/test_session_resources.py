"""Tests for server session resource endpoints (Phase 1a + 1b + 1c)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import DEFAULT_ENVIRONMENT_ID, Conversation, ConversationItem, PagedList
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import _globals, session_stream, set_runner_client, set_runner_router
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.server.schemas import SessionEventInput


class _ConversationStore:
    """Minimal in-memory conversation store for tests.

    :param conversations: Map of conversation_id to Conversation.
    """

    def __init__(self) -> None:
        """
        Initialize the canned conversations used by route tests.

        :returns: None.
        """
        self._conversations = {
            "conv_proxy": Conversation(
                id="conv_proxy",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_proxy",
                agent_id="ag_test",
            ),
            "conv_local": Conversation(
                id="conv_local",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_local",
                agent_id="ag_test",
            ),
            "conv_claude": Conversation(
                id="conv_claude",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_claude",
                agent_id="ag_test",
                labels={
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "claude-code-native-ui",
                },
            ),
            # A spec-driven native sub-agent child (e.g. a nessie
            # claude_code reviewer): kind="sub_agent" with a parent
            # ref and the regular native wrapper label (NOT the
            # internal "-subagent" label), so the native message
            # bypass runs on its first message.
            "conv_child_native": Conversation(
                id="conv_child_native",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_parent",
                kind="sub_agent",
                parent_conversation_id="conv_parent",
                agent_id="ag_test",
                labels={
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "claude-code-native-ui",
                },
            ),
            # A three-level spawn lineage for the file-copy tests:
            # conv_gp (root) -> conv_p (child) -> conv_c (grandchild).
            "conv_gp": Conversation(
                id="conv_gp",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_gp",
                agent_id="ag_test",
            ),
            "conv_p": Conversation(
                id="conv_p",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_gp",
                kind="sub_agent",
                parent_conversation_id="conv_gp",
                agent_id="ag_test",
            ),
            "conv_c": Conversation(
                id="conv_c",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_gp",
                kind="sub_agent",
                parent_conversation_id="conv_p",
                agent_id="ag_test",
            ),
        }
        self.appended_items: list[Any] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Return the conversation or None."""
        return self._conversations.get(conversation_id)

    def list_conversations(
        self,
        *,
        limit: int = 20,
        after: str | None = None,
        kind: str | None = None,
        root_conversation_id: str | None = None,
        **_kwargs: Any,
    ) -> PagedList[Conversation]:
        """Minimal list for policy-builder subtree walk.

        :param limit: Max items per page.
        :param after: Cursor for pagination.
        :param kind: Conversation kind filter (ignored when ``None``).
        :param root_conversation_id: Filter by root tree id.
        :returns: A :class:`PagedList` of matching conversations.
        """
        convs = list(self._conversations.values())
        if root_conversation_id is not None:
            convs = [c for c in convs if c.root_conversation_id == root_conversation_id]
        if kind is not None:
            convs = [c for c in convs if c.kind == kind]
        return PagedList(data=convs, has_more=False)

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        **kwargs: Any,
    ) -> Conversation | None:
        """Update the stored conversation title.

        :param conversation_id: Conversation id to update.
        :param title: Optional title to set.
        :param kwargs: Extra store fields ignored by this test stub.
        :returns: Updated conversation, or ``None`` if absent.
        """
        del kwargs
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        conv.title = title
        return conv

    def set_labels(
        self,
        conversation_id: str,
        updates: dict[str, str],
        updated_at: int | None = None,
    ) -> None:
        """Merge label updates into an in-memory conversation."""
        del updated_at
        conv = self._conversations[conversation_id]
        conv.labels.update(updates)

    def append(
        self,
        conversation_id: str,
        items: list[Any],
    ) -> list[Any]:
        """Record appended items and return them with fake ids.

        :param conversation_id: The conversation id.
        :param items: Items to append.
        :returns: Items with fake ids assigned.
        """
        import time

        from omnigent.entities import ConversationItem

        result = []
        for item in items:
            persisted = ConversationItem(
                id=f"item_{len(self.appended_items)}",
                type=item.type,
                status="completed",
                response_id=item.response_id,
                created_at=int(time.time()),
                data=item.data,
                created_by=getattr(item, "created_by", None),
            )
            self.appended_items.append(persisted)
            result.append(persisted)
        return result

    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        """Return appended items with the store interface shape.

        :param conversation_id: Conversation id to read.
        :param limit: Maximum number of items to return.
        :param after: Cursor after which to read (ignored).
        :param before: Cursor before which to read (ignored).
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :param type: Optional item type filter.
        :returns: A page of matching in-memory items.
        """
        del conversation_id, after, before
        items = [
            item
            for item in self.appended_items
            if type is None or getattr(item, "type", None) == type
        ]
        if order == "desc":
            items = list(reversed(items))
        items = items[:limit]
        return PagedList(
            data=items,
            first_id=items[0].id if items else None,
            last_id=items[-1].id if items else None,
            has_more=False,
        )


class _FakeRunnerClient:
    """Fake httpx.AsyncClient that records calls and returns canned responses.

    Supports per-path response overrides via ``responses`` dict.

    :param payload: Default JSON payload for all requests.
    :param status_code: Default HTTP status code.
    :param exc: If set, raise this on every call.
    :param responses: Optional map of URL path → (status, payload)
        for per-path overrides.
    :param text_responses: Optional map of URL path → (status,
        body text, headers) for non-JSON overrides.
    """

    def __init__(
        self,
        *,
        payload: dict[str, object] | list[object] | None = None,
        status_code: int = 200,
        exc: Exception | None = None,
        exc_paths: dict[str, Exception] | None = None,
        responses: dict[str, tuple[int, dict[str, Any]]] | None = None,
        text_responses: dict[str, tuple[int, str, dict[str, str]]] | None = None,
    ) -> None:
        """
        Initialize a fake runner HTTP client.

        :param payload: Default JSON payload for every response.
        :param status_code: Default HTTP status code.
        :param exc: Exception to raise on every request.
        :param exc_paths: Per-URL exception overrides — raise only when
            the request targets that exact path, e.g.
            ``{"/v1/sessions/conv_x/events": ConnectionError("boom")}``.
            Lets a test pass one stage (terminal ensure) and fail the
            next (message forward) on the same client.
        :param responses: Per-URL JSON response overrides.
        :param text_responses: Per-URL text response overrides.
        :returns: None.
        """
        self._payload = payload
        self._status_code = status_code
        self._exc = exc
        self._exc_paths = exc_paths or {}
        self._responses = responses or {}
        self._text_responses = text_responses or {}
        self.calls: list[tuple[str, str]] = []
        self.post_json_calls: list[tuple[str, Any]] = []
        # Query params passed to each GET, in call order. ``None`` when
        # the caller sent no params — lets tests assert that a proxy
        # forwarded (or deliberately dropped) the incoming query string.
        self.get_params: list[dict[str, str] | None] = []

    def _make_response(
        self,
        method: str,
        url: str,
    ) -> httpx.Response:
        """Build a canned response for the given method + url.

        :param method: HTTP method, e.g. ``"GET"``.
        :param url: Request URL path.
        :returns: The canned httpx.Response.
        """
        self.calls.append((method, url))
        if self._exc is not None:
            raise self._exc
        if url in self._exc_paths:
            raise self._exc_paths[url]
        if url in self._text_responses:
            status, text, headers = self._text_responses[url]
            return httpx.Response(
                status_code=status,
                text=text,
                headers=headers,
                request=httpx.Request(method, url),
            )
        if url in self._responses:
            status, body = self._responses[url]
            return httpx.Response(
                status_code=status,
                json=body,
                request=httpx.Request(method, url),
            )
        return httpx.Response(
            status_code=self._status_code,
            json=self._payload,
            request=httpx.Request(method, url),
        )

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        Record and return a GET response.

        :param url: Request URL path.
        :param params: Query params forwarded by the proxy (e.g. the
            resources route's ``{"type": "environment"}``); recorded in
            ``get_params`` for forwarding assertions, ignored by the
            canned-response lookup.
        :param timeout: Request timeout (ignored).
        :returns: The canned response.
        """
        del timeout
        self.get_params.append(params)
        return self._make_response("GET", url)

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        Record and return a POST response.

        :param url: Request URL path.
        :param json: JSON body passed to the fake client.
        :param timeout: Request timeout (ignored).
        :returns: The canned response.
        """
        del timeout
        self.post_json_calls.append((url, json))
        return self._make_response("POST", url)

    async def put(
        self,
        url: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Record and return a PUT response."""
        del json, timeout
        return self._make_response("PUT", url)

    async def patch(
        self,
        url: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Record and return a PATCH response."""
        del json, timeout
        return self._make_response("PATCH", url)

    async def delete(self, url: str, *, timeout: float | None = None) -> httpx.Response:
        """Record and return a DELETE response."""
        del timeout
        return self._make_response("DELETE", url)


class _RoutedRunner:
    def __init__(self, client: _FakeRunnerClient) -> None:
        self.runner_id = "runner_one"
        self.client = client


class _FakeRunnerRouter:
    def __init__(self, client: _FakeRunnerClient) -> None:
        self.client = client
        self.resource_calls: list[str] = []

    def client_for_session_resources(self, session_id: str) -> _RoutedRunner:
        self.resource_calls.append(session_id)
        return _RoutedRunner(self.client)


@pytest.fixture
def runner_globals_reset() -> Iterator[None]:
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    set_runner_client(None)
    set_runner_router(None)
    yield
    set_runner_client(prior_client)
    set_runner_router(prior_router)


@pytest.fixture
def app(runner_globals_reset: None) -> FastAPI:
    del runner_globals_reset
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

    class _StubAgentStore:
        """Minimal agent store stub for tests that don't exercise agents."""

        def get(self, agent_id):
            """
            :param agent_id: Agent id.
            :returns: None (no agents in stub).
            """
            return

    app.include_router(
        create_sessions_router(
            _ConversationStore(),  # type: ignore[arg-type]
            _StubAgentStore(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as c:
        yield c


def _runner_payload() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_ENVIRONMENT_ID,
                "object": "session.resource",
                "type": "environment",
                "session_id": "conv_proxy",
                "name": "Primary environment",
                "metadata": {
                    "environment_type": "caller_process",
                    "role": "primary",
                },
            },
            {
                "id": "terminal_runner_s1",
                "object": "session.resource",
                "type": "terminal",
                "session_id": "conv_proxy",
                "name": "runner:s1",
                "environment": DEFAULT_ENVIRONMENT_ID,
                "metadata": {
                    "terminal_name": "runner",
                    "session_key": "s1",
                    "running": True,
                },
            },
        ],
        "first_id": DEFAULT_ENVIRONMENT_ID,
        "last_id": "terminal_runner_s1",
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_get_session_labels_uses_labels_only_path(
    client: httpx.AsyncClient,
) -> None:
    """
    Labels endpoint must not build the full session snapshot.

    The test app's conversation store intentionally has no ``list_items``
    method. If this endpoint regresses to the full snapshot builder, the
    request fails before returning labels. A runner client is also seeded
    to prove the route does not query runner status or skills.

    :param client: HTTP client for the test app.
    :returns: None.
    """
    fake_runner = _FakeRunnerClient(payload={"status": "running"})
    set_runner_client(fake_runner)  # type: ignore[arg-type]

    resp = await client.get("/v1/sessions/conv_claude/labels")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {
        "id": "conv_claude",
        "labels": {
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "claude-code-native-ui",
        },
    }
    assert fake_runner.calls == []


@pytest.mark.asyncio
async def test_list_session_resources_proxies_to_bound_runner(
    client: httpx.AsyncClient,
) -> None:
    """Resource listing delegates runner selection to the runner router."""
    fake_runner = _FakeRunnerClient(payload=_runner_payload())
    fake_router = _FakeRunnerRouter(fake_runner)
    set_runner_router(fake_router)  # type: ignore[arg-type]

    resp = await client.get("/v1/sessions/conv_proxy/resources")

    assert resp.status_code == 200
    assert fake_router.resource_calls == ["conv_proxy"]
    assert fake_runner.calls == [("GET", "/v1/sessions/conv_proxy/resources")]
    body = resp.json()
    ids = [resource["id"] for resource in body["data"]]
    assert ids == [DEFAULT_ENVIRONMENT_ID, "terminal_runner_s1"]


@pytest.mark.asyncio
async def test_list_session_resources_validates_session_before_proxy(
    client: httpx.AsyncClient,
) -> None:
    fake_runner = _FakeRunnerClient(payload=_runner_payload())
    fake_router = _FakeRunnerRouter(fake_runner)
    set_runner_router(fake_router)  # type: ignore[arg-type]

    resp = await client.get("/v1/sessions/conv_missing/resources")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert not fake_router.resource_calls
    assert not fake_runner.calls


@pytest.mark.asyncio
async def test_list_session_resources_rejects_malformed_runner_response(
    client: httpx.AsyncClient,
) -> None:
    fake_runner = _FakeRunnerClient(
        payload={"object": "list", "data": [], "has_more": False},
    )
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get("/v1/sessions/conv_proxy/resources")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "runner session-resources endpoint returned malformed response"


@pytest.mark.asyncio
async def test_list_session_resources_local_fallback_lists_default(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/sessions/conv_local/resources")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == [
        {
            "id": DEFAULT_ENVIRONMENT_ID,
            "object": "session.resource",
            "type": "environment",
            "session_id": "conv_local",
            "name": "Primary environment",
            "metadata": {
                "environment_type": "caller_process",
                "role": "primary",
            },
        }
    ]


@pytest.mark.asyncio
async def test_claude_native_message_forwards_to_runner_without_persisting(
    client: httpx.AsyncClient,
) -> None:
    """
    Claude-native web-chat input is runner injection, not Omnigent persistence.

    This fails if the route regresses to the legacy create-or-steer
    path, which would either start a duplicate Omnigent agent task or make
    the web UI render a non-terminal-originated user message.
    """
    fake_runner = _FakeRunnerClient(payload={})
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]
    set_runner_client(fake_runner)  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_claude/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello claude"}],
            },
        },
    )

    assert resp.status_code == 202, resp.text
    # Native message bypass returns queued=True plus a pending-input id: the
    # message isn't persisted AP-side (the transcript forwarder is the single
    # writer), so the server records a pending-input entry for the optimistic
    # bubble and hands back its id (see pending_inputs.record).
    body = resp.json()
    assert body["queued"] is True
    assert body["pending_id"].startswith("pending_")
    assert fake_runner.calls == [
        ("POST", "/v1/sessions/conv_claude/resources/terminals"),
        ("POST", "/v1/sessions/conv_claude/events"),
    ]
    assert fake_runner.post_json_calls == [
        (
            "/v1/sessions/conv_claude/resources/terminals",
            {
                "terminal": "claude",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        ),
        (
            "/v1/sessions/conv_claude/events",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello claude"}],
                "model": "claude-native-ui",
                "harness": "claude-native",
                # Forwarded so the runner resolves the harness spec on the
                # first message (before POST /v1/sessions caches it).
                "agent_id": "ag_test",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_claude_native_assistant_message_rejected_not_forwarded(
    client: httpx.AsyncClient,
) -> None:
    """Only user messages are injectable into a claude-native terminal;
    an assistant-role message is rejected (400) before any runner call."""
    fake_runner = _FakeRunnerClient(payload={})
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]
    set_runner_client(fake_runner)  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_claude/events",
        json={
            "type": "message",
            "data": {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            },
        },
    )

    assert resp.status_code == 400, resp.text
    assert fake_runner.calls == [], f"nothing should reach runner; saw {fake_runner.calls!r}"


@pytest.mark.asyncio
async def test_claude_native_message_surfaces_runner_sse_failure(
    client: httpx.AsyncClient,
) -> None:
    """
    Runner SSE ``response.failed`` means terminal injection failed.

    This fails if the Omnigent route treats the runner's streaming HTTP 200
    as success while the harness reports that ``tmux send-keys`` did
    not deliver the message.
    """
    sse = (
        "event: response.failed\n"
        'data: {"type":"response.failed","error":{"message":"tmux target missing"}}\n\n'
    )
    fake_runner = _FakeRunnerClient(
        text_responses={
            "/v1/sessions/conv_claude/events": (
                200,
                sse,
                {"content-type": "text/event-stream"},
            )
        }
    )
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]
    set_runner_client(fake_runner)  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_claude/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello claude"}],
            },
        },
    )

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == (
        "Claude terminal message delivery failed: tmux target missing"
    )
    assert fake_runner.calls == [
        ("POST", "/v1/sessions/conv_claude/resources/terminals"),
        ("POST", "/v1/sessions/conv_claude/events"),
    ]


@pytest.mark.asyncio
async def test_claude_native_message_tunnel_close_mid_forward_returns_502(
    client: httpx.AsyncClient,
) -> None:
    """
    A WS-tunnel drop between terminal ensure and message forward is a 502.

    WSTunnelTransport raises bare ``ConnectionError`` — not an
    ``httpx.HTTPError`` — when the runner tunnel closes mid-request.
    Before this fix the exception escaped the forward's
    ``except httpx.HTTPError`` clause to the global catch-all and the
    client saw an opaque 500 ``internal_error``; it must map to the
    same 502 as any other transport failure on the forward leg.
    """
    fake_runner = _FakeRunnerClient(
        payload={},
        exc_paths={
            # Ensure (terminals POST) succeeds via the default payload;
            # only the message-forward leg drops the tunnel.
            "/v1/sessions/conv_claude/events": ConnectionError(
                "tunnel closed before request completed"
            ),
        },
    )
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]
    set_runner_client(fake_runner)  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_claude/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello claude"}],
            },
        },
    )

    # 502 is the designed transport-failure mapping; a 500 here means
    # the bare ConnectionError escaped to the catch-all again.
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == "Claude terminal message delivery failed"
    # Ensure ran (and passed) before the forward leg hit the drop.
    assert fake_runner.calls == [
        ("POST", "/v1/sessions/conv_claude/resources/terminals"),
        ("POST", "/v1/sessions/conv_claude/events"),
    ]


@pytest.mark.asyncio
async def test_native_subagent_terminal_boot_failure_wakes_parent(
    client: httpx.AsyncClient,
) -> None:
    """
    A native sub-agent's failed terminal boot must wake its parent.

    The parent's runner spawned this child and posted its first
    message; the native bypass returns HTTP 202 even when the
    terminal cannot boot, so the parent's ``spawn`` call sees success
    and parks a ``running`` work entry. No harness boots, so the Stop
    hook that drives the normal terminal-completion edge never fires.
    The route must therefore forward ``external_session_status: failed``
    (carrying the boot error as ``output``) to the child's runner,
    which maps it to ``mark_subagent_work_terminal(status="failed")``
    and wakes the parent. Without that forward the parent hangs
    forever and a retry of the same (agent, title) hits the busy guard.

    This fails (RED) if the failure path stops at
    ``_publish_status("failed")`` — which writes only the SSE / status
    cache — and omits the runner forward the normal completion path
    performs.
    """
    # The runner accepts the terminal-ensure POST with a definitive
    # 503 + structured error (e.g. the native CLI is missing), so
    # ``_ensure_native_terminal_ready`` returns ErrorData and the
    # bypass takes the terminal-failure branch.
    fake_runner = _FakeRunnerClient(
        responses={
            "/v1/sessions/conv_child_native/resources/terminals": (
                503,
                {
                    "error": {
                        "code": "native_terminal_start_failed",
                        "message": "Native Claude requires the 'claude' CLI on PATH.",
                    }
                },
            ),
        }
    )
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]
    set_runner_client(fake_runner)  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_child_native/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "review the PR"}],
            },
        },
    )

    # The route still acknowledges the message (202) — the failure is
    # reported to the parent out-of-band, not by failing this request.
    assert resp.status_code == 202, resp.text

    # Exactly two runner POSTs: the failed terminal-ensure, then the
    # parent-wake status forward. A third call, or the forward missing,
    # both indicate a regression.
    assert fake_runner.calls == [
        ("POST", "/v1/sessions/conv_child_native/resources/terminals"),
        ("POST", "/v1/sessions/conv_child_native/events"),
    ], f"expected ensure + status-forward; saw {fake_runner.calls!r}"

    # The second POST is the parent-wake edge. It must be a failed
    # external_session_status carrying the boot error as ``output`` so
    # the runner delivers the real cause into the parent's inbox
    # (runner: ``output or "...turn failed"``). Asserting the exact
    # payload proves the parent is notified — not left running forever.
    forward_url, forward_body = fake_runner.post_json_calls[-1]
    assert forward_url == "/v1/sessions/conv_child_native/events"
    assert forward_body == {
        "type": "external_session_status",
        "data": {
            "status": "failed",
            "output": "Native Claude requires the 'claude' CLI on PATH.",
        },
    }


@pytest.mark.asyncio
async def test_native_subagent_terminal_boot_failure_surfaces_unreachable_runner(
    client: httpx.AsyncClient,
) -> None:
    """
    The parent-wake forward must fail loud if the runner is unreachable.

    The forward reuses ``_require_external_status_forward``: dropping
    the failed edge silently would strand the parent waiting forever,
    so a forward that does not land surfaces a 503 to the caller
    instead of a misleading 202. Here the terminal-ensure POST fails
    definitively (so the failure branch runs), and the subsequent
    status-forward POST raises a transport error (no runner reachable).

    This fails if the forward is best-effort (swallowed) rather than
    required: the route would return 202 with the parent never woken.
    """
    fake_runner = _FakeRunnerClient(
        responses={
            "/v1/sessions/conv_child_native/resources/terminals": (
                503,
                {
                    "error": {
                        "code": "native_terminal_start_failed",
                        "message": "Native Claude requires the 'claude' CLI on PATH.",
                    }
                },
            ),
        },
    )

    # Make only the status-forward POST fail at the transport layer.
    # The terminal-ensure POST must still return its canned 503 so the
    # failure branch is reached before the unreachable forward.
    original_post = fake_runner.post

    async def _post(url: str, *, json: Any = None, timeout: float | None = None) -> httpx.Response:
        """
        Forward to the canned client, but raise on the status forward.

        :param url: Request URL path.
        :param json: JSON body (an ``external_session_status`` edge on
            the forward POST).
        :param timeout: Request timeout (ignored).
        :returns: The canned response for non-forward POSTs.
        :raises httpx.ConnectError: On the parent-wake status forward.
        """
        if isinstance(json, dict) and json.get("type") == "external_session_status":
            fake_runner.post_json_calls.append((url, json))
            raise httpx.ConnectError("runner gone", request=httpx.Request("POST", url))
        return await original_post(url, json=json, timeout=timeout)

    fake_runner.post = _post  # type: ignore[method-assign]
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]
    set_runner_client(fake_runner)  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_child_native/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "review the PR"}],
            },
        },
    )

    # 503 RUNNER_UNAVAILABLE: the required parent-wake forward could not
    # be delivered, so the route must not pretend the message landed.
    assert resp.status_code == 503, resp.text
    # The forward was attempted on the child's events endpoint.
    assert fake_runner.post_json_calls[-1][0] == "/v1/sessions/conv_child_native/events"
    assert fake_runner.post_json_calls[-1][1]["type"] == "external_session_status"
    assert fake_runner.post_json_calls[-1][1]["data"]["status"] == "failed"


# ── Phase 1b: typed collections & single-resource proxy tests ────


def _env_only_payload() -> dict[str, object]:
    """Runner response for GET /resources/environments."""
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_ENVIRONMENT_ID,
                "object": "session.resource",
                "type": "environment",
                "session_id": "conv_proxy",
                "name": "Primary environment",
                "metadata": {"environment_type": "caller_process", "role": "primary"},
            },
        ],
        "first_id": DEFAULT_ENVIRONMENT_ID,
        "last_id": DEFAULT_ENVIRONMENT_ID,
        "has_more": False,
    }


def _single_resource_payload() -> dict[str, object]:
    """Runner response for GET /resources/{id}."""
    return {
        "id": DEFAULT_ENVIRONMENT_ID,
        "object": "session.resource",
        "type": "environment",
        "session_id": "conv_proxy",
        "name": "Primary environment",
        "metadata": {"environment_type": "caller_process", "role": "primary"},
    }


@pytest.mark.asyncio
async def test_list_environments_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/environments validates session then proxies."""
    fake_runner = _FakeRunnerClient(payload=_env_only_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get("/v1/sessions/conv_proxy/resources/environments")

    assert resp.status_code == 200
    assert resp.json()["object"] == "list"
    assert fake_runner.calls == [
        ("GET", "/v1/sessions/conv_proxy/resources/environments"),
    ]


@pytest.mark.asyncio
async def test_list_terminals_forwards_pagination_params_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/terminals forwards order/limit to the runner.

    The web terminal tabs request ``order=asc`` so the session's own
    terminal (created first, e.g. claude-native's ``claude/main``)
    stays in the first tab slot; a proxy that drops the query string
    silently re-applies the runner's ``desc`` default and flips the tab
    order on every page refresh.
    """
    fake_runner = _FakeRunnerClient(payload={"object": "list", "data": []})
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/terminals?order=asc&limit=1000&bogus=1",
    )

    assert resp.status_code == 200
    assert fake_runner.calls == [
        ("GET", "/v1/sessions/conv_proxy/resources/terminals"),
    ]
    # Exactly the runner endpoint's supported pagination params are
    # forwarded; an unknown param (``bogus``) must be dropped so the
    # proxy can't be used to smuggle arbitrary query strings. A ``None``
    # here means the proxy dropped the whole query string — the
    # refresh-flips-tab-order regression.
    assert fake_runner.get_params == [{"order": "asc", "limit": "1000"}]


@pytest.mark.asyncio
async def test_list_environments_rejects_unknown_session(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/environments 404s for nonexistent session."""
    fake_runner = _FakeRunnerClient(payload=_env_only_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get("/v1/sessions/conv_missing/resources/environments")

    assert resp.status_code == 404
    assert not fake_runner.calls


@pytest.mark.asyncio
async def test_get_resource_by_id_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/{id} validates session then proxies."""
    fake_runner = _FakeRunnerClient(payload=_single_resource_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(f"/v1/sessions/conv_proxy/resources/{DEFAULT_ENVIRONMENT_ID}")

    assert resp.status_code == 200
    assert resp.json()["id"] == DEFAULT_ENVIRONMENT_ID
    assert fake_runner.calls == [
        ("GET", f"/v1/sessions/conv_proxy/resources/{DEFAULT_ENVIRONMENT_ID}"),
    ]


@pytest.mark.asyncio
async def test_get_resource_by_id_404_from_runner(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/{id} surfaces runner 404."""
    fake_runner = _FakeRunnerClient(
        responses={
            "/v1/sessions/conv_proxy/resources/nonexistent": (
                404,
                {"error": {"code": "not_found", "message": "Resource 'nonexistent' not found"}},
            ),
        },
    )
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get("/v1/sessions/conv_proxy/resources/nonexistent")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.fixture
def bash_terminal_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the session's agent spec to one declaring a ``bash`` terminal.

    The terminal-create route gates user-initiated creates on the
    names in the spec's ``terminals:`` block. These route tests run
    with a stub agent store (no real bundle to load), so the module's
    spec loader is patched to return a minimal spec declaring
    ``bash`` — the name the create tests request.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.spec.types import AgentSpec

    spec = AgentSpec(spec_version=1, terminals={"bash": TerminalEnvSpec(command="bash")})
    monkeypatch.setattr(
        sessions_module,
        "_load_agent_spec_for_session",
        lambda conv, agent_store: spec,
    )


@pytest.mark.asyncio
async def test_create_terminal_proxies_to_runner(
    client: httpx.AsyncClient,
    bash_terminal_spec: None,
) -> None:
    """POST /resources/terminals validates session then proxies."""
    terminal_resource = {
        "id": "terminal_bash_s1",
        "object": "session.resource",
        "type": "terminal",
        "session_id": "conv_proxy",
        "name": "bash:s1",
        "environment": DEFAULT_ENVIRONMENT_ID,
        "metadata": {
            "terminal_name": "bash",
            "session_key": "s1",
            "running": True,
        },
    }
    fake_runner = _FakeRunnerClient(payload=terminal_resource)
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_proxy/resources/terminals",
        json={"terminal": "bash", "session_key": "s1"},
    )

    assert resp.status_code == 200
    assert resp.json()["id"] == "terminal_bash_s1"
    assert fake_runner.calls == [
        ("POST", "/v1/sessions/conv_proxy/resources/terminals"),
    ]


@pytest.mark.asyncio
async def test_create_terminal_rejected_without_agent_terminal_access(
    client: httpx.AsyncClient,
) -> None:
    """User creates are rejected when the agent declares no terminals.

    The stub agent store resolves no agent, so the session has no
    spec and therefore no ``terminals:`` block — the iff gate must
    refuse the create instead of letting the runner synthesize an
    arbitrary terminal the agent can't see or manage.
    """
    fake_runner = _FakeRunnerClient(payload={})
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_proxy/resources/terminals",
        json={"terminal": "bash", "session_key": "s1"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
    # The gate fired BEFORE the proxy — a recorded call here means an
    # unauthorized launch reached the runner despite the 400.
    assert fake_runner.calls == []


@pytest.mark.asyncio
async def test_create_terminal_rejected_for_undeclared_name(
    client: httpx.AsyncClient,
    bash_terminal_spec: None,
) -> None:
    """User creates must request a terminal name declared by the spec.

    The agent declares only ``bash``; requesting ``zsh`` would hit the
    runner's synthesize-from-body path, producing a terminal outside
    the operator-declared set — the gate must refuse it.
    """
    fake_runner = _FakeRunnerClient(payload={})
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_proxy/resources/terminals",
        json={"terminal": "zsh", "session_key": "s1"},
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_input"
    # The message names the declared set so a UI/user can self-correct.
    assert "bash" in body["error"]["message"]
    assert fake_runner.calls == []


@pytest.mark.asyncio
async def test_create_terminal_native_bootstrap_exempt_from_gate(
    client: httpx.AsyncClient,
) -> None:
    """``ensure_native_terminal`` requests bypass the declared-name gate.

    The ``omnigent claude`` / ``codex`` wrappers launch the session's
    own CLI terminal under undeclared names (``"claude"`` /
    ``"codex"``); gating them would break every native session boot.
    No spec resolves here (stub agent store), so a recorded proxy call
    proves the exemption — without it this request would 400 like the
    ungated test above.
    """
    terminal_resource = {
        "id": "terminal_claude_main",
        "object": "session.resource",
        "type": "terminal",
        "session_id": "conv_proxy",
        "name": "claude:main",
        "metadata": {"terminal_name": "claude", "session_key": "main", "running": True},
    }
    fake_runner = _FakeRunnerClient(payload=terminal_resource)
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_proxy/resources/terminals",
        json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
    )

    assert resp.status_code == 200
    assert resp.json()["id"] == "terminal_claude_main"
    assert fake_runner.calls == [
        ("POST", "/v1/sessions/conv_proxy/resources/terminals"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        # Marker with an arbitrary terminal name: the bootstrap shape
        # is claude/codex only.
        pytest.param(
            {"terminal": "zsh", "session_key": "main", "bridge_inject_dir": True},
            id="bridge-inject-arbitrary-name",
        ),
        # Marker with the right name but a non-main session key: the
        # wrappers only ever bootstrap the session's own main terminal.
        pytest.param(
            {"terminal": "claude", "session_key": "s2", "ensure_native_terminal": True},
            id="ensure-non-main-key",
        ),
    ],
)
async def test_create_terminal_bootstrap_markers_do_not_bypass_gate_for_other_shapes(
    client: httpx.AsyncClient,
    body: dict[str, Any],
) -> None:
    """Client-controlled markers can't skip the gate for arbitrary launches.

    The exemption markers ride the JSON body, so any LEVEL_EDIT caller
    can set them. Without the claude/codex + main narrowing, a caller
    could launch ANY terminal name (with bridge injection) on a
    no-terminal-access agent just by adding the flag — the W5
    operator-restriction bypass flagged in review. No spec resolves
    here (stub agent store), so a 400 with no recorded runner call
    proves the marker alone no longer opens the gate.
    """
    fake_runner = _FakeRunnerClient(payload={})
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_proxy/resources/terminals",
        json=body,
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
    # Gate fired before the proxy — a recorded call means the marker
    # bypassed the gate and the unauthorized launch reached the runner.
    assert fake_runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner_status,runner_payload,expected_status,expected_code,expected_message",
    [
        # Runner returns its own error body (the common case: tunnel
        # dropped mid-launch, runner reports offline). The runner's code
        # drives the HTTP status, and its message is surfaced verbatim.
        (
            503,
            {
                "error": {
                    "code": ErrorCode.RUNNER_UNAVAILABLE,
                    "message": "tunnel closed before request completed",
                }
            },
            503,
            ErrorCode.RUNNER_UNAVAILABLE,
            "tunnel closed before request completed",
        ),
        # Runner returns an error with no body — fall back to
        # INTERNAL_ERROR (→500) and a message carrying the raw status.
        (
            500,
            {},
            500,
            ErrorCode.INTERNAL_ERROR,
            "Terminal launch failed (runner returned HTTP 500)",
        ),
    ],
)
async def test_create_terminal_surfaces_runner_error_without_crashing(
    client: httpx.AsyncClient,
    bash_terminal_spec: None,
    runner_status: int,
    runner_payload: dict[str, Any],
    expected_status: int,
    expected_code: str,
    expected_message: str,
) -> None:
    """A runner ``>=400`` on terminal launch yields a clean error, not a 500 crash.

    Regression for the masking bug at ``create_session_terminal``: it built
    ``OmnigentError(..., http_status=status)``, but ``OmnigentError``
    has no ``http_status`` arg (it is a derived property), so any runner
    error turned into an unhandled ``TypeError`` instead of a legible error.
    With the bug present, ``client.post`` below raises ``TypeError`` rather
    than returning a response, so this test errors out — the failure signal.

    :param runner_status: HTTP status the fake runner returns for the
        terminal-launch proxy POST, e.g. ``503``.
    :param runner_payload: JSON body the fake runner returns, e.g.
        ``{"error": {"code": "runner_unavailable", "message": "..."}}``.
    :param expected_status: HTTP status the route should surface (derived
        from the error code), e.g. ``503``.
    :param expected_code: Machine-readable error code the route should
        surface, e.g. ``"runner_unavailable"``.
    :param expected_message: The human-readable message the route should
        surface.
    :returns: None.
    """
    path = "/v1/sessions/conv_proxy/resources/terminals"
    fake_runner = _FakeRunnerClient(responses={path: (runner_status, runner_payload)})
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.post(path, json={"terminal": "bash", "session_key": "s1"})

    # http_status is derived from the surfaced code; 503 here proves the
    # runner's ``runner_unavailable`` propagated (a 500 would mean it was
    # masked by the old TypeError path or a generic internal error).
    assert resp.status_code == expected_status
    body = resp.json()
    # The runner's own code/message reach the client unchanged — proves the
    # error was surfaced, not swallowed or replaced by a generic 500.
    assert body["error"]["code"] == expected_code
    assert body["error"]["message"] == expected_message
    # The launch failed, so no resource-created event/resource is returned.
    assert "id" not in body
    # The proxy POST was actually attempted before the error was raised.
    assert fake_runner.calls == [("POST", path)]


@pytest.mark.asyncio
async def test_delete_terminal_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """DELETE /resources/terminals/{id} validates session then proxies."""
    deletion = {
        "id": "terminal_bash_s1",
        "object": "session.resource.deleted",
        "deleted": True,
    }
    fake_runner = _FakeRunnerClient(payload=deletion)
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.delete(
        "/v1/sessions/conv_proxy/resources/terminals/terminal_bash_s1",
    )

    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert fake_runner.calls == [
        ("DELETE", "/v1/sessions/conv_proxy/resources/terminals/terminal_bash_s1"),
    ]


@pytest.mark.asyncio
async def test_transfer_terminal_authorizes_sessions_and_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """POST terminal transfer validates source and target then proxies."""
    terminal_resource = {
        "id": "terminal_bash_s1",
        "object": "session.resource",
        "type": "terminal",
        "session_id": "conv_local",
        "name": "bash:s1",
        "environment": DEFAULT_ENVIRONMENT_ID,
        "metadata": {
            "terminal_name": "bash",
            "session_key": "s1",
            "running": True,
        },
    }
    fake_runner = _FakeRunnerClient(payload=terminal_resource)
    router = _FakeRunnerRouter(fake_runner)
    set_runner_router(router)  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_proxy/resources/terminals/terminal_bash_s1/transfer",
        json={"target_session_id": "conv_local"},
    )

    assert resp.status_code == 200
    assert resp.json()["session_id"] == "conv_local"
    assert fake_runner.calls == [
        ("POST", "/v1/sessions/conv_proxy/resources/terminals/terminal_bash_s1/transfer"),
    ]
    assert fake_runner.post_json_calls == [
        (
            "/v1/sessions/conv_proxy/resources/terminals/terminal_bash_s1/transfer",
            {"target_session_id": "conv_local"},
        ),
    ]
    assert router.resource_calls == ["conv_proxy"]


@pytest.mark.asyncio
async def test_delete_terminal_surfaces_runner_404(
    client: httpx.AsyncClient,
) -> None:
    """DELETE /resources/terminals/{id} surfaces runner 404."""
    fake_runner = _FakeRunnerClient(
        responses={
            "/v1/sessions/conv_proxy/resources/terminals/terminal_nope_s1": (
                404,
                {"error": {"code": "not_found", "message": "Terminal not found"}},
            ),
        },
    )
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.delete(
        "/v1/sessions/conv_proxy/resources/terminals/terminal_nope_s1",
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# ── Phase 1c: session-scoped file endpoint tests ────────────────


class _InMemoryArtifactStore:
    """Minimal artifact store backed by a dict for tests."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        """Store bytes."""
        self._blobs[key] = data

    def get(self, key: str) -> bytes:
        """Retrieve bytes."""
        return self._blobs[key]

    def delete(self, key: str) -> None:
        """Remove bytes."""
        self._blobs.pop(key, None)


@pytest.fixture
def file_conv_store() -> _ConversationStore:
    """Shared conversation store for file tests."""
    return _ConversationStore()


@pytest.fixture
def file_store(db_uri: str) -> Any:
    """Real SqlAlchemy file store for file endpoint tests."""
    from omnigent.stores.file_store.sqlalchemy_store import (
        SqlAlchemyFileStore,
    )

    return SqlAlchemyFileStore(db_uri)


@pytest.fixture
def artifact_store() -> _InMemoryArtifactStore:
    """In-memory artifact store for file endpoint tests."""
    return _InMemoryArtifactStore()


@pytest.fixture
def file_app(
    runner_globals_reset: None,
    file_conv_store: _ConversationStore,
    file_store: Any,
    artifact_store: _InMemoryArtifactStore,
) -> FastAPI:
    """FastAPI app with real file + artifact stores for file tests."""
    del runner_globals_reset

    test_app = FastAPI()

    @test_app.exception_handler(OmnigentError)
    async def _handle(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {"code": exc.code, "message": exc.message},
            },
        )

    test_app.include_router(
        create_sessions_router(
            file_conv_store,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]  — stub agent store
            file_store=file_store,  # type: ignore[arg-type]
            artifact_store=artifact_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return test_app


@pytest.fixture
async def file_client(
    file_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """httpx client pointed at the file-capable test app."""
    transport = httpx.ASGITransport(app=file_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://server",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_upload_and_list_session_files(
    file_client: httpx.AsyncClient,
) -> None:
    """POST + GET /resources/files round-trips through server."""
    resp = await file_client.post(
        "/v1/sessions/conv_proxy/resources/files",
        files={"file": ("report.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["object"] == "session.resource"
    assert body["type"] == "file"
    assert body["session_id"] == "conv_proxy"
    assert body["name"] == "report.txt"
    file_id = body["id"]

    list_resp = await file_client.get(
        "/v1/sessions/conv_proxy/resources/files",
    )
    assert list_resp.status_code == 200
    ids = [f["id"] for f in list_resp.json()["data"]]
    assert file_id in ids


@pytest.mark.asyncio
async def test_get_session_file_validates_ownership(
    file_client: httpx.AsyncClient,
) -> None:
    """GET /resources/files/{id} 404s for wrong session."""
    upload = await file_client.post(
        "/v1/sessions/conv_proxy/resources/files",
        files={"file": ("owned.txt", b"data", "text/plain")},
    )
    file_id = upload.json()["id"]

    resp = await file_client.get(
        f"/v1/sessions/conv_local/resources/files/{file_id}",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_session_file_content(
    file_client: httpx.AsyncClient,
) -> None:
    """GET /resources/files/{id}/content returns raw bytes."""
    upload = await file_client.post(
        "/v1/sessions/conv_proxy/resources/files",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )
    file_id = upload.json()["id"]

    resp = await file_client.get(
        f"/v1/sessions/conv_proxy/resources/files/{file_id}/content",
    )
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    # The content route must force a download and forbid MIME sniffing
    # so a browser never renders user-uploaded bytes as an active type
    # in the server's origin. A failure here means the stored-XSS
    # hardening was dropped and the response is renderable inline again.
    assert resp.headers["content-disposition"] == (
        "attachment; filename=\"hello.txt\"; filename*=UTF-8''hello.txt"
    )
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_download_session_file_html_is_attachment_not_inline(
    file_client: httpx.AsyncClient,
) -> None:
    """An uploaded .html must be served as a download, not rendered inline.

    Reproduces the stored-XSS vector: a user uploads ``evil.html``
    with a ``<script>`` body. Without the hardening the route would
    return ``Content-Type: text/html`` with no disposition, letting a
    browser execute the script in the server origin. The disposition +
    nosniff headers neutralize that.
    """
    upload = await file_client.post(
        "/v1/sessions/conv_proxy/resources/files",
        files={
            "file": (
                "evil.html",
                b"<script>alert(document.domain)</script>",
                "text/html",
            ),
        },
    )
    file_id = upload.json()["id"]

    resp = await file_client.get(
        f"/v1/sessions/conv_proxy/resources/files/{file_id}/content",
    )
    assert resp.status_code == 200
    # The bytes are still served verbatim — we don't mangle content,
    # we only change how the browser is told to handle them.
    assert resp.content == b"<script>alert(document.domain)</script>"
    # attachment => browser downloads instead of rendering the script.
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert 'filename="evil.html"' in resp.headers["content-disposition"]
    # nosniff => the browser won't second-guess the declared type.
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_delete_session_file(
    file_client: httpx.AsyncClient,
) -> None:
    """DELETE /resources/files/{id} removes the file."""
    upload = await file_client.post(
        "/v1/sessions/conv_proxy/resources/files",
        files={"file": ("temp.txt", b"gone", "text/plain")},
    )
    file_id = upload.json()["id"]

    resp = await file_client.delete(
        f"/v1/sessions/conv_proxy/resources/files/{file_id}",
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    get_resp = await file_client.get(
        f"/v1/sessions/conv_proxy/resources/files/{file_id}",
    )
    assert get_resp.status_code == 404


# ── files:copy — lineage-scoped file copy tests ─────────────────


async def _upload_file(
    client: httpx.AsyncClient,
    session_id: str,
    name: str,
    content: bytes,
) -> str:
    """Upload a file to a session and return its id.

    :param client: The file-capable test client.
    :param session_id: Owning session id.
    :param name: Filename to upload.
    :param content: Raw file bytes.
    :returns: The new file id.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": (name, content, "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_copy_files_from_direct_parent(
    file_client: httpx.AsyncClient,
    file_store: Any,
) -> None:
    """A child copies one parent-owned file into its own namespace."""
    parent_file = await _upload_file(file_client, "conv_p", "doc.txt", b"parent bytes")

    resp = await file_client.post(
        "/v1/sessions/conv_c/resources/files:copy",
        json={"source_session_id": "conv_p", "file_ids": [parent_file]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "session.files.copied"
    assert body["session_id"] == "conv_c"
    new_id = body["mapping"][parent_file]
    # A copy, not an alias: the new row is distinct from the source.
    assert new_id != parent_file

    # The new row is child-scoped and readable by the child only.
    copied = file_store.get(new_id, session_id="conv_c")
    assert copied is not None
    assert copied.filename == "doc.txt"
    # The source row is unchanged and still owned by the parent.
    assert file_store.get(parent_file, session_id="conv_p") is not None

    # The bytes match the source.
    content = await file_client.get(
        f"/v1/sessions/conv_c/resources/files/{new_id}/content",
    )
    assert content.status_code == 200
    assert content.content == b"parent bytes"


@pytest.mark.asyncio
async def test_copy_files_multi_file_mapping(
    file_client: httpx.AsyncClient,
    file_store: Any,
) -> None:
    """A multi-file copy returns a complete source→new mapping."""
    f1 = await _upload_file(file_client, "conv_p", "a.txt", b"aaa")
    f2 = await _upload_file(file_client, "conv_p", "b.txt", b"bbb")

    resp = await file_client.post(
        "/v1/sessions/conv_c/resources/files:copy",
        json={"source_session_id": "conv_p", "file_ids": [f1, f2]},
    )
    assert resp.status_code == 200, resp.text
    mapping = resp.json()["mapping"]
    assert set(mapping.keys()) == {f1, f2}
    assert len(set(mapping.values())) == 2
    for src, new_id in mapping.items():
        del src
        assert file_store.get(new_id, session_id="conv_c") is not None


@pytest.mark.asyncio
async def test_copy_files_from_grandparent_in_chain(
    file_client: httpx.AsyncClient,
    file_store: Any,
) -> None:
    """Copying from a grandparent (two levels up) is authorized."""
    gp_file = await _upload_file(file_client, "conv_gp", "root.txt", b"grandparent")

    resp = await file_client.post(
        "/v1/sessions/conv_c/resources/files:copy",
        json={"source_session_id": "conv_gp", "file_ids": [gp_file]},
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["mapping"][gp_file]
    assert file_store.get(new_id, session_id="conv_c") is not None


@pytest.mark.asyncio
async def test_copy_files_rejects_source_outside_lineage(
    file_client: httpx.AsyncClient,
    file_store: Any,
) -> None:
    """A source session not in the destination's lineage is forbidden."""
    # conv_proxy is a top-level session with no link to conv_c's chain.
    foreign_file = await _upload_file(file_client, "conv_proxy", "x.txt", b"foreign")

    before = file_store.list(session_id="conv_c").data

    resp = await file_client.post(
        "/v1/sessions/conv_c/resources/files:copy",
        json={"source_session_id": "conv_proxy", "file_ids": [foreign_file]},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"

    # Nothing was copied into the destination.
    after = file_store.list(session_id="conv_c").data
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_copy_files_missing_source_file_is_all_or_nothing(
    file_client: httpx.AsyncClient,
    file_store: Any,
) -> None:
    """A missing source file fails the whole request with no partial copy."""
    good = await _upload_file(file_client, "conv_p", "good.txt", b"ok")

    before = file_store.list(session_id="conv_c").data

    resp = await file_client.post(
        "/v1/sessions/conv_c/resources/files:copy",
        json={"source_session_id": "conv_p", "file_ids": [good, "file_does_not_exist"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"

    # The valid file in the batch must NOT have been committed: validation
    # is all-or-nothing, so the destination is unchanged.
    after = file_store.list(session_id="conv_c").data
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_copy_files_self_source_is_rejected(
    file_client: httpx.AsyncClient,
    file_store: Any,
) -> None:
    """A session may not name ITSELF as the source — lineage is ancestors only."""
    own = await _upload_file(file_client, "conv_c", "self.txt", b"mine")
    before = file_store.list(session_id="conv_c").data

    resp = await file_client.post(
        "/v1/sessions/conv_c/resources/files:copy",
        json={"source_session_id": "conv_c", "file_ids": [own]},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    # Nothing copied — the destination is unchanged.
    after = file_store.list(session_id="conv_c").data
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_copy_files_then_download_returns_bytes(
    file_client: httpx.AsyncClient,
) -> None:
    """After copy, the child can download the copied content."""
    parent_file = await _upload_file(file_client, "conv_p", "payload.bin", b"\x00\x01\x02data")

    copy_resp = await file_client.post(
        "/v1/sessions/conv_c/resources/files:copy",
        json={"source_session_id": "conv_p", "file_ids": [parent_file]},
    )
    new_id = copy_resp.json()["mapping"][parent_file]

    resp = await file_client.get(
        f"/v1/sessions/conv_c/resources/files/{new_id}/content",
    )
    assert resp.status_code == 200
    assert resp.content == b"\x00\x01\x02data"


# ── Phase 1d: integration hardening tests ────────────────────────


@pytest.mark.asyncio
async def test_files_route_not_captured_as_resource_id(
    file_client: httpx.AsyncClient,
) -> None:
    """'files' is a typed collection route, not a resource id."""
    resp = await file_client.get(
        "/v1/sessions/conv_proxy/resources/files",
    )
    assert resp.status_code == 200
    assert resp.json()["object"] == "list"


@pytest.mark.asyncio
async def test_files_appear_in_unified_inventory(
    file_client: httpx.AsyncClient,
) -> None:
    """Uploaded files appear in GET /resources with type 'file'."""
    upload = await file_client.post(
        "/v1/sessions/conv_local/resources/files",
        files={"file": ("report.txt", b"data", "text/plain")},
    )
    file_id = upload.json()["id"]

    resp = await file_client.get("/v1/sessions/conv_local/resources")
    assert resp.status_code == 200
    body = resp.json()
    ids = [r["id"] for r in body["data"]]
    assert file_id in ids
    file_resource = next(r for r in body["data"] if r["id"] == file_id)
    assert file_resource["type"] == "file"
    assert file_resource["object"] == "session.resource"
    assert file_resource["session_id"] == "conv_local"


@pytest.mark.asyncio
async def test_delete_for_session_cleans_up_all_files(
    file_client: httpx.AsyncClient,
    file_store: Any,
) -> None:
    """delete_all_for_session removes all session files."""
    for name in ("a.txt", "b.txt", "c.txt"):
        await file_client.post(
            "/v1/sessions/conv_proxy/resources/files",
            files={"file": (name, b"data", "text/plain")},
        )

    list_resp = await file_client.get(
        "/v1/sessions/conv_proxy/resources/files",
    )
    assert len(list_resp.json()["data"]) == 3

    # Use the injected file_store fixture directly — no closure
    # introspection needed.
    deleted_ids = file_store.delete_all_for_session("conv_proxy")
    assert len(deleted_ids) == 3

    list_after = await file_client.get(
        "/v1/sessions/conv_proxy/resources/files",
    )
    assert len(list_after.json()["data"]) == 0


def test_resource_lifecycle_event_schemas_in_union() -> None:
    """Session resource events are part of the ServerStreamEvent union."""
    from pydantic import TypeAdapter

    from omnigent.server.schemas import (
        ServerStreamEvent,
        SessionResourceCreatedEvent,
        SessionResourceDeletedEvent,
    )

    adapter = TypeAdapter(ServerStreamEvent)

    created = adapter.validate_python(
        {
            "type": "session.resource.created",
            "resource": {
                "id": "file_abc",
                "object": "session.resource",
                "type": "file",
                "session_id": "conv_1",
                "name": "test.txt",
                "metadata": {},
            },
        }
    )
    assert isinstance(created, SessionResourceCreatedEvent)
    assert created.resource["id"] == "file_abc"

    deleted = adapter.validate_python(
        {
            "type": "session.resource.deleted",
            "resource_id": "terminal_bash_s1",
            "resource_type": "terminal",
            "session_id": "conv_1",
        }
    )
    assert isinstance(deleted, SessionResourceDeletedEvent)
    assert deleted.resource_id == "terminal_bash_s1"


# ── Server filesystem proxy tests ────────────────────────────────


def _fs_list_payload() -> dict[str, object]:
    """Canned runner response for filesystem directory listing."""
    return {
        "object": "list",
        "data": [
            {
                "id": "README.md",
                "object": "session.environment.filesystem.entry",
                "name": "README.md",
                "path": "README.md",
                "type": "file",
                "bytes": 100,
                "modified_at": 1700000000,
            },
        ],
        "first_id": "README.md",
        "last_id": "README.md",
        "has_more": False,
    }


def _fs_write_payload() -> dict[str, object]:
    """Canned runner response for filesystem write."""
    return {
        "object": "session.environment.filesystem.write_result",
        "operation": "write",
        "path": "new.txt",
        "created": True,
        "bytes_written": 5,
        "entry": None,
    }


def _fs_edit_payload() -> dict[str, object]:
    """Canned runner response for filesystem edit."""
    return {
        "object": "session.environment.filesystem.edit_result",
        "operation": "edit",
        "path": "hello.txt",
        "replacements": 1,
        "bytes_before": 11,
        "bytes_after": 13,
        "entry": None,
    }


def _fs_delete_payload() -> dict[str, object]:
    """Canned runner response for filesystem delete."""
    return {
        "object": "session.environment.filesystem.delete_result",
        "operation": "delete",
        "path": "old.txt",
        "deleted": True,
        "type": "file",
        "bytes_deleted": 42,
        "entries_deleted": None,
    }


@pytest.mark.asyncio
async def test_filesystem_list_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """GET /environments/{id}/filesystem proxies to runner with default pagination params."""
    fake_runner = _FakeRunnerClient(payload=_fs_list_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem",
    )
    assert resp.status_code == 200
    assert resp.json()["object"] == "list"
    # Default limit=20 and order=desc are forwarded; no cursor params when absent.
    assert fake_runner.calls == [
        (
            "GET",
            "/v1/sessions/conv_proxy/resources/environments/default/filesystem?limit=20&order=desc",
        ),
    ]


@pytest.mark.asyncio
async def test_filesystem_list_forwards_custom_limit_and_order(
    client: httpx.AsyncClient,
) -> None:
    """GET /environments/{id}/filesystem forwards limit and order to runner."""
    fake_runner = _FakeRunnerClient(payload=_fs_list_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem?limit=1000&order=asc",
    )

    assert resp.status_code == 200
    # limit=1000 and order=asc must reach the runner — they are not silently capped or ignored.
    assert fake_runner.calls == [
        (
            "GET",
            "/v1/sessions/conv_proxy/resources/environments/default/filesystem?limit=1000&order=asc",
        ),
    ]


@pytest.mark.asyncio
async def test_filesystem_list_forwards_after_cursor(
    client: httpx.AsyncClient,
) -> None:
    """GET /environments/{id}/filesystem forwards `after` cursor to runner."""
    fake_runner = _FakeRunnerClient(payload=_fs_list_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem"
        "?limit=100&order=asc&after=README.md",
    )

    assert resp.status_code == 200
    forwarded_url = fake_runner.calls[0][1]
    # after=README.md must be included — without it the runner would return the
    # first page again instead of the next page.
    assert "after=README.md" in forwarded_url
    assert "limit=100" in forwarded_url
    assert "order=asc" in forwarded_url


@pytest.mark.asyncio
async def test_filesystem_list_forwards_before_cursor(
    client: httpx.AsyncClient,
) -> None:
    """GET /environments/{id}/filesystem forwards `before` cursor to runner."""
    fake_runner = _FakeRunnerClient(payload=_fs_list_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem"
        "?limit=50&order=asc&before=src",
    )

    assert resp.status_code == 200
    forwarded_url = fake_runner.calls[0][1]
    # before=src must be included — without it the runner would not constrain
    # the upper bound of the page window.
    assert "before=src" in forwarded_url


@pytest.mark.asyncio
async def test_filesystem_list_omits_absent_cursors(
    client: httpx.AsyncClient,
) -> None:
    """GET /environments/{id}/filesystem does not forward after/before when absent."""
    fake_runner = _FakeRunnerClient(payload=_fs_list_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem?limit=50",
    )

    assert resp.status_code == 200
    forwarded_url = fake_runner.calls[0][1]
    # after and before must be absent — including them as empty strings or
    # "None" would corrupt the runner's cursor parsing.
    assert "after" not in forwarded_url
    assert "before" not in forwarded_url


@pytest.mark.asyncio
async def test_filesystem_path_forwards_pagination_params(
    client: httpx.AsyncClient,
) -> None:
    """GET /environments/{id}/filesystem/{path} forwards pagination params
    for directory listing."""
    fake_runner = _FakeRunnerClient(payload=_fs_list_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem/src"
        "?limit=1000&order=asc",
    )

    assert resp.status_code == 200
    forwarded_url = fake_runner.calls[0][1]
    # limit=1000 and order=asc must reach the runner so subdirectory listings
    # are not silently capped at the default of 20.
    assert "limit=1000" in forwarded_url
    assert "order=asc" in forwarded_url


@pytest.mark.asyncio
async def test_filesystem_path_omits_absent_cursors(
    client: httpx.AsyncClient,
) -> None:
    """GET /environments/{id}/filesystem/{path} omits after/before when not provided."""
    fake_runner = _FakeRunnerClient(payload=_fs_list_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem/src"
        "?limit=100&order=asc",
    )

    assert resp.status_code == 200
    forwarded_url = fake_runner.calls[0][1]
    # Cursor params absent from the client request must not appear in the
    # proxied URL — "after=None" or "before=None" would break the runner.
    assert "after" not in forwarded_url
    assert "before" not in forwarded_url


@pytest.mark.asyncio
async def test_filesystem_read_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """GET /environments/{id}/filesystem/{path} proxies to runner."""
    payload = {
        "object": "session.environment.filesystem.file_content",
        "path": "hello.txt",
        "encoding": "utf-8",
        "content": "hello world",
        "bytes": 11,
        "truncated": False,
    }
    fake_runner = _FakeRunnerClient(payload=payload)
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem/hello.txt",
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "hello world"


@pytest.mark.asyncio
async def test_filesystem_write_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """PUT /environments/{id}/filesystem/{path} proxies to runner."""
    fake_runner = _FakeRunnerClient(payload=_fs_write_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.put(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem/new.txt",
        json={"content": "hello", "encoding": "utf-8"},
    )
    assert resp.status_code == 200
    assert resp.json()["created"] is True
    assert fake_runner.calls == [
        ("PUT", "/v1/sessions/conv_proxy/resources/environments/default/filesystem/new.txt"),
    ]


@pytest.mark.asyncio
async def test_filesystem_write_publishes_changed_files_invalidation(
    client: httpx.AsyncClient,
) -> None:
    """Successful filesystem writes publish a session filesystem invalidation."""
    fake_runner = _FakeRunnerClient(payload=_fs_write_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]
    stream = session_stream.subscribe("conv_proxy", ready_event={"type": "test.ready"})
    try:
        assert await anext(stream) == {"type": "test.ready"}

        resp = await client.put(
            "/v1/sessions/conv_proxy/resources/environments/default/filesystem/new.txt",
            json={"content": "hello", "encoding": "utf-8"},
        )
        event = await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await stream.aclose()

    assert resp.status_code == 200
    assert event == {
        "type": "session.changed_files.invalidated",
        "session_id": "conv_proxy",
        "environment_id": "default",
    }


@pytest.mark.asyncio
async def test_filesystem_edit_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """PATCH /environments/{id}/filesystem/{path} proxies to runner."""
    fake_runner = _FakeRunnerClient(payload=_fs_edit_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.patch(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem/hello.txt",
        json={"old_text": "hello", "new_text": "goodbye"},
    )
    assert resp.status_code == 200
    assert resp.json()["replacements"] == 1
    assert fake_runner.calls == [
        ("PATCH", "/v1/sessions/conv_proxy/resources/environments/default/filesystem/hello.txt"),
    ]


@pytest.mark.asyncio
async def test_filesystem_delete_proxies_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """DELETE /environments/{id}/filesystem/{path} proxies to runner."""
    fake_runner = _FakeRunnerClient(payload=_fs_delete_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.delete(
        "/v1/sessions/conv_proxy/resources/environments/default/filesystem/old.txt",
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


@pytest.mark.asyncio
async def test_filesystem_proxy_validates_session(
    client: httpx.AsyncClient,
) -> None:
    """Filesystem proxy rejects unknown sessions."""
    fake_runner = _FakeRunnerClient(payload=_fs_list_payload())
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.get(
        "/v1/sessions/conv_missing/resources/environments/default/filesystem",
    )
    assert resp.status_code == 404
    assert not fake_runner.calls


# ── Server shell proxy test ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_proxies_to_runner(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /environments/{id}/shell validates session then proxies."""
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        session_stream,
        "publish",
        lambda conversation_id, event: published.append((conversation_id, event)),
    )
    shell_payload = {
        "object": "session.environment.shell_result",
        "stdout": "hello\n",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "cwd": "/workspace",
    }
    fake_runner = _FakeRunnerClient(payload=shell_payload)
    set_runner_router(_FakeRunnerRouter(fake_runner))  # type: ignore[arg-type]

    resp = await client.post(
        "/v1/sessions/conv_proxy/resources/environments/default/shell",
        json={"command": "echo hello"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "session.environment.shell_result"
    assert body["stdout"] == "hello\n"
    assert body["exit_code"] == 0
    assert body["timed_out"] is False
    assert fake_runner.calls == [
        ("POST", "/v1/sessions/conv_proxy/resources/environments/default/shell"),
    ]
    assert published == []


# ── Conversation deletion cleanup test ───────────────────────────


@pytest.mark.asyncio
async def test_session_file_cleanup_on_delete(
    file_client: httpx.AsyncClient,
    file_store: Any,
    artifact_store: _InMemoryArtifactStore,
) -> None:
    """Session file cleanup removes metadata and artifact bytes."""
    upload = await file_client.post(
        "/v1/sessions/conv_proxy/resources/files",
        files={"file": ("cleanup.txt", b"session data", "text/plain")},
    )
    file_id = upload.json()["id"]
    assert artifact_store.get(file_id) is not None

    deleted_ids = file_store.delete_all_for_session("conv_proxy")
    assert file_id in deleted_ids
    for fid in deleted_ids:
        artifact_store.delete(fid)

    assert file_store.get(file_id, session_id="conv_proxy") is None
    assert file_id not in artifact_store._blobs


# ── Resource event persistence tests ─────────────────────────────


@pytest.mark.asyncio
async def test_file_upload_persists_resource_event(
    file_client: httpx.AsyncClient,
    file_conv_store: _ConversationStore,
) -> None:
    """Uploading a file persists a resource_event conversation item."""
    await file_client.post(
        "/v1/sessions/conv_proxy/resources/files",
        files={"file": ("persist.txt", b"data", "text/plain")},
    )

    events = [i for i in file_conv_store.appended_items if i.type == "resource_event"]
    assert len(events) == 1
    assert events[0].data.event_type == "session.resource.created"
    assert events[0].data.resource_type == "file"
    assert events[0].data.resource is not None


@pytest.mark.asyncio
async def test_file_delete_persists_resource_event(
    file_client: httpx.AsyncClient,
    file_conv_store: _ConversationStore,
) -> None:
    """Deleting a file persists a resource_event conversation item."""
    upload = await file_client.post(
        "/v1/sessions/conv_proxy/resources/files",
        files={"file": ("del.txt", b"gone", "text/plain")},
    )
    file_id = upload.json()["id"]
    file_conv_store.appended_items.clear()

    await file_client.delete(
        f"/v1/sessions/conv_proxy/resources/files/{file_id}",
    )

    events = [i for i in file_conv_store.appended_items if i.type == "resource_event"]
    assert len(events) == 1
    assert events[0].data.event_type == "session.resource.deleted"
    assert events[0].data.resource_id == file_id
    assert events[0].data.resource_type == "file"


class _FakeStreamCtx:
    """Async-context-manager body for ``_FakeStreamingRunnerClient.stream``.

    Yields the configured SSE frame strings in order, then ends — the
    relay loop returns when it reads the terminal ``[DONE]`` frame.

    :param frames: SSE frame strings (each already terminated by a
        blank line) to yield from ``aiter_text``.
    """

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    async def __aenter__(self) -> _FakeStreamCtx:
        """Enter the context, returning self as the response object."""
        return self

    async def __aexit__(self, *exc: object) -> bool:
        """Exit the context without suppressing exceptions."""
        return False

    async def aiter_text(self) -> AsyncIterator[str]:
        """Yield each configured SSE frame string in order."""
        for frame in self._frames:
            yield frame


class _FakeStreamingRunnerClient:
    """Runner-client stub whose ``stream`` yields fixed SSE frames.

    Real stub class (not ``MagicMock``) so an unexpected method call
    raises ``AttributeError`` loudly instead of silently passing.

    :param frames: SSE frame strings the relay will consume from the
        runner's ``GET /v1/sessions/{id}/stream``.
    """

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.stream_urls: list[str] = []

    def stream(self, method: str, url: str, timeout: object = None) -> _FakeStreamCtx:
        """Record the URL and return the canned streaming context."""
        del method, timeout
        self.stream_urls.append(url)
        return _FakeStreamCtx(self._frames)


def _sse_frame(payload: dict[str, Any]) -> str:
    """Encode one SSE ``data:`` frame from a JSON payload.

    :param payload: The event dict to JSON-encode.
    :returns: A complete SSE frame string ending in a blank line.
    """
    import json

    return "data: " + json.dumps(payload) + "\n\n"


class _ScriptedStreamCtx:
    """Stream context that runs side-effect hooks between SSE frames.

    Each step is either an SSE frame string (yielded to the relay) or a
    zero-arg callable (run between frames). Because the relay fully
    processes one frame before pulling the next chunk, a callable step
    runs at a deterministic point — after every prior frame's processing
    and before any later frame's — which is how these tests install the
    stop fence "mid-turn" exactly the way the ``POST /events`` route does.

    :param steps: Ordered frames and hooks to execute.
    """

    def __init__(self, steps: list[str | Callable[[], None]]) -> None:
        self._steps = steps

    async def __aenter__(self) -> _ScriptedStreamCtx:
        """Enter the context, returning self as the response object."""
        return self

    async def __aexit__(self, *exc: object) -> bool:
        """Exit the context without suppressing exceptions."""
        return False

    async def aiter_text(self) -> AsyncIterator[str]:
        """Yield frame steps in order, running callable steps in between."""
        for step in self._steps:
            if callable(step):
                step()
            else:
                yield step


class _ScriptedStreamingRunnerClient:
    """Runner-client stub whose ``stream`` interleaves frames and hooks.

    Real stub class (not ``MagicMock``) so an unexpected method call
    raises ``AttributeError`` loudly instead of silently passing.

    :param steps: SSE frame strings and zero-arg hooks, in order — see
        :class:`_ScriptedStreamCtx`.
    """

    def __init__(self, steps: list[str | Callable[[], None]]) -> None:
        self._steps = steps

    def stream(self, method: str, url: str, timeout: object = None) -> _ScriptedStreamCtx:
        """Return the scripted streaming context."""
        del method, url, timeout
        return _ScriptedStreamCtx(self._steps)


@pytest.mark.asyncio
async def test_relay_persists_terminal_resource_created_from_runner() -> None:
    """The relay persists a ``resource_event`` for a runner-emitted create.

    An agent ``sys_terminal_launch`` makes the runner emit
    ``session.resource.created`` on its SSE stream. The Omnigent relay must
    persist a durable ``resource_event`` item so a client reconnecting
    mid-turn rediscovers the terminal in the snapshot — matching the
    REST resource path.
    """
    from omnigent.server.routes.sessions import _relay_runner_stream

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            _sse_frame(
                {
                    "type": "session.resource.created",
                    "resource": {
                        "id": "terminal_zsh_s1",
                        "type": "terminal",
                        "name": "zsh:s1",
                        "metadata": {"terminal_name": "zsh", "session_key": "s1"},
                    },
                }
            ),
            "data: [DONE]\n\n",
        ]
    )

    await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

    events = [i for i in store.appended_items if i.type == "resource_event"]
    # Exactly one durable item — the created event. Zero would mean the
    # relay dropped it (the bug this change fixes); more than one would
    # mean a non-resource frame leaked into the resource-event path.
    assert len(events) == 1, f"expected 1 resource_event, got {store.appended_items}"
    assert events[0].data.event_type == "session.resource.created"
    assert events[0].data.resource_id == "terminal_zsh_s1"
    assert events[0].data.resource_type == "terminal"
    # Persisted item carries the full resource dict for snapshot replay.
    assert events[0].data.resource is not None
    assert events[0].data.resource["id"] == "terminal_zsh_s1"
    # Resource events thread on the session id (matches the REST path).
    assert events[0].response_id == "conv_proxy"


@pytest.mark.asyncio
async def test_relay_persists_terminal_resource_deleted_from_runner() -> None:
    """The relay persists a ``resource_event`` for a runner-emitted delete.

    The symmetric teardown: an agent ``sys_terminal_close`` makes the
    runner emit ``session.resource.deleted``; the relay persists the
    durable delete item.
    """
    from omnigent.server.routes.sessions import _relay_runner_stream

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            _sse_frame(
                {
                    "type": "session.resource.deleted",
                    "resource_id": "terminal_zsh_s1",
                    "resource_type": "terminal",
                    "session_id": "conv_proxy",
                }
            ),
            "data: [DONE]\n\n",
        ]
    )

    await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

    events = [i for i in store.appended_items if i.type == "resource_event"]
    assert len(events) == 1, f"expected 1 resource_event, got {store.appended_items}"
    assert events[0].data.event_type == "session.resource.deleted"
    assert events[0].data.resource_id == "terminal_zsh_s1"
    assert events[0].data.resource_type == "terminal"
    # Delete carries no resource body.
    assert events[0].data.resource is None


@pytest.mark.asyncio
async def test_relay_persists_failed_status_error_labels_from_runner() -> None:
    """Runner ``session.status: failed`` error details survive reload."""
    from omnigent.server.routes.sessions import _relay_runner_stream

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            _sse_frame(
                {
                    "type": "session.status",
                    "status": "failed",
                    "error": {
                        "code": "required_terminal_exited",
                        "message": "Required terminal exited unexpectedly",
                    },
                }
            ),
            "data: [DONE]\n\n",
        ]
    )

    await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

    assert (
        store._conversations["conv_proxy"].labels["omnigent.last_task_error_code"]
        == "required_terminal_exited"
    )
    assert (
        store._conversations["conv_proxy"].labels["omnigent.last_task_error_message"]
        == "Required terminal exited unexpectedly"
    )


@pytest.mark.asyncio
async def test_relay_does_not_persist_session_level_response_error() -> None:
    """The relay does not persist a startup ``response.error`` orphan.

    A runner can emit ``response.error`` before any response lifecycle
    frame while auto-creating a native terminal. That is a session
    status signal, not a transcript turn. Persisting it would create a
    top-of-transcript error that is duplicated when the next user
    message fast-fails and records its own ordered ``message,error``
    pair.
    """
    from omnigent.server.routes.sessions import _relay_runner_stream

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            _sse_frame(
                {
                    "type": "response.error",
                    "source": "execution",
                    "error": {
                        "code": "native_terminal_start_failed",
                        "message": "Native Codex requires the 'codex' CLI on PATH.",
                    },
                }
            ),
            "data: [DONE]\n\n",
        ]
    )

    await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

    errors = [i for i in store.appended_items if i.type == "error"]
    assert errors == []


@pytest.mark.asyncio
async def test_relay_persists_in_turn_response_error_once_from_runner() -> None:
    """The relay durably stores an in-turn runner error banner once.

    Once a ``response.in_progress`` frame establishes the active turn,
    a following ``response.error`` is transcript-scoped. The relay
    persists that visible error payload so refresh shows the banner, but
    still dedupes identical frames so history is not spammed.
    """
    from omnigent.server.routes.sessions import _relay_runner_stream

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            _sse_frame(
                {
                    "type": "response.in_progress",
                    "response": {"id": "resp_turn"},
                }
            ),
            _sse_frame(
                {
                    "type": "response.error",
                    "source": "execution",
                    "error": {
                        "code": "native_terminal_start_failed",
                        "message": "Native Codex requires the 'codex' CLI on PATH.",
                    },
                }
            ),
            _sse_frame(
                {
                    "type": "response.error",
                    "source": "execution",
                    "error": {
                        "code": "native_terminal_start_failed",
                        "message": "Native Codex requires the 'codex' CLI on PATH.",
                    },
                }
            ),
            "data: [DONE]\n\n",
        ]
    )

    await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

    errors = [i for i in store.appended_items if i.type == "error"]
    # One durable error proves the first in-turn frame was persisted;
    # zero would mean refresh loses the banner, while two would mean
    # reconnect/retry frames can spam the transcript.
    assert len(errors) == 1, f"expected 1 durable error item, got {store.appended_items}"
    assert errors[0].response_id == "resp_turn"
    assert errors[0].data.source == "execution"
    assert errors[0].data.code == "native_terminal_start_failed"
    assert errors[0].data.message == "Native Codex requires the 'codex' CLI on PATH."


@pytest.mark.asyncio
async def test_relay_dedupes_duplicate_error_persistence_but_forwards_live_frames() -> None:
    """Duplicate runner errors are deduped only in durable history.

    Reconnect can make a runner re-emit the same startup failure. The
    relay still forwards the runner's live stream exactly, but it
    persists only the first matching banner until another user message
    starts a new transcript turn.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes.sessions import _relay_runner_stream

    published: list[dict[str, Any]] = []
    live_errors_seen = asyncio.Event()

    async def _consume() -> None:
        async for event in session_stream.subscribe("conv_proxy"):
            published.append(event)
            if len([item for item in published if item.get("type") == "response.error"]) == 2:
                live_errors_seen.set()

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    try:
        store = _ConversationStore()
        client = _FakeStreamingRunnerClient(
            [
                _sse_frame(
                    {
                        "type": "response.in_progress",
                        "response": {"id": "resp_turn"},
                    }
                ),
                _sse_frame(
                    {
                        "type": "response.error",
                        "source": "execution",
                        "error": {
                            "code": "native_terminal_start_failed",
                            "message": "Native Codex requires the 'codex' CLI on PATH.",
                        },
                    }
                ),
                _sse_frame(
                    {
                        "type": "response.error",
                        "source": "execution",
                        "error": {
                            "code": "native_terminal_start_failed",
                            "message": "Native Codex requires the 'codex' CLI on PATH.",
                        },
                    }
                ),
                "data: [DONE]\n\n",
            ]
        )

        await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]
        await asyncio.wait_for(live_errors_seen.wait(), timeout=1)
    finally:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer

    error_events = [event for event in published if event.get("type") == "response.error"]
    # Two live frames proves persistence dedupe does not alter the
    # producer stream; one would mean reconnect frames are being
    # suppressed before clients see them.
    assert len(error_events) == 2
    errors = [i for i in store.appended_items if i.type == "error"]
    # One durable item is the transcript dedupe invariant: zero loses
    # the refresh banner, while two duplicates the same runner failure.
    assert len(errors) == 1


@pytest.mark.asyncio
async def test_native_dispatch_fast_fails_and_consumes_message_on_terminal_error() -> None:
    """Definitive native terminal failure consumes the user message quickly.

    The Omnigent server first asks the runner to ensure the native terminal.
    When the runner returns a structured startup failure, Omnigent must not
    create a pending input or forward into the dead terminal. It should
    persist the user message plus a durable error item instead.
    """
    from omnigent.server.routes.sessions import _dispatch_session_event_to_runner

    store = _ConversationStore()
    conv = store.get_conversation("conv_claude")
    assert conv is not None
    client = _FakeRunnerClient(
        responses={
            "/v1/sessions/conv_claude/resources/terminals": (
                500,
                {
                    "error": {
                        "code": "native_terminal_start_failed",
                        "message": "Native Claude requires the 'claude' CLI on PATH.",
                    }
                },
            )
        }
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "try again"}]},
    )

    result = await _dispatch_session_event_to_runner(
        "conv_claude",
        conv,
        body,
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        agent_name="claude-native-ui",
        file_store=None,
        artifact_store=None,
        created_by="alice@example.com",
    )

    assert result.pending_id is None
    assert result.item_id is not None
    assert [call[0] for call in client.post_json_calls] == [
        "/v1/sessions/conv_claude/resources/terminals"
    ]
    messages = [i for i in store.appended_items if i.type == "message"]
    errors = [i for i in store.appended_items if i.type == "error"]
    assert [i.type for i in store.appended_items] == ["message", "error"]
    # One message means the failed turn was consumed. Zero would leave
    # the input pending forever; two would mean the Omnigent fast-fail path
    # duplicated the user's message.
    assert len(messages) == 1
    assert messages[0].created_by == "alice@example.com"
    assert messages[0].data.content == [{"type": "input_text", "text": "try again"}]
    # One error means the consumed message has exactly one visible
    # failure sibling. Zero would be silent after refresh; two would
    # duplicate the same terminal-start failure.
    assert len(errors) == 1
    assert errors[0].data.code == "native_terminal_start_failed"
    assert errors[0].data.message == "Native Claude requires the 'claude' CLI on PATH."


@pytest.mark.asyncio
async def test_native_dispatch_reports_malformed_runner_error_body() -> None:
    """Opaque framework 500 bodies become explicit ensure errors.

    If the runner/tunnel returns a plain ``Internal Server Error`` body
    instead of the structured native startup error JSON, Omnigent still fast
    fails the message. The transcript should not invent a missing
    native CLI cause or persist the raw HTTP body; it should say the
    runner returned a malformed ensure response.
    """
    from omnigent.server.routes.sessions import _dispatch_session_event_to_runner

    store = _ConversationStore()
    conv = store.get_conversation("conv_claude")
    assert conv is not None
    client = _FakeRunnerClient(
        text_responses={
            "/v1/sessions/conv_claude/resources/terminals": (
                500,
                "Internal Server Error",
                {"content-type": "text/plain"},
            )
        }
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "retry"}]},
    )

    result = await _dispatch_session_event_to_runner(
        "conv_claude",
        conv,
        body,
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        agent_name="claude-native-ui",
        file_store=None,
        artifact_store=None,
        created_by=None,
    )

    assert result.pending_id is None
    errors = [i for i in store.appended_items if i.type == "error"]
    # One durable error proves the failed user message is consumed with
    # a visible banner. Zero would leave the turn silent after refresh;
    # two would duplicate the AP-side fast-fail record.
    assert len(errors) == 1
    assert errors[0].data.code == "native_terminal_ensure_failed"
    assert (
        errors[0].data.message
        == "Native Claude terminal ensure failed with malformed runner response (HTTP 500)."
    )


@pytest.mark.asyncio
async def test_native_dispatch_transport_error_does_not_fallback_to_forwarding() -> None:
    """Runner transport failure is a definitive AP-side ensure error.

    If Omnigent cannot reach the runner's terminal ensure endpoint, it must
    consume the user message and record an explicit ensure failure. It
    should not fall back to forwarding the message and waiting on the
    old boot grace path, because that reintroduces the slow hang.
    """
    from omnigent.server.routes.sessions import _dispatch_session_event_to_runner

    store = _ConversationStore()
    conv = store.get_conversation("conv_claude")
    assert conv is not None
    client = _FakeRunnerClient(
        exc=httpx.ConnectError(
            "connection refused",
            request=httpx.Request(
                "POST",
                "/v1/sessions/conv_claude/resources/terminals",
            ),
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "retry"}]},
    )

    result = await _dispatch_session_event_to_runner(
        "conv_claude",
        conv,
        body,
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        agent_name="claude-native-ui",
        file_store=None,
        artifact_store=None,
        created_by=None,
    )

    assert result.pending_id is None
    assert [call[0] for call in client.post_json_calls] == [
        "/v1/sessions/conv_claude/resources/terminals"
    ]
    assert [i.type for i in store.appended_items] == ["message", "error"]
    errors = [i for i in store.appended_items if i.type == "error"]
    # One error proves the transport failure was surfaced durably. Zero
    # would be silent after refresh; two would mean the fast-fail path
    # duplicated its own banner.
    assert len(errors) == 1
    assert errors[0].data.code == "native_terminal_ensure_failed"
    assert (
        errors[0].data.message
        == "Native Claude terminal ensure request failed. connection refused"
    )


@pytest.mark.asyncio
async def test_native_dispatch_tunnel_close_is_definitive_ensure_error() -> None:
    """A WS-tunnel drop during terminal ensure fails the turn durably.

    WSTunnelTransport raises bare ``ConnectionError`` — not an
    ``httpx.HTTPError`` — when the runner's tunnel closes mid-request
    ("tunnel closed before request completed"). Before this fix the
    exception escaped the ensure probe's ``except httpx.HTTPError``
    clause to the global catch-all, so the web client saw an opaque
    500 ``internal_error`` instead of the durable ensure-failure turn
    error. The dispatch must treat it exactly like an httpx transport
    failure: consume the user message and persist an explicit error.
    """
    from omnigent.server.routes.sessions import _dispatch_session_event_to_runner

    store = _ConversationStore()
    conv = store.get_conversation("conv_claude")
    assert conv is not None
    client = _FakeRunnerClient(exc=ConnectionError("tunnel closed before request completed"))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "retry"}]},
    )

    result = await _dispatch_session_event_to_runner(
        "conv_claude",
        conv,
        body,
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        agent_name="claude-native-ui",
        file_store=None,
        artifact_store=None,
        created_by=None,
    )

    assert result.pending_id is None
    # message + error proves the fast-fail path consumed the input and
    # recorded a visible failure; an escaped ConnectionError would have
    # raised out of the dispatch instead of returning a result at all.
    assert [i.type for i in store.appended_items] == ["message", "error"]
    errors = [i for i in store.appended_items if i.type == "error"]
    assert len(errors) == 1
    assert errors[0].data.code == "native_terminal_ensure_failed"
    assert (
        errors[0].data.message
        == "Native Claude terminal ensure request failed. tunnel closed before request completed"
    )


@pytest.mark.asyncio
async def test_native_dispatch_persists_error_for_each_user_retry() -> None:
    """A fresh user retry gets its own durable terminal error.

    Reconnect spam should dedupe, but a user sending a new message to a
    still-broken native terminal is a new transcript turn. The error
    must be written after that user message so refresh still shows why
    the retry failed.
    """
    from omnigent.server.routes.sessions import _dispatch_session_event_to_runner

    store = _ConversationStore()
    conv = store.get_conversation("conv_claude")
    assert conv is not None
    client = _FakeRunnerClient(
        responses={
            "/v1/sessions/conv_claude/resources/terminals": (
                500,
                {
                    "error": {
                        "code": "native_terminal_start_failed",
                        "message": "Native Claude requires the 'claude' CLI on PATH.",
                    }
                },
            )
        }
    )

    for text in ("first retry", "second retry"):
        body = SessionEventInput(
            type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": text}]},
        )
        result = await _dispatch_session_event_to_runner(
            "conv_claude",
            conv,
            body,
            store,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            agent_name="claude-native-ui",
            file_store=None,
            artifact_store=None,
            created_by=None,
        )
        assert result.pending_id is None

    messages = [i for i in store.appended_items if i.type == "message"]
    errors = [i for i in store.appended_items if i.type == "error"]
    assert [m.data.content[0]["text"] for m in messages] == ["first retry", "second retry"]
    assert [i.type for i in store.appended_items] == ["message", "error", "message", "error"]
    # Two errors proves dedupe resets after each new user message. One
    # would incorrectly treat a user retry as reconnect spam; three or
    # more would mean one retry wrote duplicate banners.
    assert len(errors) == 2
    assert [call[0] for call in client.post_json_calls] == [
        "/v1/sessions/conv_claude/resources/terminals",
        "/v1/sessions/conv_claude/resources/terminals",
    ]
    assert all(
        e.data.message == "Native Claude requires the 'claude' CLI on PATH." for e in errors
    )


@pytest.mark.asyncio
async def test_native_dispatch_records_same_error_after_recovery_boundary() -> None:
    """A retry after intervening activity gets its own error.

    If a native terminal recovers enough to produce assistant output
    and then later hits the same startup failure again, the user's new
    retry should record a new visible error instead of being suppressed
    by the previous failure.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.server.routes.sessions import _dispatch_session_event_to_runner

    store = _ConversationStore()
    conv = store.get_conversation("conv_claude")
    assert conv is not None
    client = _FakeRunnerClient(
        responses={
            "/v1/sessions/conv_claude/resources/terminals": (
                500,
                {
                    "error": {
                        "code": "native_terminal_start_failed",
                        "message": "Native Claude requires the 'claude' CLI on PATH.",
                    }
                },
            )
        }
    )

    first = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "first retry"}]},
    )
    await _dispatch_session_event_to_runner(
        "conv_claude",
        conv,
        first,
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        agent_name="claude-native-ui",
        file_store=None,
        artifact_store=None,
        created_by=None,
    )
    store.append(
        "conv_claude",
        [
            NewConversationItem(
                type="message",
                response_id="resp_recovered",
                data=MessageData(
                    role="assistant",
                    agent="claude-native-ui",
                    content=[{"type": "output_text", "text": "recovered"}],
                ),
            )
        ],
    )
    second = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "second retry"}]},
    )
    await _dispatch_session_event_to_runner(
        "conv_claude",
        conv,
        second,
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        agent_name="claude-native-ui",
        file_store=None,
        artifact_store=None,
        created_by=None,
    )

    errors = [i for i in store.appended_items if i.type == "error"]
    # Two errors proves a later user retry after intervening activity is
    # not suppressed by session-global dedupe. One would hide the second
    # failure; three or more would indicate duplicate persistence.
    assert len(errors) == 2
    assert all(
        e.data.message == "Native Claude requires the 'claude' CLI on PATH." for e in errors
    )


@pytest.mark.asyncio
async def test_relay_skips_malformed_resource_created_from_runner() -> None:
    """A ``session.resource.created`` missing its ``resource`` persists nothing.

    A malformed frame must not poison the relay or persist a partial
    item — the snapshot endpoint stays the source of truth.
    """
    from omnigent.server.routes.sessions import _relay_runner_stream

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            _sse_frame({"type": "session.resource.created"}),
            "data: [DONE]\n\n",
        ]
    )

    await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

    events = [i for i in store.appended_items if i.type == "resource_event"]
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame_payload",
    [
        # created: empty-string id / type are malformed — isinstance(str)
        # admits "" but it can't resolve to a real resource.
        {"type": "session.resource.created", "resource": {"id": "", "type": "terminal"}},
        {"type": "session.resource.created", "resource": {"id": "terminal_zsh_s1", "type": ""}},
        # deleted: same for the flat id / type fields.
        {
            "type": "session.resource.deleted",
            "resource_id": "",
            "resource_type": "terminal",
            "session_id": "conv_proxy",
        },
        {
            "type": "session.resource.deleted",
            "resource_id": "terminal_zsh_s1",
            "resource_type": "",
            "session_id": "conv_proxy",
        },
    ],
)
async def test_relay_skips_empty_resource_id_or_type_from_runner(
    frame_payload: dict[str, Any],
) -> None:
    """Empty-string resource id/type frames persist nothing.

    A frame whose id or type is ``""`` is malformed — persisting it
    would leave a ``resource_event`` item the snapshot can't map back
    to a real resource. The relay must drop it (regression guard for
    the ``isinstance(x, str)``-only check that accepted ``""``).
    """
    from omnigent.server.routes.sessions import _relay_runner_stream

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient([_sse_frame(frame_payload), "data: [DONE]\n\n"])

    await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

    events = [i for i in store.appended_items if i.type == "resource_event"]
    assert events == []


@pytest.mark.asyncio
async def test_relay_pairs_function_call_output_with_call_response_id() -> None:
    """
    A ``function_call_output`` persists with its call's ``response_id``.

    When the harness starts a new response before the tool result
    arrives, the relay's ``current_response_id`` has already advanced.
    Without the call_id → response_id tracking, the output gets the
    new id and the web UI can't pair it with the spinner — the tool
    card shows "Waiting for output" forever.
    """
    from omnigent.server.routes.sessions import _relay_runner_stream

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            _sse_frame(
                {
                    "type": "response.in_progress",
                    "response": {"id": "resp_first"},
                }
            ),
            # function_call: the relay records call_id → response_id
            # from the raw event even though parse_item_data fails
            # (serialization alias mismatch for the ``model`` field).
            _sse_frame(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "id": "item_fc1",
                        "call_id": "call_abc",
                        "name": "github_write_api_call",
                        "arguments": '{"comment_body": "/merge"}',
                        "status": "completed",
                        "model": "nessie",
                        "response_id": "resp_first",
                    },
                }
            ),
            # New response starts BEFORE tool result arrives.
            _sse_frame(
                {
                    "type": "response.in_progress",
                    "response": {"id": "resp_second"},
                }
            ),
            _sse_frame(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call_output",
                        "id": "item_fco1",
                        "call_id": "call_abc",
                        "output": "Comment posted.",
                        "response_id": "resp_second",
                    },
                }
            ),
            "data: [DONE]\n\n",
        ]
    )

    await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

    outputs = [i for i in store.appended_items if i.type == "function_call_output"]
    # Exactly one output — the single tool result in the stream. Zero
    # would mean the relay dropped it; more than one means a duplicate.
    assert len(outputs) == 1
    # The output must share the CALL's response_id, not the second
    # response's id that current_response_id would have contained.
    assert outputs[0].response_id == "resp_first", (
        f"function_call_output got response_id={outputs[0].response_id!r}; "
        f"expected 'resp_first' (the matching function_call's response_id)"
    )


@pytest.mark.asyncio
async def test_relay_publishes_inflight_frames_and_discards_on_exit() -> None:
    """
    The relay feeds the in-flight index, then discards it on exit.

    Drives the REAL relay with the runner's actual SSE frame shapes —
    ``response.created`` + ``response.output_text.delta`` — and ends the
    stream with NO terminal ``response.*`` (a runner death / Stop /
    tunnel drop mid-turn). Two properties:

    * **Producer:** the relay republishes the lifecycle + text frames
      through ``session_stream.publish`` (captured via a concurrent
      subscriber). Those are exactly what ``record_publish`` accumulates
      into the in-flight index for snapshot replay — so the populate
      path is real, not just asserted in isolation.
    * **Leak fix:** when the relay task exits without a terminal event,
      its ``finally`` calls ``inflight_text.discard`` so the entry is
      gone. Without that discard, a turn ended by Stop / runner death
      would strand the entry forever and replay stale text on the next
      reload (and grow the index unbounded).
    """
    from omnigent.runtime import inflight_text, session_stream
    from omnigent.server.routes.sessions import _relay_runner_stream

    inflight_text.reset_for_tests()
    published: list[dict[str, Any]] = []

    async def _consume() -> None:
        async for event in session_stream.subscribe("conv_proxy"):
            published.append(event)

    try:
        consumer = asyncio.create_task(_consume())
        # One loop turn lets the subscriber register its slot before the
        # relay starts publishing, so nothing is missed.
        await asyncio.sleep(0)

        store = _ConversationStore()
        client = _FakeStreamingRunnerClient(
            [
                _sse_frame(
                    {
                        "type": "response.created",
                        "response": {
                            "id": "resp_mid",
                            "model": "nessie",
                            "status": "queued",
                            "created_at": 1,
                        },
                    }
                ),
                _sse_frame({"type": "response.output_text.delta", "delta": "Planning "}),
                _sse_frame({"type": "response.output_text.delta", "delta": "the work."}),
                # No response.completed / cancelled — the turn is still
                # streaming when the stream ends (runner death / Stop).
                "data: [DONE]\n\n",
            ]
        )

        await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]
        session_stream.close("conv_proxy")
        await consumer

        # Producer: the relay published the lifecycle + text frames that
        # feed the in-flight index. Missing either would mean the relay
        # stopped republishing them and the index could never populate.
        published_types = [e.get("type") for e in published]
        assert "response.created" in published_types, published_types
        assert published_types.count("response.output_text.delta") == 2, published_types
        # The turn never completed → no assistant message persisted.
        assert [i for i in store.appended_items if i.type == "message"] == [], (
            "an unfinished turn must not persist an assistant message"
        )
        # Leak fix: the relay's teardown discarded the entry. A non-empty
        # result means the finally discard was dropped and a Stop / runner
        # death would strand stale text (replayed on the next reload).
        assert inflight_text.snapshot_for("conv_proxy") == [], (
            "relay exit must discard the in-flight entry"
        )
    finally:
        inflight_text.reset_for_tests()
        session_stream._subscribers.clear()


@pytest.mark.asyncio
async def test_relay_fences_cancelled_turn_and_resumes_on_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop fences the turn: its trailing deltas aren't persisted or forwarded.

    Repro of the stop-mid-stream bug: after the user Stops, the cancelled
    turn's remaining deltas must be DROPPED — not persisted to the transcript
    and not forwarded to the live stream. Dropped deltas never enter the text
    buffer, so the turn's ``response.completed`` (which now lifts the fence
    and is processed normally) flushes nothing for the abandoned tail. The
    follow-up turn persists + forwards normally so it has a real effect.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import _relay_runner_stream

    sid = "conv_fenced"
    published: list[dict[str, Any]] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        """Record events the relay forwards to the live stream for *sid*."""
        if session_id == sid:
            published.append(event)

    monkeypatch.setattr(session_stream, "publish", _capture)

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            # Cancelled turn's trailing content — the session is already fenced.
            _sse_frame({"type": "response.output_text.delta", "delta": "ABANDONED"}),
            _sse_frame({"type": "response.completed", "response": {"id": "r1", "model": "m"}}),
            # Follow-up turn: running lifts the fence, then a real reply.
            _sse_frame({"type": "session.status", "status": "running"}),
            _sse_frame({"type": "response.output_text.delta", "delta": "REAL REPLY"}),
            _sse_frame({"type": "response.completed", "response": {"id": "r2", "model": "m"}}),
            "data: [DONE]\n\n",
        ]
    )

    # The Stop fenced the session before this relay run (set by the route).
    sessions_module._interrupt_fenced_sessions.add(sid)
    try:
        await _relay_runner_stream(sid, client, store)  # type: ignore[arg-type]
    finally:
        sessions_module._interrupt_fenced_sessions.discard(sid)

    # Exactly one assistant message persisted — the follow-up reply. The
    # cancelled turn's delta+completed were dropped. If 2 (or "ABANDONED"),
    # the fence leaked and the abandoned reply reached the transcript.
    messages = [
        i for i in store.appended_items if i.type == "message" and i.data.role == "assistant"
    ]
    assert len(messages) == 1, f"expected 1 assistant message, got {[m.data for m in messages]}"
    assert messages[0].data.content[0]["text"] == "REAL REPLY"

    # The follow-up turn's running status lifted the fence.
    assert sid not in sessions_module._interrupt_fenced_sessions

    # The cancelled turn's content was never forwarded to the live stream;
    # only the follow-up's reply was.
    published_deltas = [
        e.get("delta") for e in published if e.get("type") == "response.output_text.delta"
    ]
    assert "ABANDONED" not in published_deltas, (
        f"cancelled-turn delta must not be forwarded; got {published_deltas}"
    )
    assert "REAL REPLY" in published_deltas


@pytest.mark.asyncio
async def test_relay_flushes_partial_text_on_failed_turn_before_error_item() -> None:
    """A failed turn persists its streamed narration, ordered before the error.

    Repro of the lost-narration bug: scaffold text only flushed at tool
    boundaries and on ``response.completed``, so a turn that FAILED dropped
    everything streamed since the last boundary — after reload only the error
    remained. The relay must flush the buffered text on ``response.failed``
    (before the durable error item, matching what the user watched stream),
    and the terminal's publish must clear the in-flight replay entry.
    """
    from omnigent.runtime import inflight_text
    from omnigent.server.routes.sessions import _relay_runner_stream

    inflight_text.reset_for_tests()
    sid = "conv_proxy"
    store = _ConversationStore()
    # In-flight replay snapshots probed at deterministic points: after the
    # deltas (entry populated) and after the failed terminal (entry cleared).
    inflight_after_deltas: list[list[dict[str, Any]]] = []
    inflight_after_failed: list[list[dict[str, Any]]] = []

    client = _ScriptedStreamingRunnerClient(
        [
            _sse_frame(
                {
                    "type": "response.in_progress",
                    "response": {"id": "resp_fail", "model": "nessie"},
                }
            ),
            _sse_frame({"type": "response.output_text.delta", "delta": "Drafting the plan. "}),
            _sse_frame({"type": "response.output_text.delta", "delta": "Now running checks."}),
            lambda: inflight_after_deltas.append(inflight_text.snapshot_for(sid)),
            _sse_frame(
                {
                    "type": "response.failed",
                    "response": {
                        "id": "resp_fail",
                        "model": "nessie",
                        "error": {"code": "llm_error", "message": "LLM exploded"},
                    },
                }
            ),
            lambda: inflight_after_failed.append(inflight_text.snapshot_for(sid)),
            "data: [DONE]\n\n",
        ]
    )

    try:
        await _relay_runner_stream(sid, client, store)  # type: ignore[arg-type]

        # Store order: narration message FIRST, then the error item. Reversed
        # (or missing the message) means the failed-turn flush regressed and
        # reload shows the error without the text the user watched stream.
        types = [i.type for i in store.appended_items]
        assert types == ["message", "error"], types
        message, error = store.appended_items
        assert "".join(b["text"] for b in message.data.content) == (
            "Drafting the plan. Now running checks."
        )
        # Both items share the failed turn's id so they group in one bubble.
        assert message.response_id == "resp_fail"
        assert error.response_id == "resp_fail"
        assert error.data.code == "llm_error"
        assert error.data.message == "LLM exploded"
        # Populated before the terminal: proves the clear below is a real
        # transition, not "the index was never fed".
        assert inflight_after_deltas == [
            [
                {
                    "type": "response.created",
                    "response": {"id": "resp_fail", "model": "nessie"},
                },
                {
                    "type": "response.output_text.delta",
                    "delta": "Drafting the plan. Now running checks.",
                },
            ]
        ], inflight_after_deltas
        # Cleared by the terminal's publish AFTER the flush persisted the
        # text — a non-empty snapshot here would double-render on reconnect.
        assert inflight_after_failed == [[]], inflight_after_failed
    finally:
        inflight_text.reset_for_tests()


@pytest.mark.asyncio
async def test_relay_flushes_final_text_on_fenced_response_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``response.completed`` consumed while fenced still persists the answer.

    Repro of the stop-vs-completion race: the user hits Stop just as a
    no-tool turn finishes naturally. The fence used to drop the fenced
    ``response.completed`` (skipping the final flush), losing the ENTIRE
    answer from history even though the live tab rendered it. A fenced
    completed proves the turn was NOT cancelled: it must flush + publish
    normally and lift the fence.
    """
    from omnigent.runtime import inflight_text, session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import _relay_runner_stream

    inflight_text.reset_for_tests()
    sid = "conv_fence_completed"
    store = _ConversationStore()
    published: list[dict[str, Any]] = []
    real_publish = session_stream.publish

    def _capture_publish(conversation_id: str, event: dict[str, Any]) -> None:
        """Record events for *sid* while keeping the real publish path."""
        if conversation_id == sid:
            published.append(event)
        real_publish(conversation_id, event)

    monkeypatch.setattr(session_stream, "publish", _capture_publish)

    def _install_fence() -> None:
        """Install the stop fence mid-turn, as POST /events does on Stop."""
        sessions_module._interrupt_fenced_sessions.add(sid)

    client = _ScriptedStreamingRunnerClient(
        [
            _sse_frame(
                {"type": "response.in_progress", "response": {"id": "resp_done", "model": "m"}}
            ),
            _sse_frame({"type": "response.output_text.delta", "delta": "The answer is 42."}),
            _install_fence,
            _sse_frame(
                {"type": "response.completed", "response": {"id": "resp_done", "model": "m"}}
            ),
            "data: [DONE]\n\n",
        ]
    )

    try:
        await _relay_runner_stream(sid, client, store)  # type: ignore[arg-type]

        # The full answer persisted despite the fence. Empty means the fenced
        # completed was dropped again and the turn vanishes from history.
        messages = [i for i in store.appended_items if i.type == "message"]
        assert len(messages) == 1, f"expected 1 assistant message, got {store.appended_items}"
        assert "".join(b["text"] for b in messages[0].data.content) == "The answer is 42."
        assert messages[0].response_id == "resp_done"
        # The terminal lifted the fence — without this, every later turn's
        # output would be dropped until the next "running" status.
        assert sid not in sessions_module._interrupt_fenced_sessions
        # The completed event reached the live stream so clients close the
        # turn; absence means the fence still swallows terminal events.
        assert any(e.get("type") == "response.completed" for e in published), published
    finally:
        sessions_module._interrupt_fenced_sessions.discard(sid)
        inflight_text.reset_for_tests()


@pytest.mark.asyncio
async def test_relay_persists_pre_stop_narration_on_fenced_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted turn keeps its pre-Stop narration; the trailing tail drops.

    The live tab keeps showing the text streamed before the Stop, so reload
    must too: the fenced ``response.incomplete`` (the runtime cancel
    handler's terminal) flushes the buffered pre-Stop text. Deltas that
    arrive AFTER the Stop are still suppressed — they never reach the live
    stream, so persisting them would make reload diverge from what was
    watched.
    """
    from omnigent.runtime import inflight_text, session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import _relay_runner_stream

    inflight_text.reset_for_tests()
    sid = "conv_fence_incomplete"
    store = _ConversationStore()
    published: list[dict[str, Any]] = []
    real_publish = session_stream.publish

    def _capture_publish(conversation_id: str, event: dict[str, Any]) -> None:
        """Record events for *sid* while keeping the real publish path."""
        if conversation_id == sid:
            published.append(event)
        real_publish(conversation_id, event)

    monkeypatch.setattr(session_stream, "publish", _capture_publish)

    def _install_fence() -> None:
        """Install the stop fence mid-turn, as POST /events does on Stop."""
        sessions_module._interrupt_fenced_sessions.add(sid)

    client = _ScriptedStreamingRunnerClient(
        [
            _sse_frame(
                {"type": "response.in_progress", "response": {"id": "resp_int", "model": "m"}}
            ),
            _sse_frame({"type": "response.output_text.delta", "delta": "Partial thought."}),
            _install_fence,
            _sse_frame({"type": "response.output_text.delta", "delta": " DOOMED TAIL"}),
            _sse_frame(
                {"type": "response.incomplete", "response": {"id": "resp_int", "model": "m"}}
            ),
            _sse_frame({"type": "session.status", "status": "idle"}),
            "data: [DONE]\n\n",
        ]
    )

    try:
        await _relay_runner_stream(sid, client, store)  # type: ignore[arg-type]

        # Only the pre-Stop narration persisted. A message containing the
        # tail means fenced deltas leaked into the buffer; no message means
        # the fenced terminal still drops the text the user watched.
        messages = [i for i in store.appended_items if i.type == "message"]
        assert len(messages) == 1, f"expected 1 assistant message, got {store.appended_items}"
        assert "".join(b["text"] for b in messages[0].data.content) == "Partial thought."
        assert messages[0].response_id == "resp_int"
        # The post-Stop delta never reached the live stream.
        published_deltas = [
            e.get("delta") for e in published if e.get("type") == "response.output_text.delta"
        ]
        assert " DOOMED TAIL" not in published_deltas, published_deltas
        # The terminal lifted the fence without waiting for the next turn.
        assert sid not in sessions_module._interrupt_fenced_sessions
    finally:
        sessions_module._interrupt_fenced_sessions.discard(sid)
        inflight_text.reset_for_tests()


@pytest.mark.asyncio
async def test_relay_lets_elicitation_resolved_pass_the_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``response.elicitation_resolved`` passes the fence and clears the index.

    Repro of the ghost-approval-card leak: on Stop, the runner's parked-ASK
    cleanup publishes ``response.elicitation_resolved`` exactly inside the
    fenced window. The pending-elicitations index decrements ONLY via that
    event, so swallowing it leaks the entry — every later snapshot replays a
    dead approval card and the "Needs input" badge never clears.
    """
    from omnigent.runtime import inflight_text, pending_elicitations, session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import _relay_runner_stream

    inflight_text.reset_for_tests()
    pending_elicitations.reset_for_tests()
    sid = "conv_fence_elicit"
    store = _ConversationStore()
    published: list[dict[str, Any]] = []
    real_publish = session_stream.publish

    def _capture_publish(conversation_id: str, event: dict[str, Any]) -> None:
        """Record events for *sid* while keeping the real publish path."""
        if conversation_id == sid:
            published.append(event)
        real_publish(conversation_id, event)

    monkeypatch.setattr(session_stream, "publish", _capture_publish)

    # Pending count probed between frames: after the request (tracked) and
    # after the fenced resolved (must be drained).
    counts: list[int] = []

    def _record_count_and_install_fence() -> None:
        """Capture the tracked count, then fence the session (Stop pressed)."""
        counts.append(pending_elicitations.count_for(sid))
        sessions_module._interrupt_fenced_sessions.add(sid)

    client = _ScriptedStreamingRunnerClient(
        [
            _sse_frame(
                {
                    "type": "response.elicitation_request",
                    "elicitation_id": "elicit_1",
                    "params": {"message": "Approve push?", "mode": "question"},
                }
            ),
            _record_count_and_install_fence,
            _sse_frame({"type": "response.elicitation_resolved", "elicitation_id": "elicit_1"}),
            lambda: counts.append(pending_elicitations.count_for(sid)),
            "data: [DONE]\n\n",
        ]
    )

    try:
        await _relay_runner_stream(sid, client, store)  # type: ignore[arg-type]

        # [1, 0]: the request was tracked, and the FENCED resolved drained it.
        # A trailing 1 means the fence swallowed the resolved event — the
        # ghost-card leak this change fixes.
        assert counts == [1, 0], counts
        # The resolved event also reached live subscribers so open tabs
        # clear their approval card.
        assert any(e.get("type") == "response.elicitation_resolved" for e in published), published
        # Elicitation bookkeeping is exempt from the fence but must not
        # LIFT it — only a terminal or the next turn's running does.
        assert sid in sessions_module._interrupt_fenced_sessions
    finally:
        sessions_module._interrupt_fenced_sessions.discard(sid)
        pending_elicitations.reset_for_tests()
        inflight_text.reset_for_tests()


@pytest.mark.asyncio
async def test_relay_suppresses_fenced_deltas_until_running_when_no_terminal_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no terminal event, the fence holds until the next turn's running.

    The stop_session hard-kill path can end a turn with NO terminal
    ``response.*`` at all. The fence must keep suppressing the dead turn's
    trailing deltas and lift only on the next turn's ``running`` status, so
    the follow-up turn flows normally.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import _relay_runner_stream

    sid = "conv_fence_no_terminal"
    published: list[dict[str, Any]] = []

    def _capture(session_id: str, event: dict[str, Any]) -> None:
        """Record events the relay forwards to the live stream for *sid*."""
        if session_id == sid:
            published.append(event)

    monkeypatch.setattr(session_stream, "publish", _capture)

    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            # Dead turn's trailing delta — the session is already fenced.
            _sse_frame({"type": "response.output_text.delta", "delta": "GHOST"}),
            # Next turn: running lifts the fence, then a real reply.
            _sse_frame({"type": "session.status", "status": "running"}),
            _sse_frame(
                {"type": "response.in_progress", "response": {"id": "resp_next", "model": "m"}}
            ),
            _sse_frame({"type": "response.output_text.delta", "delta": "REAL"}),
            _sse_frame(
                {"type": "response.completed", "response": {"id": "resp_next", "model": "m"}}
            ),
            "data: [DONE]\n\n",
        ]
    )

    sessions_module._interrupt_fenced_sessions.add(sid)
    try:
        await _relay_runner_stream(sid, client, store)  # type: ignore[arg-type]
    finally:
        sessions_module._interrupt_fenced_sessions.discard(sid)

    # Only the follow-up turn's text persisted; "GHOST" persisting means the
    # kept-buffer change let a fenced delta leak into the transcript.
    messages = [i for i in store.appended_items if i.type == "message"]
    assert len(messages) == 1, f"expected 1 assistant message, got {store.appended_items}"
    assert "".join(b["text"] for b in messages[0].data.content) == "REAL"
    # The dead turn's delta was never forwarded live; the real one was.
    published_deltas = [
        e.get("delta") for e in published if e.get("type") == "response.output_text.delta"
    ]
    assert published_deltas == ["REAL"], published_deltas


@pytest.mark.asyncio
async def test_relay_interleaves_text_segments_with_tool_calls() -> None:
    """
    Scaffold narration persists interleaved with the tool calls it preceded.

    A scaffold turn streams text1 → tool1 → tool2 → text2 → tool3 → text3.
    The relay must persist three SEPARATE assistant messages, each BEFORE
    the tool call that followed it — not one concatenated message after all
    the tools (which renders tools-above-text + run-on text on reload).
    """
    from omnigent.runtime import inflight_text
    from omnigent.server.routes.sessions import _relay_runner_stream

    inflight_text.reset_for_tests()
    store = _ConversationStore()

    def _fc(call_id: str, name: str) -> str:
        return _sse_frame(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": f"item_{call_id}",
                    "call_id": call_id,
                    "name": name,
                    "arguments": "{}",
                    "status": "completed",
                    "agent": "nessie",
                },
            }
        )

    def _fco(call_id: str) -> str:
        return _sse_frame(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call_output",
                    "id": f"out_{call_id}",
                    "call_id": call_id,
                    "output": "ok",
                },
            }
        )

    client = _FakeStreamingRunnerClient(
        [
            _sse_frame(
                {"type": "response.in_progress", "response": {"id": "resp_1", "model": "nessie"}}
            ),
            _sse_frame({"type": "response.output_text.delta", "delta": "I'll dispatch "}),
            _sse_frame({"type": "response.output_text.delta", "delta": "both agents."}),
            _fc("call_1", "sys_session_send"),
            _fco("call_1"),
            _fc("call_2", "sys_session_send"),
            _fco("call_2"),
            _sse_frame({"type": "response.output_text.delta", "delta": "Both are running. "}),
            _sse_frame({"type": "response.output_text.delta", "delta": "Checking inbox."}),
            _fc("call_3", "sys_read_inbox"),
            _fco("call_3"),
            _sse_frame({"type": "response.output_text.delta", "delta": "Here are the jokes."}),
            _sse_frame(
                {"type": "response.completed", "response": {"id": "resp_1", "model": "nessie"}}
            ),
            "data: [DONE]\n\n",
        ]
    )

    try:
        await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

        # Persisted order interleaves narration with its tool calls — the
        # first text lands BEFORE call_1, not pooled after every tool.
        types = [i.type for i in store.appended_items]
        assert types == [
            "message",
            "function_call",
            "function_call_output",
            "function_call",
            "function_call_output",
            "message",
            "function_call",
            "function_call_output",
            "message",
        ], types

        # Three SEPARATE messages, each its own segment — not one run-on.
        msgs = [i for i in store.appended_items if i.type == "message"]
        texts = ["".join(b["text"] for b in m.data.content) for m in msgs]
        assert texts == [
            "I'll dispatch both agents.",
            "Both are running. Checking inbox.",
            "Here are the jokes.",
        ], texts
    finally:
        inflight_text.reset_for_tests()


@pytest.mark.asyncio
async def test_relay_flush_drops_committed_text_from_inflight_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    After a text→tool flush, a mid-turn reconnect must NOT replay the
    just-committed narration — it would double-render beside the persisted
    copy. Pins that the relay calls ``inflight_text.reset_text`` at the
    flush: feed a turn that ENDS right after the first flush (no terminal
    event), neutralize the relay's teardown ``discard``, and assert the
    text both committed AND no longer replays. Without the reset call this
    fails (the in-flight buffer would still replay the committed text).
    """
    from omnigent.runtime import inflight_text
    from omnigent.server.routes.sessions import _relay_runner_stream

    inflight_text.reset_for_tests()
    # The relay's finally discards the in-flight entry (the leak fix);
    # neutralize it so we observe the state the flush itself left behind.
    monkeypatch.setattr(inflight_text, "discard", lambda *_a, **_k: None)
    store = _ConversationStore()
    client = _FakeStreamingRunnerClient(
        [
            _sse_frame(
                {"type": "response.in_progress", "response": {"id": "resp_1", "model": "nessie"}}
            ),
            _sse_frame(
                {"type": "response.output_text.delta", "delta": "Narration before the tool."}
            ),
            _sse_frame(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "id": "item_c1",
                        "call_id": "c1",
                        "name": "sys_session_send",
                        "arguments": "{}",
                        "status": "completed",
                        "agent": "nessie",
                    },
                }
            ),
            # Ends mid-turn (no terminal response.* / session.status) — only
            # the flush's reset_text could have cleared the in-flight buffer.
            "data: [DONE]\n\n",
        ]
    )
    try:
        await _relay_runner_stream("conv_proxy", client, store)  # type: ignore[arg-type]

        # The narration committed as its own message...
        msgs = [i for i in store.appended_items if i.type == "message"]
        assert [b["text"] for m in msgs for b in m.data.content] == [
            "Narration before the tool."
        ], [m.data.content for m in msgs]
        # ...and the flush dropped it from the replay, so a reconnect here
        # gets nothing. If reset_text were removed, snapshot_for would
        # replay the committed text and double-render it.
        assert inflight_text.snapshot_for("conv_proxy") == [], (
            "flushed (committed) text must not replay on mid-turn reconnect"
        )
    finally:
        inflight_text.reset_for_tests()


class _StreamAndCaptureRunnerClient(_FakeStreamingRunnerClient):
    """``_FakeStreamingRunnerClient`` that also captures forwarded POSTs.

    The relay forwards the sub-agent terminal ``external_session_status`` back
    to the bound runner via ``post``; capturing those lets a test assert the
    wake-triggering delivery. ``post_failures`` makes the first N posts return
    503 so the retry path can be exercised.

    :param frames: SSE frame strings yielded from the runner stream.
    :param post_failures: Number of initial posts to fail with 503.
    """

    def __init__(self, frames: list[str], post_failures: int = 0) -> None:
        """Initialize the stream frames, post capture, and failure count."""
        super().__init__(frames)
        self.posts: list[dict[str, Any]] = []
        self._post_failures = post_failures

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: object = None,
    ) -> httpx.Response:
        """Capture a forwarded event; 503 for the first ``post_failures``, else 204."""
        del timeout
        self.posts.append({"url": url, "body": json})
        if len(self.posts) <= self._post_failures:
            return httpx.Response(503)
        return httpx.Response(204)


class _SubagentTerminalStore:
    """Conversation store stub for claude-native sub-agent delivery tests.

    Returns one configurable :class:`Conversation` from
    ``get_conversation`` and one assistant message from ``list_items``,
    using real entity types so the relay's enrichment
    (``_latest_assistant_text_from_store``) runs for real.

    :param conv: The conversation row the relay inspects to decide
        whether a terminal edge needs sub-agent delivery.
    :param assistant_text: Latest assistant text surfaced via
        ``list_items``, or ``None`` for no persisted assistant message.
    """

    def __init__(self, conv: Conversation, assistant_text: str | None) -> None:
        """Store the canned conversation and assistant text."""
        self._conv = conv
        self._assistant_text = assistant_text

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Return the configured conversation when the id matches."""
        return self._conv if conversation_id == self._conv.id else None

    def list_items(
        self,
        conversation_id: str,
        *,
        limit: int,
        order: str,
        type: str,
    ) -> Any:
        """Return a one-item assistant page (or empty) as a real PagedList."""
        from omnigent.entities import ConversationItem, MessageData, PagedList

        del conversation_id, limit, order, type
        if self._assistant_text is None:
            return PagedList(data=[], first_id=None, last_id=None, has_more=False)
        item = ConversationItem(
            id="item_assistant",
            type="message",
            status="completed",
            response_id="resp_done",
            created_at=1,
            data=MessageData(
                role="assistant",
                agent="claude-native-ui",
                content=[{"type": "output_text", "text": self._assistant_text}],
            ),
        )
        return PagedList(data=[item], first_id=item.id, last_id=item.id, has_more=False)


def _make_subagent_conv(child_id: str, *, wrapper: str, kind: str = "sub_agent") -> Conversation:
    """Build a sub-agent conversation row for terminal-delivery relay tests.

    :param child_id: Child session id, e.g. ``"conv_cc_child"``.
    :param wrapper: ``omnigent.wrapper`` label, e.g.
        ``"claude-code-native-ui"`` or ``"codex-native-ui"``.
    :param kind: Conversation kind, ``"sub_agent"`` or ``"default"``.
    :returns: A real :class:`Conversation` carrying the wrapper label.
    """
    return Conversation(
        id=child_id,
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_parent",
        parent_conversation_id="conv_parent",
        agent_id="ag_test",
        kind=kind,
        labels={"omnigent.ui": "terminal", "omnigent.wrapper": wrapper},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper, kind, status",
    [
        ("claude-code-native-ui", "sub_agent", "idle"),  # the reported bug
        ("claude-code-native-ui", "sub_agent", "failed"),
        ("codex-native-ui", "sub_agent", "idle"),
        ("claude-code-native-ui", "default", "idle"),  # top-level
    ],
)
async def test_relay_never_delivers_terminal_on_pty_status(
    wrapper: str,
    kind: str,
    status: str,
) -> None:
    """The PTY-activity ``session.status`` edge never triggers sub-agent delivery.

    Regression guard for the premature-completion bug. claude-native's PTY
    ``idle`` is a ~1s-quiescence heuristic that oscillates on every mid-turn
    lull (thinking, an API round-trip, a long Bash). Bridging it to terminal
    delivery (the old relay forward) reported ``finished (completed)`` to the
    orchestrator mid-turn and idempotently locked out the child's real
    completion. Terminal delivery now rides the ``Stop`` / ``StopFailure``
    hook (``external_session_status``, the codex-shared path), so the relay
    must forward nothing on a PTY status edge — it only republishes the UI
    status. Were the relay to deliver here, ``posts`` would be non-empty.
    """
    from omnigent.server.routes.sessions import _relay_runner_stream

    child_id = "conv_relay_nodeliver"
    store = _SubagentTerminalStore(
        _make_subagent_conv(child_id, wrapper=wrapper, kind=kind),
        assistant_text="work in progress",
    )
    client = _StreamAndCaptureRunnerClient(
        [_sse_frame({"type": "session.status", "status": status}), "data: [DONE]\n\n"]
    )

    await _relay_runner_stream(child_id, client, store)  # type: ignore[arg-type]

    assert client.posts == []
