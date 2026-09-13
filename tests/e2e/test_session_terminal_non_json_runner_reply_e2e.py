"""E2E: a non-JSON runner reply must not fail session requests with HTTP 500.

A user opens a session bound to a runner and then opens/creates a terminal
(shell) in it. The server proxies the create-terminal POST to the runner via
``_proxy_post_to_runner``. When the runner -- or an intermediary between the
server and the runner (e.g. a Databricks Apps relay) -- answers with a
**non-JSON body** (an empty body, or an HTML ``502 Bad Gateway`` page), an
unguarded ``resp.json()`` raises ``json.JSONDecodeError`` uncaught. It bubbles
to ``omnigent.server.app._handle_unhandled_exception``, which logs
``Unhandled exception: Expecting value: line 1 column 1 (char 0)`` and returns
HTTP 500. The sibling GET proxy already maps that reply to a graceful
``HTTPException(502, "runner resource endpoint returned invalid JSON")``.

Reproduction strategy: drive the **real** ``create_session_terminal`` route
(``POST /v1/sessions/{id}/resources/terminals``) through the real
``create_sessions_router``, with the runner boundary fault-injected -- a runner
client that returns a non-JSON body, standing in for the failing intermediary.
The fault lives below the browser layer, so this is exercised at the API
surface. A production-faithful catch-all mirroring
``_handle_unhandled_exception`` is installed so the observable outcome (HTTP 500
plus the ``Unhandled exception: ...`` log) matches production exactly.

Fail -> pass contract:

* **Before the fix (RED):** the unguarded ``resp.json()`` raises, the catch-all
  fires -> HTTP 500 ``internal_error`` and an ``Unhandled exception: Expecting
  value: line 1 column 1 (char 0)`` log record.
* **After the fix (GREEN):** the POST proxy handles the non-JSON reply the same
  way the GET proxy already does -> a graceful HTTP 502, no unhandled-exception
  log.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import (
    _globals,
    set_runner_client,
    set_runner_direct_attach_resolver,
    set_runner_router,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.sessions import create_sessions_router

# A claude-native terminal session: the native-bootstrap request shape
# (terminal="claude", session_key="main", ensure_native_terminal=True) is
# exempt from the agent-spec terminal-declaration gate, so it reaches the
# runner proxy without needing a full agent spec loaded -- the same path the
# web UI uses to open the session's native terminal.
_SESSION_ID = "64a784c3aa907d1774f44313546947c6"
_TERMINALS_PATH = f"/v1/sessions/{_SESSION_ID}/resources/terminals"
_UNHANDLED_LOG_PREFIX = "Unhandled exception"


class _ConversationStore:
    """Minimal in-memory conversation store with one native-terminal session."""

    def __init__(self) -> None:
        self._conversations = {
            _SESSION_ID: Conversation(
                id=_SESSION_ID,
                created_at=1,
                updated_at=1,
                root_conversation_id=_SESSION_ID,
                agent_id="087b7cb7ac30abf4debfaa578d052ec6",
                labels={
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "claude-code-native-ui",
                },
            )
        }

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Return the canned conversation, or ``None``.

        :param conversation_id: Conversation/session id.
        :returns: The conversation row, or ``None`` when unknown.
        """
        return self._conversations.get(conversation_id)


