"""Unit tests for the gateway servlet's catalog translation and state file."""

from __future__ import annotations

import os

import pytest

from omnigent.gateway.auth import databrickscfg_host_for_profile
from omnigent.gateway.catalog import (
    build_models_response,
    catalog_etag,
    codex_slug,
    dumps_catalog,
    picker_options,
    routable_models,
    service_id_for_slug,
)
from omnigent.gateway.state import (
    ServletState,
    clear_servlet_state,
    read_servlet_state,
    read_session_registry,
    write_servlet_state,
    write_session_registry,
)


@pytest.mark.parametrize(
    ("service_id", "expected"),
    [
        ("system.ai.gpt-5-6-sol", "gpt-5.6-sol"),
        ("system.ai.gpt-5-5", "gpt-5.5"),
        ("system.ai.gpt-5-5-pro", "gpt-5.5-pro"),
        ("system.ai.gpt-5", "gpt-5"),
        ("system.ai.gpt-5-mini", "gpt-5-mini"),
        ("system.ai.gpt-5-4-nano", "gpt-5.4-nano"),
        ("system.ai.gpt-5-3-codex", "gpt-5.3-codex"),
        # Non-mainline ids stay verbatim (no native Codex metadata exists).
        ("system.ai.gpt-oss-120b", "system.ai.gpt-oss-120b"),
        ("system.ai.glm-5-2", "glm-5-2"),
    ],
)
def test_codex_slug(service_id: str, expected: str) -> None:
    assert codex_slug(service_id) == expected


def _native_catalog() -> dict:
    levels = [{"effort": e} for e in ("low", "medium", "high", "xhigh")]
    return {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6 Sol",
                "description": "Latest frontier agentic coding model.",
                "supported_reasoning_levels": levels,
                "default_reasoning_level": "medium",
                "priority": 0,
                "visibility": "list",
                "base_instructions": "instr",
                "tool_mode": "code_mode_only",
                "multi_agent_version": "v2",
                "use_responses_lite": True,
            },
            {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "description": "Frontier model.",
                "supported_reasoning_levels": levels,
                "default_reasoning_level": "medium",
                "priority": 1,
                "visibility": "list",
                "base_instructions": "instr",
            },
        ]
    }


def test_build_models_response_orders_and_enriches() -> None:
    service_ids = [
        "system.ai.glm-5-2",
        "system.ai.gpt-5-5",
        "system.ai.gpt-5-6-luna",
        "system.ai.gpt-5-6-sol",
    ]
    response = build_models_response(service_ids, _native_catalog())
    assert response is not None
    slugs = [m["slug"] for m in response["models"]]
    # Native-priority order first (sol, 5.5), then unknown mainline
    # newest-first (luna), then non-mainline verbatim ids.
    assert slugs == ["gpt-5.6-sol", "gpt-5.5", "gpt-5.6-luna", "glm-5-2"]
    assert [m["priority"] for m in response["models"]] == [0, 1, 2, 3]
    sol = response["models"][0]
    assert sol["display_name"] == "GPT-5.6 Sol"
    assert sol["description"] == "Databricks AI Gateway (system.ai.gpt-5-6-sol)"
    luna = response["models"][2]
    # Synthesized entry: template clone with a clamped effort ladder.
    assert luna["display_name"] == "gpt-5.6-luna"
    assert "system.ai.gpt-5-6-luna" in luna["description"]
    assert [lvl["effort"] for lvl in luna["supported_reasoning_levels"]] == [
        "low",
        "medium",
        "high",
    ]
    assert luna["default_reasoning_level"] == "medium"

    options = picker_options(response)
    assert options[0] == {
        "id": "gpt-5.6-sol",
        "model": "system.ai.gpt-5-6-sol",
        "displayName": "GPT-5.6 Sol",
        "isDefault": True,
        "description": "Databricks AI Gateway (system.ai.gpt-5-6-sol)",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
            {"reasoningEffort": "xhigh"},
        ],
    }
    assert all("isDefault" not in option for option in options[1:])
    assert routable_models(response) == slugs


def test_build_models_response_requires_native_and_inventory() -> None:
    assert build_models_response(["system.ai.gpt-5-5"], None) is None
    assert build_models_response(["system.ai.gpt-5-5"], {"models": []}) is None
    assert build_models_response([], _native_catalog()) is None


