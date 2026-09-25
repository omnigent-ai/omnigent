"""Browser-level coverage for serving the Web UI behind a reverse-proxy
subpath (``OMNIGENT_WEB_BASE_PATH`` / ``omnigent server --base-path``).

Server-side rewriting and middleware routing are unit/integration-tested in
``tests/server/integration/test_base_path.py`` (httpx against the ASGI app,
no browser). These tests instead drive a real Chromium instance against a
real server to prove the piece those tests can't: that the *browser* actually
resolves the SPA's asset/API/WebSocket URLs correctly under a prefix,
including on a deep-linked route refresh — the scenario the relative
(``base: "./"``) Vite build depends on the server-side ``index.html`` rewrite
for (see ``_rewrite_web_ui_index`` in ``omnigent/server/app.py`` and the
``<base href="/">`` fallback in ``web/index.html``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, Request, expect

from tests.e2e_ui.base_path._base_path_server import BasePathServer, spawn_base_path_server

_BASE_PATH = "/proxy/6767"


@pytest.fixture(scope="module")
def base_path_server(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[BasePathServer]:
    """A dedicated server mounted at :data:`_BASE_PATH`."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_base_path")
    yield from spawn_base_path_server(server_tmp, _BASE_PATH)


def _failed_asset_requests(requests: list[Request]) -> list[Request]:
    """Requests for a JS/CSS asset that didn't come back 200."""
    failed = []
    for req in requests:
        if "/assets/" not in req.url:
            continue
        response = req.response()
        if response is None or response.status != 200:
            failed.append(req)
    return failed


def test_root_loads_under_base_path(base_path_server: BasePathServer, page: Page) -> None:
    """The SPA shell, its assets, and the API are all reachable under the prefix.

    Covers the base case the relative-asset build depends on the server
    rewrite for: without it, the browser would 404 every ``./assets/...``
    reference (it resolves relative to the *current* URL, which already
    includes the prefix) and the page would stay blank.
    """
    requests: list[Request] = []
    page.on("request", lambda req: requests.append(req))

    page.goto(f"{base_path_server.prefixed_url}/")

    # The React app actually mounted — a blank/errored boot leaves #root empty.
    expect(page.locator("#root")).not_to_be_empty()
    assert page.evaluate("window.__OMNIGENT_BASE_PATH__") == base_path_server.base_path
    assert not _failed_asset_requests(requests)


def test_deep_link_navigation_loads_assets_under_base_path(
    base_path_server: BasePathServer, page: Page
) -> None:
    """A direct navigation (refresh-equivalent) to a nested route still resolves
    assets correctly under the prefix.

    This is the regression case: a relative asset ref resolved against a
    nested path like ``{base}/c/<id>`` (one level deeper than the shell's own
    URL) is wrong unless the server rewrites it to an absolute, prefixed path.
    """
    requests: list[Request] = []
    page.on("request", lambda req: requests.append(req))

    fake_session_id = uuid.uuid4().hex
    page.goto(f"{base_path_server.prefixed_url}/c/{fake_session_id}")

    expect(page.locator("#root")).not_to_be_empty()
    assert not _failed_asset_requests(requests)


def test_websocket_and_api_urls_are_prefixed(base_path_server: BasePathServer, page: Page) -> None:
    """The always-on sessions-updates WebSocket and its REST calls carry the
    configured base path, proving ``hostFetch``/``resolveWebSocketUrl`` pick up
    ``withBasePath`` rather than hitting the origin root.
    """
    api_requests: list[str] = []
    page.on(
        "request",
        lambda req: api_requests.append(req.url) if "/v1/" in req.url else None,
    )
    ws_urls: list[str] = []
    page.on("websocket", lambda ws: ws_urls.append(ws.url))

    page.goto(f"{base_path_server.prefixed_url}/")
    # SessionUpdatesProvider opens WS /v1/sessions/updates unconditionally on
    # mount; give it a moment to connect.
    expect(page.locator("#root")).not_to_be_empty()
    page.wait_for_timeout(2_000)

    assert ws_urls, "expected the sessions-updates WebSocket to connect"
    assert all(urlsplit(url).path.startswith(base_path_server.base_path) for url in ws_urls)
    assert api_requests, "expected at least one /v1/... REST call"
    assert all(urlsplit(url).path.startswith(base_path_server.base_path) for url in api_requests)