class _NonJsonRunnerClient:
    """Fake runner ``httpx.AsyncClient`` returning a non-JSON body.

    Stands in for the runner -- or an intermediary relay -- that answers the
    terminal-create proxy with a body that is not JSON. Every HTTP verb
    returns the same canned non-JSON
    response so the reproduction does not depend on which verb the route uses.

    :param body: Raw response body text (e.g. ``""`` for an empty body, or an
        HTML error page).
    :param status_code: HTTP status the runner/intermediary returned.
    :param content_type: ``Content-Type`` header for the canned response.
    """

    def __init__(self, *, body: str, status_code: int, content_type: str) -> None:
        self._body = body
        self._status_code = status_code
        self._content_type = content_type
        self.calls: list[tuple[str, str]] = []

    def _response(self, method: str, url: str) -> httpx.Response:
        """Record the call and build the canned non-JSON response.

        :param method: HTTP method, e.g. ``"POST"``.
        :param url: Request URL path.
        :returns: The canned non-JSON ``httpx.Response``.
        """
        self.calls.append((method, url))
        return httpx.Response(
            status_code=self._status_code,
            text=self._body,
            headers={"content-type": self._content_type},
            request=httpx.Request(method, url),
        )

    async def get(
        self, url: str, *, params: Any = None, timeout: float | None = None
    ) -> httpx.Response:
        """Return the canned non-JSON GET response."""
        del params, timeout
        return self._response("GET", url)

    async def post(
        self, url: str, *, json: Any = None, timeout: float | None = None
    ) -> httpx.Response:
        """Return the canned non-JSON POST response."""
        del json, timeout
        return self._response("POST", url)

    async def put(
        self, url: str, *, json: Any = None, timeout: float | None = None
    ) -> httpx.Response:
        """Return the canned non-JSON PUT response."""
        del json, timeout
        return self._response("PUT", url)

    async def patch(
        self, url: str, *, json: Any = None, timeout: float | None = None
    ) -> httpx.Response:
        """Return the canned non-JSON PATCH response."""
        del json, timeout
        return self._response("PATCH", url)

    async def delete(self, url: str, *, timeout: float | None = None) -> httpx.Response:
        """Return the canned non-JSON DELETE response."""
        del timeout
        return self._response("DELETE", url)


class _RoutedRunner:
    """A resolved runner binding (id + client) for the router stub."""

    def __init__(self, client: _NonJsonRunnerClient) -> None:
        self.runner_id = "runner_one"
        self.client = client


class _FakeRunnerRouter:
    """Router stub that always resolves to the non-JSON runner client."""

    def __init__(self, client: _NonJsonRunnerClient) -> None:
        self.client = client

    def client_for_session_resources(
        self, session_id: str, *, conversation: Conversation | None = None
    ) -> _RoutedRunner:
        """Resolve the runner client for resource access.

        :param session_id: Conversation/session id.
        :param conversation: Pre-loaded conversation (ignored by the stub).
        :returns: The routed non-JSON runner.
        """
        del session_id, conversation
        return _RoutedRunner(self.client)

    def client_for_existing_conversation(self, session_id: str) -> _RoutedRunner:
        """Resolve the pinned runner for an existing conversation.

        :param session_id: Conversation/session id.
        :returns: The routed non-JSON runner.
        """
        del session_id
        return _RoutedRunner(self.client)


@pytest.fixture
def runner_globals_reset() -> Iterator[None]:
    """Save/restore the process-global runner client + router + resolver."""
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    prior_direct = _globals._runner_direct_attach_resolver
    set_runner_client(None)
    set_runner_router(None)
    set_runner_direct_attach_resolver(None)
    yield
    set_runner_client(prior_client)
    set_runner_router(prior_router)
    set_runner_direct_attach_resolver(prior_direct)


@pytest.fixture
def app(runner_globals_reset: None) -> FastAPI:
    """Build the real sessions router app with a production-faithful catch-all.

    The ``Exception`` handler mirrors
    ``omnigent.server.app._handle_unhandled_exception`` (same logger name,
    same ``"Unhandled exception: %s"`` message, same 500 ``internal_error``
    envelope) so an unguarded ``resp.json()`` surfaces as the same HTTP 500 +
    log line production emits, rather than a bare re-raise.

    :param runner_globals_reset: Ensures a clean runner-globals slate.
    :returns: The configured FastAPI app.
    """
    del runner_globals_reset
    application = FastAPI()
    conversation_store = _ConversationStore()
    host_registry = HostRegistry()
    server_logger = logging.getLogger("omnigent.server.app")

    @application.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @application.exception_handler(Exception)
    async def _handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        del request
        server_logger.error("Unhandled exception: %s", exc, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR,
                    "message": "An internal error occurred.",
                }
            },
        )

    class _StubAgentStore:
        """Agent store stub -- no specs, so the native-bootstrap exemption is used."""

        def get(self, agent_id: str) -> None:
            """:param agent_id: Agent id. :returns: ``None`` (no agents)."""
            del agent_id
            return

    application.include_router(
        create_sessions_router(
            conversation_store,  # type: ignore[arg-type]
            _StubAgentStore(),  # type: ignore[arg-type]
            host_registry=host_registry,
        ),
        prefix="/v1",
    )
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an httpx client bound to the app.

    ``raise_app_exceptions=False`` lets the installed catch-all convert an
    unhandled exception into the observed HTTP 500 (as the ASGI server does in
    production) instead of re-raising into the test.

    :param app: The configured FastAPI app.
    :yields: An ``httpx.AsyncClient`` for driving requests.
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as http_client:
        yield http_client