def test_catalog_etag_deterministic() -> None:
    response = build_models_response(["system.ai.gpt-5-5"], _native_catalog())
    assert response is not None
    first = catalog_etag(dumps_catalog(response))
    second = catalog_etag(dumps_catalog(response))
    assert first == second
    assert first.startswith('"') and first.endswith('"')


def test_servlet_state_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert read_servlet_state() is None
    pid = os.getpid()
    write_servlet_state(ServletState(url="http://127.0.0.1:5", admin_token="t", pid=pid))
    state = read_servlet_state()
    assert state == ServletState(url="http://127.0.0.1:5", admin_token="t", pid=pid)
    # A different owner must not clear a newer daemon's file.
    clear_servlet_state(owner_pid=999)
    assert read_servlet_state() is not None
    clear_servlet_state(owner_pid=pid)
    assert read_servlet_state() is None


def test_servlet_state_stale_owner_reads_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    write_servlet_state(ServletState(url="http://127.0.0.1:6768", admin_token="t", pid=4))
    monkeypatch.setattr("omnigent.gateway.state._pid_alive", lambda pid: False)
    # Launchers see no servlet (fall open without a connect timeout)…
    assert read_servlet_state() is None
    # …but the next daemon start can still read the port to reclaim it,
    # and the dead owner's pid still matches for retraction.
    stale = read_servlet_state(allow_stale=True)
    assert stale is not None and stale.url.endswith(":6768")
    clear_servlet_state(owner_pid=4)
    assert read_servlet_state(allow_stale=True) is None


