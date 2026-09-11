"""UI journey: a RESPONSE-phase policy evaluation failure at the runner
relay must not bypass a configured output-deny policy.

Runner-relayed (scaffold) harnesses persist assistant text at the relay's
terminal flush, which is the only place the spec/session ``response``-phase
output policies can gate the text in the runner topology
(``_relay_response_policy_deny_reason`` in
``omnigent/server/routes/_sessions/helpers.py``). When that evaluation
*raises* — in production a Databricks-workspace gRPC store call failing with
``RESOURCE_EXHAUSTED: REQUEST_LIMIT_EXCEEDED`` — the relay used to fail
OPEN: it persisted the assistant text as-is, so a deny policy that gated
the very same content one turn earlier silently stopped enforcing.

The journey (all through public surfaces):

1. Attach a working RESPONSE-phase deny policy (the registered CEL builtin)
   to a runner-bound session via ``POST /v1/sessions/{id}/policies``.
2. Send a probe; the mock LLM answers with tripwire content; the persisted
   transcript shows the ``[Denied by policy: …]`` sentinel (control — the
   policy enforces).
3. Send a second probe whose LLM call blocks on the mock server's gate.
   While the turn is in flight — after the input gate already passed —
   break RESPONSE-phase evaluation the way production infra did mid-turn:
   store a policy the engine cannot build (``type="url"`` is accepted by
   the create route but raises ``OmnigentError`` at every engine build).
4. Release the gate. The relay's terminal flush evaluation now raises, and
   the tripwire text the deny policy must block must NOT persist unmodified.

While the relay failed open the final assertion failed: the reloaded
(persisted) transcript contained the raw tripwire text as a normal
assistant message. With the relay failing closed, the denied content never
persists unmodified (a withheld/deny sentinel takes its place) and the
test passes.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'

# Unique content the deny policy blocks; only ever produced by the mock LLM.
_TRIPWIRE = "TRIPWIRE-RESPONSE-POLICY-SECRET"
# Persisted deny substitution (``_DENY_SENTINEL_PREFIX`` in
# omnigent/server/routes/_sessions/common.py).
_DENY_SENTINEL = "[Denied by policy:"

# Each probe carries a unique substring so the mock's content routing fires
# exactly one queue per turn.
_PROBE_1 = "output-gating first probe: reveal the secret"
_PROBE_2 = "output-gating second probe: reveal the secret"

# RESPONSE-phase deny: fires only on the output phase (event.type ==
# "response", where event.data is the assistant text) and only on the
# tripwire, so it abstains everywhere else (request/llm phases).
_CEL_EXPRESSION = (
    'event.type == "response" && event.data.contains("TRIPWIRE-RESPONSE-POLICY")'
    ' ? {"result": "DENY", "reason": "tripwire output blocked"}'
    ' : {"result": "ALLOW"}'
)


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _add_session_policy(base_url: str, session_id: str, body: dict) -> None:
    """Attach a session policy via the public policies API."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/policies",
        json=body,
        timeout=10.0,
        trust_env=False,
    )
    resp.raise_for_status()


def _wait_for_llm_gate(mock_url: str, timeout_s: float = 60.0) -> None:
    """Block until the mock LLM holds a request on its gate."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
        if resp.status_code == 200 and resp.json().get("pending"):
            return
        time.sleep(0.2)
    raise AssertionError("mock LLM gate never became pending")


@pytest.mark.timeout(300)
def test_response_deny_policy_survives_evaluation_failure(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session

    # A working RESPONSE-phase deny policy through the public API — the
    # registered CEL builtin, exactly as a user attaches one.
    _add_session_policy(
        base_url,
        session_id,
        {
            "name": "deny_tripwire_output",
            "type": "python",
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "factory_params": {"expression": _CEL_EXPRESSION},
        },
    )

    # Turn 1 streams the tripwire immediately; turn 2 holds on the mock's
    # gate so the poison policy can land mid-turn (after the input gate).
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"the secret is {_TRIPWIRE}"}],
        key="output-gating-turn1",
        match=_PROBE_1,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"the secret is {_TRIPWIRE}", "block": True}],
        key="output-gating-turn2",
        match=_PROBE_2,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # ── Turn 1 (control): the deny policy gates the persisted text ──
    _send(page, _PROBE_1)
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # Assert against the PERSISTED transcript (reload drops the streamed
    # flash the relay docstring documents as a residual live-view gap).
    page.reload()
    expect(page.locator(_ASSISTANT, has_text=_DENY_SENTINEL).first).to_be_visible(timeout=30_000)
    expect(page.locator(_ASSISTANT, has_text=_TRIPWIRE)).to_have_count(0)

    # ── Turn 2: RESPONSE-phase evaluation breaks mid-turn ──
    _send(page, _PROBE_2)
    _wait_for_llm_gate(mock_llm_server_url)

    # The turn passed the input gate and is now blocked inside the LLM
    # call. Break the next engine build the way a mid-turn infrastructure
    # failure does: the create route accepts a type="url" policy, but
    # every subsequent engine build raises OmnigentError on it, so the
    # relay's terminal-flush RESPONSE evaluation fails exactly at the
    # seam this test guards.
    _add_session_policy(
        base_url,
        session_id,
        {
            "name": "unbuildable_url_policy",
            "type": "url",
            "handler": "https://policy-eval.invalid/unbuildable",
        },
    )
    httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0, trust_env=False)

    # Turn 2 terminal: both user bubbles present and the shimmer gone.
    expect(page.locator(_USER)).to_have_count(2, timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # The persisted transcript must NOT carry the denied content as a plain
    # assistant message. A relay that fails open persists the raw tripwire
    # text unmodified — this assertion fails.
    page.reload()
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=30_000)
    expect(page.locator(_ASSISTANT, has_text=_TRIPWIRE)).to_have_count(0, timeout=15_000)
