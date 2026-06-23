"""Tests for serving the Web UI + API under a base-path prefix (issue #1031)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as app_module
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("/", ""),
        ("/proxy/6767", "/proxy/6767"),
        ("/proxy/6767/", "/proxy/6767"),
        ("proxy/6767", "/proxy/6767"),
        ("  /proxy/6767  ", "/proxy/6767"),
        ("/absproxy/6767//", "/absproxy/6767"),
    ],
)
def test_normalize_base_path(raw: str | None, expected: str) -> None:
    """The base path is normalized to leading-slash, no-trailing-slash (or '')."""
    assert app_module._normalize_base_path(raw) == expected


def test_rewrite_web_ui_index_absolute_when_no_base() -> None:
    """With no base, relative Vite asset refs become root-absolute and no global is injected."""
    html = (
        '<!doctype html><head><link rel="icon" href="./favicon.svg" />'
        '<script type="module" src="./assets/index-AbCd1234.js"></script></head>'
    )
    out = app_module._rewrite_web_ui_index(html, "")
    assert 'src="/assets/index-AbCd1234.js"' in out
    assert 'href="/favicon.svg"' in out
    assert "./assets/" not in out
    assert "__OMNIGENT_BASE_PATH__" not in out


def test_rewrite_web_ui_index_prefixes_and_injects_with_base() -> None:
    """With a base, asset refs are prefixed and the base path is injected for the SPA."""
    html = (
        "<!doctype html><head>"
        '<link rel="icon" href="./favicon.svg" />'
        '<script type="module" src="./assets/index-AbCd1234.js"></script></head>'
    )
    out = app_module._rewrite_web_ui_index(html, "/proxy/6767")
    assert 'src="/proxy/6767/assets/index-AbCd1234.js"' in out
    assert 'href="/proxy/6767/favicon.svg"' in out
    assert 'window.__OMNIGENT_BASE_PATH__ = "/proxy/6767"' in out
    # The injected global must precede the entry module script so it runs first.
    assert out.index("__OMNIGENT_BASE_PATH__") < out.index("assets/index-AbCd1234.js")


# --------------------------------------------------------------------------- #
# BasePathMiddleware (pure ASGI)
# --------------------------------------------------------------------------- #


async def _run_middleware(base_path: str, scope: dict[str, Any]) -> dict[str, Any]:
    """Drive BasePathMiddleware over ``scope`` and return the scope the inner app saw."""
    seen: dict[str, Any] = {}

    async def inner(s: dict[str, Any], receive: Any, send: Any) -> None:
        seen["path"] = s.get("path")
        seen["root_path"] = s.get("root_path")
        seen["raw_path"] = s.get("raw_path")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(_message: dict[str, Any]) -> None:
        return None

    mw = app_module.BasePathMiddleware(inner, base_path)
    await mw(scope, receive, send)
    return seen


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
async def test_base_path_middleware_strips_prefix(scope_type: str) -> None:
    """A request carrying the prefix is rewritten to the canonical path + root_path."""
    seen = await _run_middleware(
        "/proxy/6767",
        {
            "type": scope_type,
            "path": "/proxy/6767/v1/sessions",
            "raw_path": b"/proxy/6767/v1/sessions",
        },
    )
    assert seen["path"] == "/v1/sessions"
    assert seen["root_path"] == "/proxy/6767"
    assert seen["raw_path"] == b"/v1/sessions"


async def test_base_path_middleware_maps_bare_prefix_to_root() -> None:
    """The bare prefix (no trailing slash) maps to '/'."""
    seen = await _run_middleware(
        "/proxy/6767", {"type": "http", "path": "/proxy/6767", "raw_path": b"/proxy/6767"}
    )
    assert seen["path"] == "/"


async def test_base_path_middleware_passes_through_unprefixed_path() -> None:
    """A path that does NOT carry the prefix (stripping proxy) is left untouched."""
    seen = await _run_middleware(
        "/proxy/6767", {"type": "http", "path": "/v1/sessions", "raw_path": b"/v1/sessions"}
    )
    assert seen["path"] == "/v1/sessions"
    assert seen["root_path"] is None


async def test_base_path_middleware_does_not_match_partial_segment() -> None:
    """`/proxy/67670` must not be treated as living under `/proxy/6767`."""
    seen = await _run_middleware(
        "/proxy/6767", {"type": "http", "path": "/proxy/67670", "raw_path": b"/proxy/67670"}
    )
    assert seen["path"] == "/proxy/67670"


async def test_base_path_middleware_noop_without_base() -> None:
    """With no configured base, the middleware is a pure pass-through."""
    seen = await _run_middleware(
        "",
        {
            "type": "http",
            "path": "/proxy/6767/v1/sessions",
            "raw_path": b"/proxy/6767/v1/sessions",
        },
    )
    assert seen["path"] == "/proxy/6767/v1/sessions"
    assert seen["root_path"] is None


# --------------------------------------------------------------------------- #
# End-to-end through the real app
# --------------------------------------------------------------------------- #


def _make_web_ui(tmp_path: Path) -> Path:
    """Write a minimal Vite-style build (relative asset refs) and return its dir."""
    web_ui_dist = tmp_path / "web-ui"
    assets_dir = web_ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    (web_ui_dist / "index.html").write_text(
        "<!doctype html><html><head>"
        '<link rel="icon" href="./favicon.svg" />'
        '<script type="module" crossorigin src="./assets/index-AbCd1234.js"></script>'
        "</head><body><div id='root'></div></body></html>"
    )
    (assets_dir / "index-AbCd1234.js").write_text("console.log('hi');")
    (web_ui_dist / "favicon.svg").write_text("<svg/>")
    return web_ui_dist


def _make_app(db_uri: str, tmp_path: Path, base_path: str | None) -> Any:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        base_path=base_path,
    )


async def test_web_ui_served_under_base_path(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SPA, its assets, and the API are reachable under the prefixed path."""
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", _make_web_ui(tmp_path))
    app = _make_app(db_uri, tmp_path, "/proxy/6767")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/proxy/6767/")
        deep = await client.get("/proxy/6767/c/session_123")
        asset = await client.get("/proxy/6767/assets/index-AbCd1234.js")
        favicon = await client.get("/proxy/6767/favicon.svg")
        info = await client.get("/proxy/6767/v1/info")

    assert root.status_code == 200
    assert 'window.__OMNIGENT_BASE_PATH__ = "/proxy/6767"' in root.text
    assert 'src="/proxy/6767/assets/index-AbCd1234.js"' in root.text
    assert "./assets/" not in root.text
    # Deep client-route refresh serves the same prefixed shell.
    assert deep.status_code == 200
    assert 'src="/proxy/6767/assets/index-AbCd1234.js"' in deep.text
    assert asset.status_code == 200
    assert favicon.status_code == 200
    # The API is reachable through the prefix (middleware strips it before routing).
    assert info.status_code == 200


