"""UI journey: a gateway-served model id is priced from the gateway's reported cost.

The server prices a turn from the provider's static ``pricing:`` block or the
MLflow catalog. A LiteLLM-style gateway alias such as ``bedrock.claude-sonnet-5``
matches neither, so the session must take the per-request cost the gateway
reports (``x-litellm-response-cost``) or it accrues tokens with no USD cost.

Journey (real web SPA, live server + runner, openai-agents SDK harness pointed at
the mock OpenAI-compatible gateway):

1. register an agent pinned to ``bedrock.claude-sonnet-5`` and bind it to the runner
2. script the gateway reply with token usage and ``x-litellm-response-cost``
3. send one message and wait for the reply
4. open the agent-info popover: the Token usage breakdown lists the model's
   tokens and the Session cost row shows the gateway-reported amount, which the
   sessions API also returns as ``total_cost_usd``
"""

from __future__ import annotations

import re
import subprocess
import uuid

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.chat.test_session_usage_loading import _usage_panel
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

_GATEWAY_MODEL = "bedrock.claude-sonnet-5"
_GATEWAY_COST_USD = 0.0123
# The mock reports input 10 / output max(5, words) for a text reply.
_REPLY = "Gateway turn answered in brief."
_INPUT_TOKENS = 10
_OUTPUT_TOKENS = 5

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: You are a terse assistant. Reply in one short sentence.
executor:
  model: {model}
  config:
    harness: openai-agents
"""


def _create_gateway_session(base_url: str, runner_id: str, model: str) -> str:
    """Register a runner-bound openai-agents session pinned to *model*.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :param model: Wire model id the gateway serves.
    :returns: The new session id.
    """
    name = f"gateway-cost-{uuid.uuid4().hex[:8]}"
    bundle = bundle_files({"config.yaml": _AGENT_YAML.format(name=name, model=model).encode()})
    # A preset title keeps background title inference from spending the scripted reply.
    create = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        bundle,
        metadata={"title": f"Gateway cost {model}"},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def _complete_turn(page: Page, base_url: str, session_id: str, token: str) -> None:
    """Open the session, send one message carrying *token*, and wait for the reply."""
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"{token} please answer in one sentence")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).filter(has_text=_REPLY).first).to_be_visible(timeout=90_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)


def _expect_model_tokens(panel: Locator, model: str) -> None:
    """Expand the popover's Token usage breakdown and check *model*'s token rows."""
    breakdown = panel.get_by_test_id("agent-info-usage-by-model")
    expect(breakdown).to_be_visible(timeout=30_000)
    if breakdown.get_attribute("open") is None:
        breakdown.locator("summary").click()
    group = panel.get_by_test_id(f"agent-info-model-{model}")
    expect(group).to_be_visible()
    expect(group).to_contain_text(re.compile(rf"Input\s*{_INPUT_TOKENS}(?!\d)"))
    expect(group).to_contain_text(re.compile(rf"Output\s*{_OUTPUT_TOKENS}(?!\d)"))


def _session_usage(base_url: str, session_id: str) -> dict[str, object]:
    """Read the session's cost and per-model usage through the real sessions API."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}",
        params={"include_usage": "true", "include_items": "false"},
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()
    return {"total_cost_usd": body["total_cost_usd"], "usage_by_model": body["usage_by_model"]}


def _run_gateway_turn(
    request: pytest.FixtureRequest,
    base_url: str,
    mock_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    *,
    model: str,
    response_headers: dict[str, str],
) -> tuple[Page, str, subprocess.Popen[bytes] | None]:
    """Set up the gateway session and scripted reply, then drive one chat turn.

    The recorded page is created only after the non-browser setup so the clip
    starts at the user's first navigation.
    """
    respawned = _ensure_runner_online(base_url, tmp_path_factory)
    token = f"gateway-cost-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_url, [{"text": _REPLY, "response_headers": response_headers}], match=token
    )
    session_id = _create_gateway_session(base_url, str(_server_state["runner_id"]), model)
    page: Page = request.getfixturevalue("page")
    _complete_turn(page, base_url, session_id, token)
    return page, session_id, respawned


def test_gateway_reported_cost_prices_non_catalog_model(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A turn on ``bedrock.claude-sonnet-5`` shows the gateway's reported cost.

    The Token usage breakdown lists the model's tokens regardless; the Session
    cost row and ``total_cost_usd`` must come from ``x-litellm-response-cost``,
    since the id matches no catalog entry and the provider has no ``pricing:``.
    """
    page, session_id, respawned = _run_gateway_turn(
        request,
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        model=_GATEWAY_MODEL,
        response_headers={"x-litellm-response-cost": str(_GATEWAY_COST_USD)},
    )
    try:
        panel = _usage_panel(page)
        _expect_model_tokens(panel, _GATEWAY_MODEL)
        expect(panel.get_by_test_id("agent-info-session-cost")).to_have_text(
            "$0.01", timeout=10_000
        )
        usage = _session_usage(live_server, session_id)
        assert usage["total_cost_usd"] == pytest.approx(_GATEWAY_COST_USD), usage
    finally:
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)