@pytest.mark.parametrize(
    ("body", "content_type", "scenario"),
    [
        # An empty body -> "Expecting value: line 1 column 1 (char 0)".
        ("", "text/html", "empty body"),
        # An intermediary that returns an HTML 502 page instead of JSON.
        ("<html><body>502 Bad Gateway</body></html>", "text/html", "html error page"),
    ],
)
async def test_terminal_create_non_json_runner_reply_is_handled(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    body: str,
    content_type: str,
    scenario: str,
) -> None:
    """A non-JSON runner reply to the terminal-create proxy must be handled.

    Drives the real ``create_session_terminal`` route with a runner client that
    replies with a non-JSON body. Broken behavior: the unguarded
    ``resp.json()`` in ``_proxy_post_to_runner`` raises ``JSONDecodeError``,
    surfacing as HTTP 500 plus an ``Unhandled exception: Expecting value: ...``
    log. Fixed behavior: the route maps the non-JSON reply to a graceful HTTP
    502 (mirroring ``_proxy_get_to_runner``, which already returns 502 "runner
    resource endpoint returned invalid JSON") with no unhandled-exception log.

    :param client: httpx client bound to the real sessions router app.
    :param caplog: Pytest log capture fixture.
    :param body: Non-JSON runner response body.
    :param content_type: ``Content-Type`` of the runner response.
    :param scenario: Human-readable variant label.
    :returns: None.
    """
    runner = _NonJsonRunnerClient(body=body, status_code=502, content_type=content_type)
    set_runner_router(_FakeRunnerRouter(runner))  # type: ignore[arg-type]
    set_runner_client(runner)  # type: ignore[arg-type]

    with caplog.at_level(logging.ERROR, logger="omnigent.server.app"):
        resp = await client.post(
            _TERMINALS_PATH,
            json={
                "terminal": "claude",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        )

    # The real proxy path executed: the terminal-create POST reached the runner.
    assert ("POST", _TERMINALS_PATH) in runner.calls, (
        f"[{scenario}] expected the route to proxy the terminal-create POST to "
        f"the runner; saw calls: {runner.calls!r}"
    )

    unhandled = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(_UNHANDLED_LOG_PREFIX)
    ]

    # Primary signal: the non-JSON reply must NOT escape as an unhandled
    # JSONDecodeError. Broken behavior logs
    # "Unhandled exception: Expecting value: line 1 column 1 (char 0)".
    assert not unhandled, (
        f"[{scenario}] a non-JSON runner reply raised an unhandled exception in "
        f"_proxy_post_to_runner (unguarded resp.json()); server logged {unhandled!r}. "
        "It must be handled like the GET proxy, which returns a graceful 502."
    )

    # The intended graceful mapping: the same 502 the GET proxy already returns
    # for a non-JSON runner reply -- not the unhandled-exception HTTP 500.
    assert resp.status_code == 502, (
        f"[{scenario}] expected a graceful HTTP 502 for a non-JSON runner reply "
        f"(matching _proxy_get_to_runner's 'runner resource endpoint returned "
        f"invalid JSON'); got {resp.status_code}: {resp.text[:200]!r}. "
        "HTTP 500 means the JSONDecodeError is still unhandled."
    )
