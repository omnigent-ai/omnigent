"""UI journey: a session on a named, non-default provider must be priced at
the NAMED provider's custom rates, not the harness DEFAULT provider's.

Custom pricing resolves rates through
``omnigent.llms.context_window.fetch_model_pricing_with_provider``, which the
relay accounting path (``_accumulate_session_usage``) feeds via
``default_provider_for_harness()``. That lookup returns the DEFAULT provider
for the harness family — never the provider the session was actually launched
with (a named provider selected via ``executor.auth: {type: provider,
name: ...}``). So when two providers serve the same family (openai) at
different custom rates and a session is bound to the non-default named one,
its turns are priced at the DEFAULT provider's rate, and the wrong cost is
what the user sees in the web SPA (the agent-info popover's "Session cost").

Journey (real web SPA, live server + runner, openai-agents harness against
the mock LLM):

1. configure ``~/.omnigent/config.yaml`` with two ``openai``-family providers
   at different custom per-million rates: ``cheap-default`` (the family
   default) and ``expensive-named`` (not default; its ``base_url`` is the
   mock LLM — the default's is unreachable on purpose, so a completed turn
   PROVES the session is actually served through the named provider)
2. create an agent bound to the named provider via
   ``executor.auth: {type: provider, name: expensive-named}`` and start a
   session with it
3. send a message; the turn completes against the mock (usage: 10 input /
   5 output tokens, no harness-reported cost, so the server estimates cost
   from configured pricing)
4. open the agent-info popover
5. observable failure: "Session cost" shows $2.00 — the CHEAP DEFAULT
   provider's rate — instead of $20.00, the rate of the expensive named
   provider that actually served the session

Regression guard: the final assertions FAIL on the current build (cost is
the default provider's $2.00) and pass once pricing threads the actual
provider identity from launch/session state ($20.00).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# A model name unique to this test so the mock queue keyed on it can't be
# drained by any other session's calls (the mock routes by request model).
_MODEL = "mock-pricing-model"

# Two openai-family providers at DIFFERENT custom per-million rates. The mock
# LLM reports a fixed 10 input / 5 output tokens per text turn, so the rates
# are chosen to land on legible dollar values in the SPA's cost display.
_CHEAP_INPUT_PER_M = 100_000.0  # $0.10 / token
_CHEAP_OUTPUT_PER_M = 200_000.0  # $0.20 / token
_NAMED_INPUT_PER_M = 1_000_000.0  # $1.00 / token
_NAMED_OUTPUT_PER_M = 2_000_000.0  # $2.00 / token

# The mock /v1/responses wire reports usage {input: 10, output: max(5, words)}
# for a text response; the scripted reply below is one word, so 5.
_INPUT_TOKENS = 10
_OUTPUT_TOKENS = 5

# What the buggy default-provider lookup produces (the wrong, cheaper cost).
_DEFAULT_PROVIDER_COST = (
    _INPUT_TOKENS * _CHEAP_INPUT_PER_M / 1_000_000
    + _OUTPUT_TOKENS * _CHEAP_OUTPUT_PER_M / 1_000_000
)  # $2.00
# What the session SHOULD cost: the named provider's rate.
_NAMED_PROVIDER_COST = (
    _INPUT_TOKENS * _NAMED_INPUT_PER_M / 1_000_000
    + _OUTPUT_TOKENS * _NAMED_OUTPUT_PER_M / 1_000_000
)  # $20.00


def _format_session_cost(cost: float) -> str:
    """Mirror the SPA's ``formatSessionCostUsd`` (web/src/lib/formatCost.ts)."""
    if 0 < cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"


def _omnigent_config_path() -> Path:
    """The global provider-config path the server/runner read via load_config()."""
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".omnigent"
    return base / "config.yaml"


@pytest.fixture
def two_rate_provider_config(mock_llm_server_url: str) -> Iterator[None]:
    """Two same-family providers with different custom pricing, in place for
    the test's full lifetime (launch resolves the named provider; every
    accounting call re-reads the config). Restores the original on exit.

    ``cheap-default`` is the openai-family default; its base_url is
    deliberately unreachable so a completed turn proves the session was NOT
    served through it. ``expensive-named`` points at the mock LLM.
    """
    path = _omnigent_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text() if path.exists() else None
    path.write_text(
        textwrap.dedent(f"""\
            providers:
              cheap-default:
                kind: key
                default: [openai]
                openai:
                  # Unreachable on purpose: if the launch (wrongly) routed the
                  # session through the DEFAULT provider, the turn could never
                  # complete — so a completed turn pins the session to the
                  # named provider, and any default-rate cost to the pricing
                  # lookup alone.
                  base_url: "http://127.0.0.1:9/v1"
                  api_key: "cheap-key"
                  wire_api: responses
                  pricing:
                    input_per_million: {_CHEAP_INPUT_PER_M}
                    output_per_million: {_CHEAP_OUTPUT_PER_M}
              expensive-named:
                kind: key
                openai:
                  base_url: "{mock_llm_server_url}/v1"
                  api_key: "expensive-key"
                  wire_api: responses
                  pricing:
                    input_per_million: {_NAMED_INPUT_PER_M}
                    output_per_million: {_NAMED_OUTPUT_PER_M}
            """)
    )
    try:
        yield
    finally:
        if original is not None:
            path.write_text(original)
        else:
            path.unlink(missing_ok=True)