async def test_unprefixed_paths_still_served_under_base_path(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stripping proxy (code-server /proxy/) delivers unprefixed paths — still served."""
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", _make_web_ui(tmp_path))
    app = _make_app(db_uri, tmp_path, "/proxy/6767")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        info = await client.get("/v1/info")
    assert root.status_code == 200
    # The shell still advertises the configured base so the SPA prefixes its calls.
    assert 'window.__OMNIGENT_BASE_PATH__ = "/proxy/6767"' in root.text
    assert info.status_code == 200


async def test_root_deployment_unchanged_without_base_path(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (no base) serves an absolute-asset shell with no injected global."""
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", _make_web_ui(tmp_path))
    app = _make_app(db_uri, tmp_path, None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
    assert root.status_code == 200
    assert 'src="/assets/index-AbCd1234.js"' in root.text
    assert "./assets/" not in root.text
    assert "__OMNIGENT_BASE_PATH__" not in root.text


async def test_base_path_read_from_env(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no base_path arg is given, OMNIGENT_WEB_BASE_PATH is honored."""
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", _make_web_ui(tmp_path))
    monkeypatch.setenv("OMNIGENT_WEB_BASE_PATH", "/absproxy/6767")
    app = _make_app(db_uri, tmp_path, None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/absproxy/6767/")
    assert root.status_code == 200
    assert 'window.__OMNIGENT_BASE_PATH__ = "/absproxy/6767"' in root.text