def test_session_registry_roundtrip_and_cap(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert read_session_registry() == {}
    write_session_registry(
        {
            "tok1": {"profile": "oss", "workspace_host": "https://a.example"},
            "bad": {"profile": ""},  # dropped on read
        }
    )
    assert read_session_registry() == {
        "tok1": {"profile": "oss", "workspace_host": "https://a.example"}
    }
    # Cap keeps the newest entries (insertion order).
    write_session_registry(
        {f"tok{i}": {"profile": "p", "workspace_host": "https://a.example"} for i in range(600)}
    )
    loaded = read_session_registry()
    assert len(loaded) == 512
    assert "tok599" in loaded and "tok0" not in loaded


def test_registry_restores_into_new_servlet(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from omnigent.gateway.servlet import GatewayServlet

    first = GatewayServlet()
    session = first.register_session("oss", "https://ws.example")
    # A fresh servlet (post-restart) restores the same token → session map.
    second = GatewayServlet()
    restored = second._sessions[session.token]
    assert restored.profile == "oss"
    assert restored.upstream_base == "https://ws.example/ai-gateway/codex/v1"


def test_databrickscfg_host_for_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert databrickscfg_host_for_profile("oss") is None
    (tmp_path / ".databrickscfg").write_text(
        "[oss]\nhost = https://ws.example/\nauth_type = databricks-cli\n"
    )
    assert databrickscfg_host_for_profile("oss") == "https://ws.example"
    assert databrickscfg_host_for_profile("missing") is None


def test_fetch_includes_translated_arms(monkeypatch) -> None:
    """Chat-only ids in the translated-arm set are served; other chat-only
    ids stay excluded."""
    import asyncio

    from omnigent.gateway.catalog import fetch_codex_service_ids

    payload = {
        "model_services": [
            {
                "name": "model-services/system.ai.gpt-5-6-sol",
                "supported_api_types": ["mlflow/v1/chat/completions", "openai/v1/responses"],
            },
            {
                "name": "model-services/system.ai.glm-5-2",
                "supported_api_types": ["mlflow/v1/chat/completions"],
            },
            {
                "name": "model-services/system.ai.llama-4-maverick",
                "supported_api_types": ["mlflow/v1/chat/completions"],
            },
        ]
    }

    class _Resp:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return payload

    class _Client:
        async def get(self, url: str, params: dict, headers: dict) -> _Resp:
            return _Resp()

    ids = asyncio.run(fetch_codex_service_ids(_Client(), "https://ws.example", "tok"))
    assert ids == ["system.ai.glm-5-2", "system.ai.gpt-5-6-sol"]


def test_glm_arm_row_is_verbatim_and_never_default() -> None:
    response = build_models_response(
        ["system.ai.gpt-5-6-sol", "system.ai.glm-5-2"], _native_catalog()
    )
    assert response is not None
    slugs = [m["slug"] for m in response["models"]]
    assert slugs == ["gpt-5.6-sol", "glm-5-2"]
    glm = response["models"][1]
    assert glm["display_name"] == "glm-5-2"
    assert [lvl["effort"] for lvl in glm["supported_reasoning_levels"]] == [
        "low",
        "medium",
        "high",
    ]
    options = picker_options(response)
    assert options[0]["isDefault"] is True and options[0]["id"] == "gpt-5.6-sol"
    assert options[1] == {
        "id": "glm-5-2",
        "model": "system.ai.glm-5-2",
        "displayName": "glm-5-2",
        "description": "Databricks AI Gateway (system.ai.glm-5-2)",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
        ],
    }


def test_normalize_relay_model_body_translates_bare_arms() -> None:
    import json as _json

    from omnigent.gateway.catalog import normalize_relay_model_body

    out = normalize_relay_model_body(b'{"model": "glm-5-2", "stream": true}')
    assert _json.loads(out)["model"] == "system.ai.glm-5-2"
    for untouched in (b'{"model": "gpt-5.6-sol"}', b"not json", b"{}"):
        assert normalize_relay_model_body(untouched) == untouched


def test_service_id_for_slug_inverts_codex_slug() -> None:
    """Every served id round-trips slug -> service id (the row's ``model``)."""
    for service_id in (
        "system.ai.gpt-5-6-sol",
        "system.ai.gpt-5-5",
        "system.ai.gpt-5-5-pro",
        "system.ai.gpt-5",
        "system.ai.gpt-5-mini",
        "system.ai.gpt-5-4-nano",
        "system.ai.gpt-5-3-codex",
        "system.ai.glm-5-2",
        "system.ai.gpt-oss-120b",
    ):
        assert service_id_for_slug(codex_slug(service_id)) == service_id


def test_newest_mainline_slug_by_version_never_alphabetical() -> None:
    from omnigent.gateway.catalog import newest_mainline_slug

    assert (
        newest_mainline_slug(["system.ai.gpt-5", "system.ai.gpt-5-5", "system.ai.gpt-5-6-luna"])
        == "gpt-5.6-luna"
    )
    # Non-GPT and non-mainline ids are never candidates.
    assert newest_mainline_slug(["system.ai.glm-5-2", "system.ai.gpt-oss-120b"]) is None
    assert newest_mainline_slug([]) is None


def test_servlet_client_fails_open_without_state(tmp_path, monkeypatch) -> None:
    from omnigent.gateway.client import fetch_servlet_codex_slugs

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert fetch_servlet_codex_slugs("oss") is None
    assert fetch_servlet_codex_slugs(None) is None


def test_servlet_client_reads_routable_slugs(tmp_path, monkeypatch) -> None:
    from omnigent.gateway import client as gateway_client
    from omnigent.gateway.state import ServletState

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".databrickscfg").write_text("[oss]\nhost = https://ws.example\n")
    write_servlet_state(
        ServletState(url="http://127.0.0.1:6768", admin_token="tok", pid=os.getpid())
    )
    seen: dict[str, object] = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"models": [], "routable_models": ["gpt-5.6-sol", "glm-5-2", 7]}

    def _get(url, params=None, headers=None, timeout=None):
        seen.update({"url": url, "params": params, "headers": headers})
        return _Resp()

    monkeypatch.setattr(gateway_client.httpx, "get", _get)
    assert gateway_client.fetch_servlet_codex_slugs("oss") == ["gpt-5.6-sol", "glm-5-2"]
    assert seen["url"] == "http://127.0.0.1:6768/admin/catalog"
    assert seen["params"] == {"profile": "oss", "workspace_host": "https://ws.example"}
    assert seen["headers"] == {"authorization": "Bearer tok"}


def test_normalize_relay_model_body_translates_localized_spellings() -> None:
    """``databricks-``-localized ids resolve to the same service on the wire.

    Orchestrator surfaces predating the servlet hand out ``databricks-glm-5-2``;
    a session pinned with that spelling must still run. Claude-style localized
    ids stay untouched — they never route through the codex servlet.
    """
    import json as _json

    from omnigent.gateway.catalog import normalize_relay_model_body

    out = normalize_relay_model_body(b'{"model": "databricks-glm-5-2"}')
    assert _json.loads(out)["model"] == "system.ai.glm-5-2"
    out = normalize_relay_model_body(b'{"model": "databricks-gpt-5-4"}')
    assert _json.loads(out)["model"] == "system.ai.gpt-5-4"
    untouched = b'{"model": "databricks-claude-opus-4-8"}'
    assert normalize_relay_model_body(untouched) == untouched