def _build_named_provider_bundle(name: str) -> bytes:
    """Build a one-file agent bundle bound to the ``expensive-named`` provider.

    Uses the omnigent shorthand YAML (non-``config.yaml`` arcname routes it
    through the compat translator, like the packaged hello_world agent).
    ``context_window`` gives the SPA a denominator for the unknown mock model.

    :param name: Agent name (unique per test run).
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "openai-agents",
            "model": _MODEL,
            "context_window": 200000,
            "auth": {"type": "provider", "name": "expensive-named"},
        },
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _create_named_provider_session(base_url: str, runner_id: str) -> str:
    """Create a runner-bound session for a fresh named-provider agent.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :returns: The new session id.
    """
    name = f"named-pricing-{uuid.uuid4().hex[:8]}"
    bundle = _build_named_provider_bundle(name)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _persisted_session_usage(session_id: str) -> dict:
    """Read the persisted ``session_usage`` for cross-checking the UI value."""
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    database_uri = str(_server_state.get("database_uri") or "")
    if not database_uri:
        return {}
    conv = SqlAlchemyConversationStore(database_uri).get_conversation(session_id)
    return dict(conv.session_usage) if conv and conv.session_usage else {}


@pytest.mark.timeout(600)
def test_named_provider_session_cost_uses_named_rate_not_default(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    two_rate_provider_config: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A session launched on a named, non-default provider must show the
    NAMED provider's cost in the agent-info popover — not the default's.

    On the current build the accounting path resolves pricing via
    ``default_provider_for_harness()``, so the popover shows the cheap
    default provider's $2.00 instead of the named provider's $20.00 and the
    final assertions fail — that failure is the reproduction.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])

        token = f"pricing-{uuid.uuid4().hex[:6]}"
        # Route by the unique token (and model key) so this test's queue can't
        # be drained by another session's calls; a couple of copies cover any
        # incidental extra call within the turn.
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "ack"}] * 4,
            key=_MODEL,
            match=token,
        )

        session_id = _create_named_provider_session(live_server, runner_id)
        try:
            page.goto(f"{live_server}/c/{session_id}")

            composer = page.get_by_placeholder(_COMPOSER)
            expect(composer).to_be_visible(timeout=30_000)
            composer.fill(f"Say ack. {token}")
            page.get_by_role("button", name="Send", exact=True).click()

            # Drive the turn to completion: the assistant bubble renders and
            # the working indicator clears. Completion also proves the session
            # was served through the NAMED provider (the default's base_url is
            # unreachable), pinning any default-rate cost to the pricing
            # lookup rather than request routing.
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=120_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=120_000)

            # Open the agent-info popover: the session-cost line is the
            # user-facing readout of the accounted cost.
            page.get_by_test_id("agent-info-trigger").click()
            cost_el = page.get_by_test_id("agent-info-session-cost")
            expect(cost_el).to_be_visible(timeout=15_000)
            # Brief linger so the popover (with the accounted cost) is legible
            # in recorded runs before the assertions below end the page.
            page.wait_for_timeout(1500)
            cost_text = (cost_el.inner_text() or "").strip()

            wrong = _format_session_cost(_DEFAULT_PROVIDER_COST)
            expected = _format_session_cost(_NAMED_PROVIDER_COST)

            # The bug: cost renders at the default provider's rate. Guard
            # against it explicitly so a regression is legible, then assert
            # the correct contract.
            assert cost_text != wrong, (
                f"Session cost shows {cost_text} — the DEFAULT provider's rate "
                f"(cheap-default: {_CHEAP_INPUT_PER_M}/{_CHEAP_OUTPUT_PER_M} per M) — "
                f"instead of the named provider the session was actually launched "
                f"with (expensive-named: {_NAMED_INPUT_PER_M}/{_NAMED_OUTPUT_PER_M} "
                f"per M, expected {expected}). Wrong provider used for pricing."
            )
            assert cost_text == expected, (
                f"expected the named provider's cost {expected}, got {cost_text} "
                f"(persisted usage: {_persisted_session_usage(session_id)})"
            )

            # Cross-check the persisted accounting the UI renders from.
            usage = _persisted_session_usage(session_id)
            persisted = usage.get("total_cost_usd")
            assert persisted == pytest.approx(_NAMED_PROVIDER_COST), (
                f"persisted total_cost_usd={persisted} is not the named "
                f"provider's cost {_NAMED_PROVIDER_COST}; usage={usage}"
            )
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
