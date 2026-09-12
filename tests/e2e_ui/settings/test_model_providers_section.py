"""UI journey: the Settings → Models & providers section (``/settings/models``).

Covers the per-host provider config control plane from the web panel: the
host picker resolves the first online host, the providers table renders the
redacted wire entries (name, kind badge, base URL, default flag), the
agent-specs list renders each agent's current provider/model pin, the
add-provider dialog writes through ``PUT /v1/hosts/{id}/providers/{name}``,
and the per-agent pin dialog writes through
``PUT /v1/hosts/{id}/agent-specs/{name}/pin``.

The host-facing REST surface is injected at the network layer with Playwright
``page.route`` (the same approach as the worktree-source tests in
``sessions/``): the SPA talks to exactly the endpoints the real panel uses,
but no host daemon or real provider credential is needed. LLM-free: no agent
turn is dispatched.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect

_HOST_ID = "host_e2e_models"
_HOSTS_BODY = {
    "hosts": [
        {
            "host_id": _HOST_ID,
            "name": "e2e-laptop",
            "owner": "e2e",
            "status": "online",
            "configured_harnesses": {},
        }
    ]
}

_OPENROUTER = {
    "name": "openrouter",
    "kind": "gateway",
    "default": True,
    "openai": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_set": True,
        "wire_api": "chat",
        "models": {"default": "gpt-x", "alt": "gpt-y"},
    },
}

_AGENTS_BODY = {
    "agents": [
        {
            "name": "my-agent",
            "harness": "pi",
            "model": None,
            "auth": {"type": "provider", "name": "openrouter"},
            "spec_version": 1,
            "path": "/home/e2e/.omnigent/agents/my-agent/config.yaml",
        }
    ]
}


def _json(route: Route, body: dict) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _route_config(page: Page, providers: list) -> None:
    """Serve the hosts/providers/agent-specs read surface from ``providers``."""
    page.route("**/v1/hosts", lambda route: _json(route, _HOSTS_BODY))
    page.route(
        f"**/v1/hosts/{_HOST_ID}/providers",
        lambda route: (
            _json(route, {"providers": providers})
            if route.request.method == "GET"
            else route.fallback()
        ),
    )
    page.route(
        f"**/v1/hosts/{_HOST_ID}/agent-specs",
        lambda route: _json(route, _AGENTS_BODY),
    )


def _goto_models_section(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/settings/general")
    page.get_by_role("link", name="Models & providers").click()
    expect(page).to_have_url(f"{base_url}/settings/models")


def test_models_section_renders_provider_and_agent_rows(page: Page, live_server: str) -> None:
    """The section lists the host's providers and agent pins for the online host."""
    _route_config(page, [_OPENROUTER])
    _goto_models_section(page, live_server)

    # Host picker defaulted to the (only) online host.
    expect(page.get_by_text("e2e-laptop")).to_be_visible()

    # Providers table: redacted entry rendering — name, kind badge, base URL,
    # default flag. api_key_set must never surface as a key value.
    expect(page.get_by_text("openrouter", exact=True)).to_be_visible()
    expect(page.get_by_text("gateway", exact=True)).to_be_visible()
    expect(page.get_by_text("https://openrouter.ai/api/v1")).to_be_visible()

    # Agent specs: name + harness badge + the provider pin echo.
    expect(page.get_by_text("my-agent")).to_be_visible()
    expect(page.get_by_text("provider: openrouter")).to_be_visible()


def test_add_provider_dialog_writes_through_the_api(page: Page, live_server: str) -> None:
    """The add dialog builds an entry and PUTs it to the host's provider route."""
    providers: list = []
    _route_config(page, providers)
    captured: dict = {}

    def handle_upsert(route: Route) -> None:
        captured["url"] = route.request.url
        captured["body"] = route.request.post_data_json
        providers.append({**captured["body"]["entry"], "name": "e2e-gw"})
        _json(route, {})

    page.route(f"**/v1/hosts/{_HOST_ID}/providers/e2e-gw", handle_upsert)
    _goto_models_section(page, live_server)

    page.get_by_text("Add provider").click()
    page.get_by_label("Name", exact=True).fill("e2e-gw")
    page.get_by_label("Base URL / endpoint").fill("http://127.0.0.1:9/v1")
    page.get_by_label("Variable name").fill("E2E_TEST_KEY")
    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.get_by_text("e2e-gw", exact=True)).to_be_visible()
    assert captured["url"].endswith(f"/v1/hosts/{_HOST_ID}/providers/e2e-gw")
    entry = captured["body"]["entry"]
    assert entry["kind"] == "gateway"
    assert entry["openai"]["base_url"] == "http://127.0.0.1:9/v1"
    assert entry["openai"]["api_key_ref"] == "env:E2E_TEST_KEY"
    # the raw key material is never part of the wire body — only the ref
    assert "api_key" not in entry["openai"]


def test_pin_dialog_sets_agent_pin(page: Page, live_server: str) -> None:
    """The pin dialog PUTs the provider/model pin for the agent spec."""
    _route_config(page, [_OPENROUTER])
    captured: dict = {}

    def handle_pin(route: Route) -> None:
        captured["url"] = route.request.url
        captured["body"] = route.request.post_data_json
        _json(route, {})

    page.route(f"**/v1/hosts/{_HOST_ID}/agent-specs/my-agent/pin", handle_pin)
    _goto_models_section(page, live_server)

    page.get_by_text("Pin", exact=True).click()
    # Pick the provider from the radix select, type the model, save.
    page.get_by_role("combobox", name="Pin provider").click()
    page.get_by_role("option", name="openrouter").click()
    page.get_by_label("Model").fill("gpt-x")
    page.get_by_role("button", name="Save pin").click()

    assert captured["url"].endswith(
        f"/v1/hosts/{_HOST_ID}/agent-specs/my-agent/pin"
    )
    assert captured["body"] == {"provider": "openrouter", "model": "gpt-x"}