def test_service_id_for_slug_accepts_localized_spellings() -> None:
    assert service_id_for_slug("databricks-glm-5-2") == "system.ai.glm-5-2"
    assert service_id_for_slug("databricks-gpt-5-6-sol") == "system.ai.gpt-5-6-sol"
    assert service_id_for_slug("databricks-claude-opus-4-8") == "databricks-claude-opus-4-8"


def test_synthesized_arm_entries_do_not_advertise_code_mode() -> None:
    """Arm entries must not clone GPT's Code Mode markers.

    ``tool_mode: code_mode_only`` / ``multi_agent_version: v2`` declare the
    trained-in GPT grammar; advertising them for a translated arm makes codex
    withhold the classic JSON tool set, leaving the model unable to run shell
    or MCP tools.
    """
    response = build_models_response(
        ["system.ai.glm-5-2", "system.ai.gpt-5-6-sol"], _native_catalog()
    )
    assert response is not None
    by_slug = {m["slug"]: m for m in response["models"]}
    assert by_slug["glm-5-2"]["tool_mode"] is None
    assert by_slug["glm-5-2"]["multi_agent_version"] is None
    assert by_slug["glm-5-2"]["supports_search_tool"] is False
    # The lite Responses wire omits the JSON tools array from app-server
    # turns entirely — the fourth GPT-only protocol marker arms must drop.
    assert by_slug["glm-5-2"]["use_responses_lite"] is False
    assert by_slug["gpt-5.6-sol"]["use_responses_lite"] is True
    # Native entries keep their own metadata untouched.
    assert by_slug["gpt-5.6-sol"]["tool_mode"] == _native_catalog()["models"][0]["tool_mode"]


def test_admin_register_rejects_mismatched_workspace_host(tmp_path, monkeypatch) -> None:
    """The minted-bearer destination is always the profile's own host.

    A caller-supplied ``workspace_host`` is only accepted when it matches the
    ``~/.databrickscfg`` resolution — anything else would let a loopback
    caller aim minted credentials at an attacker-chosen origin.
    """
    from starlette.testclient import TestClient

    from omnigent.gateway.servlet import GatewayServlet

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "omnigent.gateway.servlet.databrickscfg_host_for_profile",
        lambda profile: "https://real.example" if profile == "oss" else None,
    )
    servlet = GatewayServlet()
    client = TestClient(servlet.build_app())
    auth = {"authorization": f"Bearer {servlet.admin_token}"}

    ok = client.post("/admin/sessions", json={"profile": "oss"}, headers=auth)
    assert ok.status_code == 200

    matching = client.post(
        "/admin/sessions",
        json={"profile": "oss", "workspace_host": "https://real.example/"},
        headers=auth,
    )
    assert matching.status_code == 200

    hostile = client.post(
        "/admin/sessions",
        json={"profile": "oss", "workspace_host": "https://evil.example"},
        headers=auth,
    )
    assert hostile.status_code == 400

    unknown = client.post("/admin/sessions", json={"profile": "nope"}, headers=auth)
    assert unknown.status_code == 400

    catalog_hostile = client.get(
        "/admin/catalog",
        params={"profile": "oss", "workspace_host": "https://evil.example"},
        headers=auth,
    )
    assert catalog_hostile.status_code == 400


def test_proxy_rejects_dot_segment_traversal(tmp_path, monkeypatch) -> None:
    """``..`` segments must not walk the token off the codex surface.

    httpx normalizes dot segments when building the upstream request, so an
    unchecked path could reach arbitrary same-origin workspace APIs with the
    minted bearer.
    """
    from starlette.testclient import TestClient

    from omnigent.gateway.servlet import GatewayServlet

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    servlet = GatewayServlet()
    session = servlet.register_session("oss", "https://ws.example")
    client = TestClient(servlet.build_app())

    encoded = client.get(f"/g/{session.token}/v1/%2e%2e/%2e%2e/api/2.1/secrets")
    assert encoded.status_code == 404

    literal = client.get(f"/g/{session.token}/v1/../../api/2.1/secrets")
    assert literal.status_code == 404


