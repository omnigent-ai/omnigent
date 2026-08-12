"""Tests for the GitHub MCP stdio proxy (omnigent.github_mcp_proxy).

Covers the two robustness behaviors the proxy must guarantee: it refreshes the
brokered token on a 401 (a stdio session can outlive the ~8h token), and it
degrades to an empty server — never a crashed subprocess — when GitHub is
unreachable or the token is missing/rejected.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent import github_mcp_proxy as proxy


def test_broker_auth_attaches_cached_token() -> None:
    auth = proxy._BrokerAuth("tok_old")
    flow = auth.auth_flow(httpx.Request("GET", "https://api.example/mcp/"))
    first = next(flow)
    assert first.headers["Authorization"] == "Bearer tok_old"
    # A 2xx ends the flow with no retry (no extra broker fetch).
    with pytest.raises(StopIteration):
        flow.send(httpx.Response(200))


def test_broker_auth_refreshes_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "github_mcp_token", lambda: "tok_new")
    auth = proxy._BrokerAuth("tok_old")
    flow = auth.auth_flow(httpx.Request("GET", "https://api.example/mcp/"))
    first = next(flow)
    assert first.headers["Authorization"] == "Bearer tok_old"
    # 401 → re-fetch from the broker and retry once with the fresh token.
    retried = flow.send(httpx.Response(401))
    assert retried.headers["Authorization"] == "Bearer tok_new"
    with pytest.raises(StopIteration):
        flow.send(httpx.Response(200))


def test_broker_auth_no_retry_when_token_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 401 with the same token still available → don't loop; give up after one.
    monkeypatch.setattr(proxy, "github_mcp_token", lambda: "tok_old")
    auth = proxy._BrokerAuth("tok_old")
    flow = auth.auth_flow(httpx.Request("GET", "https://api.example/mcp/"))
    next(flow)
    with pytest.raises(StopIteration):
        flow.send(httpx.Response(401))


@pytest.mark.asyncio
async def test_serve_degrades_to_empty_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy, "github_mcp_token", lambda: None)
    served_empty = False

    async def _fake_empty() -> None:
        nonlocal served_empty
        served_empty = True

    monkeypatch.setattr(proxy, "_serve_empty", _fake_empty)
    await proxy._serve("https://omni.example/c/sess")
    assert served_empty is True


@pytest.mark.asyncio
async def test_serve_degrades_to_empty_on_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A rejected token / unreachable GitHub during setup must not crash the
    # harness's MCP startup — it degrades to the empty server.
    monkeypatch.setattr(proxy, "github_mcp_token", lambda: "tok")

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("GitHub unreachable")

    monkeypatch.setattr(proxy, "streamablehttp_client", _boom)
    served_empty = False

    async def _fake_empty() -> None:
        nonlocal served_empty
        served_empty = True

    monkeypatch.setattr(proxy, "_serve_empty", _fake_empty)
    await proxy._serve("https://omni.example/c/sess")
    assert served_empty is True
