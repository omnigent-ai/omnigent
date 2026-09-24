"""Browser-contract fixtures for the built web SPA.

This lane serves static build output with history fallback and replaces every
backend dependency explicitly. It must never start an Omnigent process or let
an unregistered same-origin API, SSE, or WebSocket request reach a server.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from re import Pattern
from typing import Any
from urllib.parse import urlparse

import pytest
from playwright.sync_api import BrowserContext, Request, Route, WebSocketRoute

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD_OUTPUT = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"
_API_PATH = re.compile(r"^/(?:v1(?:/|$)|health(?:\?|$))")
_NETWORK_RESOURCE_TYPES = {"fetch", "xhr", "eventsource"}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("browser-ui")
    group.addoption(
        "--browser-ui-skip-build",
        action="store_true",
        help="Serve the existing built SPA instead of rebuilding web/.",
    )


@pytest.fixture(scope="session", autouse=True)
def _built_spa(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--browser-ui-skip-build"):
        subprocess.run(
            ["pnpm", "--filter", "web", "run", "build"],
            cwd=_REPO_ROOT,
            check=True,
        )
    index = _BUILD_OUTPUT / "index.html"
    if not index.is_file():
        raise RuntimeError(
            f"Built SPA is missing at {index}. Run `pnpm --filter web run build` "
            "or omit --browser-ui-skip-build."
        )


class _SpaHandler(SimpleHTTPRequestHandler):
    """Serve static files and return index.html for client-side routes."""

    def __init__(
        self,
        *args: Any,
        static_paths: frozenset[str],
        **kwargs: Any,
    ) -> None:
        self._static_paths = static_paths
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in self._static_paths:
            super().do_GET()
            return
        if _API_PATH.match(self.path):
            self.send_error(404, "Browser-contract API requests must be mocked")
            return
        self.path = "/index.html"
        super().do_GET()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture(scope="session")
def browser_base_url(_built_spa: None) -> Iterator[str]:
    static_paths = frozenset(
        f"/{path.relative_to(_BUILD_OUTPUT).as_posix()}"
        for path in _BUILD_OUTPUT.rglob("*")
        if path.is_file()
    )
    handler = partial(
        _SpaHandler,
        directory=str(_BUILD_OUTPUT),
        static_paths=static_paths,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


RouteHandler = Callable[[Route], None]
WebSocketHandler = Callable[[WebSocketRoute], None]


@dataclass
class BrowserContract:
    """Explicit browser-backend contract with a default-deny safety net."""

    context: BrowserContext
    base_url: str
    violations: list[str] = field(default_factory=list)

    def route(self, url: str | Pattern[str], handler: RouteHandler) -> None:
        """Register an HTTP mock; later registrations win over the deny route."""
        self.context.route(url, handler)

    def json(
        self,
        path: str | Pattern[str],
        body: Any,
        *,
        method: str = "GET",
        status: int = 200,
    ) -> None:
        """Register a method-specific JSON response for a path or regex."""
        matcher: str | Pattern[str]
        if isinstance(path, str):
            matcher = re.compile(rf"{re.escape(path)}(?:\?.*)?$")
        else:
            matcher = path

        def fulfill(route: Route) -> None:
            if route.request.method != method:
                route.fallback()
                return
            payload = body(route.request) if callable(body) else body
            route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

        self.route(matcher, fulfill)

    def sse(self, path: str | Pattern[str], body: str = "data: [DONE]\n\n") -> None:
        """Register a deterministic server-sent event response."""
        matcher = re.compile(rf"{re.escape(path)}(?:\?.*)?$") if isinstance(path, str) else path
        self.route(
            matcher,
            lambda route: route.fulfill(
                status=200,
                content_type="text/event-stream",
                body=body,
            ),
        )

    def response(
        self,
        path: str | Pattern[str],
        *,
        method: str = "GET",
        status: int = 204,
        body: str = "",
    ) -> None:
        """Register a method-specific response that does not need JSON."""
        matcher = re.compile(rf"{re.escape(path)}(?:\?.*)?$") if isinstance(path, str) else path

        def fulfill(route: Route) -> None:
            if route.request.method != method:
                route.fallback()
                return
            route.fulfill(status=status, body=body)

        self.route(matcher, fulfill)

    def websocket(self, url: str | Pattern[str], handler: WebSocketHandler) -> None:
        """Register an in-page WebSocket mock."""
        self.context.route_web_socket(url, handler)


def _same_origin(url: str, base_url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(base_url)
    return parsed.hostname == base.hostname and parsed.port == base.port


@pytest.fixture
def browser_contract(
    context: BrowserContext,
    browser_base_url: str,
) -> Iterator[BrowserContract]:
    contract = BrowserContract(context=context, base_url=browser_base_url)

    def deny_http(route: Route, request: Request) -> None:
        parsed = urlparse(request.url)
        is_contract_request = request.resource_type in _NETWORK_RESOURCE_TYPES or bool(
            _API_PATH.match(parsed.path)
        )
        if _same_origin(request.url, browser_base_url) and is_contract_request:
            contract.violations.append(f"{request.method} {request.resource_type} {request.url}")
            route.abort("blockedbyclient")
            return
        route.continue_()

    def deny_websocket(socket: WebSocketRoute) -> None:
        if _same_origin(socket.url, browser_base_url):
            contract.violations.append(f"WEBSOCKET {socket.url}")
            # Not connecting is the WebSocket equivalent of aborting an HTTP
            # route: no packet can reach a backend. The teardown assertion
            # turns the recorded dependency into a deterministic test failure.
            socket.on_message(lambda _message: None)
            return
        socket.connect_to_server()

    # Playwright gives the most recently registered matching route precedence,
    # so tests can add concise explicit mocks on top of these catch-alls.
    context.route("**/*", deny_http)
    context.route_web_socket("**/*", deny_websocket)
    try:
        yield contract
    finally:
        context.unroute_all(behavior="wait")
        assert not contract.violations, "Unregistered browser dependencies:\n" + "\n".join(
            contract.violations
        )