def _catalog_servlet(monkeypatch, tmp_path):
    """A servlet with a stubbed inventory + native catalog and no disk state."""
    import httpx as _httpx

    from omnigent.gateway.servlet import GatewayServlet

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    async def fake_ids(client, workspace_host, bearer):
        return ["system.ai.glm-5-2", "system.ai.gpt-5-6-sol"]

    monkeypatch.setattr(
        "omnigent.gateway.servlet.fetch_codex_service_ids",
        fake_ids,
    )
    servlet = GatewayServlet(_native_catalog)

    async def fake_bearer(self, profile):
        return f"minted-{profile}"

    monkeypatch.setattr(type(servlet._minter), "bearer", fake_bearer)
    return servlet, _httpx


async def test_models_endpoint_serves_catalog_with_etag(monkeypatch, tmp_path) -> None:
    """A registered session's /models serves the built catalog; ETag → 304."""
    import httpx as _httpx

    servlet, _ = _catalog_servlet(monkeypatch, tmp_path)
    session = servlet.register_session("oss", "https://ws.example")
    transport = _httpx.ASGITransport(app=servlet.build_app())
    async with _httpx.AsyncClient(transport=transport, base_url="http://sv") as client:
        first = await client.get(f"/g/{session.token}/v1/models")
        assert first.status_code == 200
        slugs = [m["slug"] for m in first.json()["models"]]
        assert slugs == ["gpt-5.6-sol", "glm-5-2"]
        etag = first.headers["etag"]
        cached = await client.get(f"/g/{session.token}/v1/models", headers={"if-none-match": etag})
        assert cached.status_code == 304
        unknown = await client.get("/g/not-a-token/v1/models")
        assert unknown.status_code == 404
    await servlet.aclose()


async def test_admin_catalog_serves_picker_rows(monkeypatch, tmp_path) -> None:
    """/admin/catalog returns standard rows + routable ids for the tunnel."""
    import httpx as _httpx

    servlet, _ = _catalog_servlet(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "omnigent.gateway.servlet.databrickscfg_host_for_profile",
        lambda profile: "https://ws.example" if profile == "oss" else None,
    )
    transport = _httpx.ASGITransport(app=servlet.build_app())
    async with _httpx.AsyncClient(transport=transport, base_url="http://sv") as client:
        resp = await client.get(
            "/admin/catalog",
            params={"profile": "oss"},
            headers={"authorization": f"Bearer {servlet.admin_token}"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert [row["id"] for row in payload["models"]] == ["gpt-5.6-sol", "glm-5-2"]
        assert payload["models"][0]["isDefault"] is True
        assert payload["routable_models"] == ["gpt-5.6-sol", "glm-5-2"]
        denied = await client.get("/admin/catalog", params={"profile": "oss"})
        assert denied.status_code == 401
    await servlet.aclose()


async def test_proxy_relays_with_minted_bearer_and_arm_translation(monkeypatch, tmp_path) -> None:
    """The relay mints a bearer, translates arm slugs, and streams the body."""
    import json as _json

    import httpx as _httpx

    servlet, _ = _catalog_servlet(monkeypatch, tmp_path)
    session = servlet.register_session("oss", "https://ws.example")
    seen: dict[str, object] = {}

    class _BodyStream(_httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok": true}'

    def upstream_handler(request: _httpx.Request) -> _httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        seen["body"] = _json.loads(request.content)
        return _httpx.Response(200, stream=_BodyStream(), headers={"x-upstream": "yes"})

    await servlet._client.aclose()
    servlet._client = _httpx.AsyncClient(transport=_httpx.MockTransport(upstream_handler))
    transport = _httpx.ASGITransport(app=servlet.build_app())
    async with _httpx.AsyncClient(transport=transport, base_url="http://sv") as client:
        resp = await client.post(
            f"/g/{session.token}/v1/responses",
            json={"model": "glm-5-2", "stream": False},
            headers={"accept-encoding": "identity"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert resp.headers["x-upstream"] == "yes"
    assert seen["url"] == "https://ws.example/ai-gateway/codex/v1/responses"
    assert seen["auth"] == "Bearer minted-oss"
    assert seen["accept_encoding"] == "identity"
    assert seen["body"]["model"] == "system.ai.glm-5-2"
    await servlet.aclose()


async def test_token_minter_caches_and_maps_failures(monkeypatch) -> None:
    """The minter caches per profile, honors the env override, and fails loud."""
    from omnigent.gateway.auth import TokenMinter

    calls: list[str] = []

    async def fake_mint(self, profile):
        calls.append(profile)
        return f"tok-{profile}-{len(calls)}", 900.0

    monkeypatch.setattr(TokenMinter, "_mint", fake_mint)
    minter = TokenMinter()
    assert await minter.bearer("oss") == "tok-oss-1"
    assert await minter.bearer("oss") == "tok-oss-1"
    assert calls == ["oss"]
    monkeypatch.setenv("DATABRICKS_BEARER", "static-tok")
    assert await minter.bearer("anything") == "static-tok"
    monkeypatch.delenv("DATABRICKS_BEARER")

    async def failing_mint(self, profile):
        raise RuntimeError("dead auth")

    monkeypatch.setattr(TokenMinter, "_mint", failing_mint)
    fresh = TokenMinter()
    with pytest.raises(RuntimeError, match="dead auth"):
        await fresh.bearer("oss")


async def test_token_minter_clamps_cache_to_token_lifetime(monkeypatch) -> None:
    """A near-expiry CLI token is not cached past its death.

    ``databricks auth token`` returns the CLI's *cached* OAuth token, which
    can be minutes from expiry; a flat cadence served dead bearers for the
    rest of the window (upstream 401 "Invalid Token"). A zero cache TTL from
    the expiry clamp must re-mint on every request.
    """
    from omnigent.gateway.auth import TokenMinter, _cache_ttl_for

    assert _cache_ttl_for({"expires_in": 3600}) == 900.0
    assert _cache_ttl_for({"expires_in": 120}) == 60.0
    assert _cache_ttl_for({"expires_in": 45}) == 0.0
    assert _cache_ttl_for({"expiry": "2020-01-01T00:00:00.123456789Z"}) == 0.0
    assert _cache_ttl_for({}) == 900.0

    mints: list[str] = []

    async def short_lived_mint(self, profile):
        mints.append(profile)
        return f"tok-{len(mints)}", 0.0

    monkeypatch.setattr(TokenMinter, "_mint", short_lived_mint)
    minter = TokenMinter()
    assert await minter.bearer("oss") == "tok-1"
    assert await minter.bearer("oss") == "tok-2"
    assert mints == ["oss", "oss"]


async def test_proxy_invalidates_bearer_on_upstream_401(monkeypatch, tmp_path) -> None:
    """An upstream auth rejection drops the cached bearer immediately.

    The cached token is provably dead no matter what its clock says, so the
    very next request must carry a freshly minted bearer instead of failing
    for the rest of the cache window.
    """
    import httpx as _httpx

    from omnigent.gateway.auth import TokenMinter
    from omnigent.gateway.servlet import GatewayServlet

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    mints: list[int] = []

    async def counting_mint(self, profile):
        mints.append(len(mints) + 1)
        return f"minted-{len(mints)}", 900.0

    monkeypatch.setattr(TokenMinter, "_mint", counting_mint)
    servlet = GatewayServlet()
    session = servlet.register_session("oss", "https://ws.example")
    seen_bearers: list[str] = []

    class _BodyStream(_httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok": true}'

    def upstream_handler(request: _httpx.Request) -> _httpx.Response:
        seen_bearers.append(request.headers.get("authorization", ""))
        status = 401 if len(seen_bearers) == 1 else 200
        return _httpx.Response(status, stream=_BodyStream())

    await servlet._client.aclose()
    servlet._client = _httpx.AsyncClient(transport=_httpx.MockTransport(upstream_handler))
    transport = _httpx.ASGITransport(app=servlet.build_app())
    async with _httpx.AsyncClient(transport=transport, base_url="http://sv") as client:
        first = await client.post(f"/g/{session.token}/v1/responses", json={"x": 1})
        assert first.status_code == 401
        second = await client.post(f"/g/{session.token}/v1/responses", json={"x": 2})
        assert second.status_code == 200
    assert seen_bearers == ["Bearer minted-1", "Bearer minted-2"]
    await servlet.aclose()


async def test_start_gateway_servlet_lifecycle(monkeypatch, tmp_path) -> None:
    """Start binds loopback, publishes state, serves health, and stops clean."""
    import httpx as _httpx

    from omnigent.gateway.servlet import start_gateway_servlet

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    handle = await start_gateway_servlet(None, port=0)
    try:
        state = read_servlet_state()
        assert state is not None and state.url == handle.url
        async with _httpx.AsyncClient() as client:
            health = await client.get(f"{handle.url}/healthz")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"
    finally:
        await handle.stop()
    assert read_servlet_state(allow_stale=True) is None
