"""
E2E tests for the policy system through the real workflow.

Uploads the ``e2e-policy-gate`` fixture agent (FunctionPolicy
at INPUT that DENYs messages containing a sentinel token),
posts responses with real LLM calls through the server, and
verifies:

- Clean messages pass through → real LLM response.
- Sentinel-containing messages hit the policy DENY path →
  assistant sentinel text, no LLM call.
- The DENY sentinel is persisted to conversation_items so a
  follow-up turn sees it.
- The DENY path terminates the turn in ``completed`` status
  (the agent didn't crash, it just replied with the
  sentinel).
- Agents without any guardrails block run unchanged (the
  archer agent is the regression test for this — if the
  no-op engine path broke, every non-policy agent would
  too).

Usage::

    pytest tests/e2e/test_policies_e2e.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
)

_E2E_POLICY_GATE_DIR = (
    Path(__file__).resolve().parents[1] / "_fixtures" / "agents" / "e2e-policy-gate"
)
_E2E_LABEL_GATE_DIR = (
    Path(__file__).resolve().parents[1] / "_fixtures" / "agents" / "e2e-label-gate"
)
_ASK_DEMO_DIR = Path(__file__).resolve().parents[1] / "resources" / "agents" / "ask-demo"
_E2E_PROMPT_POLICY_DIR = (
    Path(__file__).resolve().parents[1] / "_fixtures" / "agents" / "e2e-prompt-policy"
)


@pytest.fixture(scope="session")
def policy_gate_agent(
    http_client: httpx.Client,
    databricks_workspace_host: str | None,
    databricks_profile_or_none: str | None,
) -> str:
    """Upload the e2e-policy-gate fixture and return its name."""
    return upload_agent(
        http_client,
        _E2E_POLICY_GATE_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
        databricks_profile=databricks_profile_or_none,
    )


@pytest.fixture(scope="session")
def label_gate_agent(
    http_client: httpx.Client,
    databricks_workspace_host: str | None,
    databricks_profile_or_none: str | None,
) -> str:
    """Upload the e2e-label-gate fixture and return its name."""
    return upload_agent(
        http_client,
        _E2E_LABEL_GATE_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
        databricks_profile=databricks_profile_or_none,
    )


@pytest.fixture(scope="session")
def ask_demo_agent(http_client: httpx.Client) -> str:
    """Upload the ``ask-demo`` example agent — always-ASK on INPUT."""
    return upload_agent(http_client, _ASK_DEMO_DIR)


@pytest.fixture(scope="session")
def prompt_policy_agent(
    http_client: httpx.Client,
    databricks_workspace_host: str | None,
    databricks_profile_or_none: str | None,
) -> str:
    """Upload the e2e-prompt-policy fixture and return its name."""
    return upload_agent(
        http_client,
        _E2E_PROMPT_POLICY_DIR,
        rewrite_model_for_databricks=databricks_workspace_host is not None,
        databricks_profile=databricks_profile_or_none,
    )


def _extract_all_assistant_text(body: dict) -> str:
    """Concatenate assistant-message text from a response body."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") != "message":
            continue
        if item.get("role") != "assistant":
            continue
        for block in item.get("content", []):
            if isinstance(block, dict):
                text = block.get("text") or block.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def _post_user_message(client: httpx.Client, session_id: str, text: str) -> httpx.Response:
    """Post a user message event and return the raw response.

    Unlike :func:`send_user_message_to_session`, returns the response
    unparsed so a caller can inspect a synchronous INPUT-policy DENY
    (resolved inline as a verdict, with no queued ``item_id``).
    """
    return client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )


# ── Clean-path: no policy trigger ─────────────────────


def test_policy_gate_allows_clean_message(
    http_client: httpx.Client,
    policy_gate_agent: str,
    live_runner_id: str,
) -> None:
    """A normal message (no sentinel) passes through the
    policy → reaches the LLM → gets a real response. If
    this regresses, the policy is over-firing and blocking
    legitimate traffic."""
    session_id = create_runner_bound_session(
        http_client, agent_name=policy_gate_agent, runner_id=live_runner_id
    )
    rid = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Say hi in exactly three words.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid, timeout=120
    )
    # Terminal status must be completed — policy ALLOW should
    # not turn the turn into a failure.
    assert body["status"] == "completed", f"Unexpected status: {body.get('error')}"
    text = _extract_all_assistant_text(body)
    # Real LLM response — just verify something came back
    # (content varies; checking for non-empty is the right
    # granularity since we're testing policy pass-through,
    # not LLM output quality).
    assert len(text.strip()) > 0, (
        "Expected real LLM output after policy ALLOW; got empty response."
    )
    # Sentinel must NOT appear — the clean path doesn't
    # invoke the DENY branch.
    assert "[Denied by policy" not in text


# ── DENY path: sentinel-containing message ────────────


def test_policy_gate_denies_sentinel_message(
    http_client: httpx.Client,
    policy_gate_agent: str,
    live_runner_id: str,
) -> None:
    """A message containing the sentinel token hits the
    FunctionPolicy DENY. The events endpoint resolves the DENY
    synchronously — the turn is not queued and the deny reason
    is returned inline, with no LLM call. If this regresses, the
    policy system is not wired into the events path and policies
    are effectively no-ops in production."""
    session_id = create_runner_bound_session(
        http_client, agent_name=policy_gate_agent, runner_id=live_runner_id
    )
    resp = _post_user_message(
        http_client, session_id, "Please process this: BLOCK_THIS_TOKEN now."
    )
    assert resp.status_code == 202, f"unexpected status: {resp.status_code} {resp.text[:300]}"
    verdict = resp.json()
    # ``denied: true`` (no ``item_id``) proves the DENY fired
    # synchronously and the turn was never queued to the runner.
    assert verdict.get("denied") is True, f"expected synchronous DENY verdict; got {verdict}"
    # The policy's reason is carried inline — drives the UI's
    # "why was this blocked?" surface.
    assert "BLOCK_THIS_TOKEN" in verdict.get("reason", ""), (
        f"expected reason mentioning BLOCK_THIS_TOKEN; got {verdict}"
    )


# ── DENY persisted for follow-up turns ────────────────


def test_policy_gate_deny_persists_to_history(
    http_client: httpx.Client,
    policy_gate_agent: str,
    live_runner_id: str,
) -> None:
    """After a DENY, the sentinel is readable from conversation history.

    Proves the sentinel was written to conversation_items (not just surfaced
    on the stream). The INPUT DENY fires before the runner/model, so this
    regression check stays deterministic and does not need a live LLM turn.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=policy_gate_agent, runner_id=live_runner_id
    )

    # Turn 1: DENY. The events endpoint resolves INPUT-policy
    # DENY synchronously, so no runner turn is queued.
    resp1 = _post_user_message(http_client, session_id, "Trigger BLOCK_THIS_TOKEN please.")
    assert resp1.status_code == 202, f"unexpected status: {resp1.status_code} {resp1.text[:300]}"
    verdict = resp1.json()
    assert verdict.get("denied") is True, f"expected synchronous DENY verdict; got {verdict}"
    assert "BLOCK_THIS_TOKEN" in verdict.get("reason", ""), (
        f"expected reason mentioning BLOCK_THIS_TOKEN; got {verdict}"
    )

    # Fetch conversation items — the turn-1 sentinel MUST
    # be persisted so replay sees it.
    items_resp = http_client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 100},
    )
    items_resp.raise_for_status()
    items = items_resp.json().get("data", [])
    assistant_texts = [
        block.get("text") or block.get("output_text") or ""
        for item in items
        if item.get("type") == "message" and item.get("role") == "assistant"
        for block in item.get("content", [])
        if isinstance(block, dict)
    ]
    # Turn-1 sentinel is in the persisted history.
    assert any("[Denied by policy" in t for t in assistant_texts), (
        f"DENY sentinel not persisted to conversation_items. Assistant texts: {assistant_texts!r}"
    )


# ── Regression: no-guardrails agents still work ──────


# ── Multi-policy composition via labels across turns ─


def test_label_gate_taint_persists_across_turns(
    http_client: httpx.Client,
    label_gate_agent: str,
    live_runner_id: str,
) -> None:
    """Turn 1: user triggers FunctionPolicy that writes
    ``tainted: "1"``. Turn 2: clean input, but
    The condition ``tainted: "1"`` now matches →
    DENY.

    End-to-end proof that FunctionPolicy set_labels reach
    the store, persist across workflow restarts, and drive
    condition gates on the next turn — the core IFC-through-
    labels pattern. Both turns run on the same runner-bound
    session so turn 2 sees turn 1's persisted label."""
    session_id = create_runner_bound_session(
        http_client, agent_name=label_gate_agent, runner_id=live_runner_id
    )
    # Turn 1: trigger the taint. ALLOW-with-set_labels, so the message
    # is queued and the LLM runs (deny_when_tainted hasn't fired yet —
    # its condition is evaluated against the pre-turn-1 label snapshot).
    rid1 = send_user_message_to_session(
        http_client, session_id=session_id, content="BANANA_TRIGGER — say hi briefly."
    )
    body1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid1, timeout=120
    )
    assert body1["status"] == "completed", f"Turn 1 failed: {body1.get('error')}"
    text1 = _extract_all_assistant_text(body1)
    assert "[Denied by policy" not in text1
    assert len(text1.strip()) > 0

    # Turn 2: clean input on the SAME session — no trigger, but the
    # label persisted from turn 1. deny_when_tainted now matches at
    # INPUT and the events endpoint resolves the DENY synchronously
    # with an inline verdict (no queued turn).
    resp2 = _post_user_message(http_client, session_id, "A clean follow-up message.")
    assert resp2.status_code == 202, f"unexpected status: {resp2.status_code} {resp2.text[:300]}"
    verdict = resp2.json()
    # ``denied: true`` proves the persisted tainted=1 drove the DENY.
    assert verdict.get("denied") is True, (
        f"Turn 2 should DENY on tainted conversation; got {verdict}"
    )
    # Reason matches the policy declaration ("...tainted from a prior turn.").
    assert "tainted" in verdict.get("reason", "").lower(), (
        f"DENY reason should mention the taint; got {verdict}"
    )


def test_label_gate_untainted_conversation_passes(
    http_client: httpx.Client,
    label_gate_agent: str,
    live_runner_id: str,
) -> None:
    """A conversation that never triggers taint_on_banana
    should pass every turn — the condition
    ``tainted: "1"`` never matches against the default
    ``tainted: "0"`` seed."""
    session_id = create_runner_bound_session(
        http_client, agent_name=label_gate_agent, runner_id=live_runner_id
    )
    rid = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Hello. Reply briefly.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid, timeout=120
    )
    assert body["status"] == "completed", f"Clean conversation failed: {body.get('error')}"
    text = _extract_all_assistant_text(body)
    assert "[Denied by policy" not in text
    assert len(text.strip()) > 0


def test_label_gate_persisted_labels_in_store(
    http_client: httpx.Client,
    label_gate_agent: str,
    live_runner_id: str,
) -> None:
    """After the taint turn, the ``tainted`` label is
    persisted to ``conversation_labels`` — verifiable via
    a follow-up turn whose engine is rebuilt from persisted
    state.

    Not just an in-memory snapshot — the labels survive
    workflow restarts, which is what Phase 1's store API
    guarantees."""
    session_id = create_runner_bound_session(
        http_client, agent_name=label_gate_agent, runner_id=live_runner_id
    )
    # Turn 1: taint (ALLOW + set_labels, real LLM turn).
    rid1 = send_user_message_to_session(
        http_client, session_id=session_id, content="BANANA_TRIGGER, please acknowledge."
    )
    body1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid1, timeout=120
    )
    assert body1["status"] == "completed"

    # Turn 2 on the SAME session. The engine rebuilds from persisted
    # state — if the label didn't persist, the condition wouldn't
    # match and turn 2 would pass through. The synchronous DENY proves
    # tainted=1 survived to this turn.
    resp2 = _post_user_message(http_client, session_id, "ok.")
    assert resp2.status_code == 202, f"unexpected status: {resp2.status_code} {resp2.text[:300]}"
    verdict = resp2.json()
    assert verdict.get("denied") is True, f"Persisted tainted=1 should DENY turn 2; got {verdict}"


def test_no_guardrails_agent_unaffected(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
) -> None:
    """Archer has no guardrails block — the engine is a
    no-op, every INPUT ALLOWs, workflow runs normally.

    Regression test for the Phase 6 wiring: if
    `build_policy_engine` misbehaves on the no-guardrails
    path, OR `_enforce_input_policies` over-fires, EVERY
    production agent without policies would start failing.
    Detecting this at the e2e level catches bugs the unit
    tests' `noop_engine` doesn't cover (real workflow,
    real message flow, real LLM round-trip)."""
    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )
    rid = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="What is 2 + 2? Answer with one number only.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid, timeout=120
    )
    assert body["status"] == "completed", f"Archer (no guardrails) failed: {body.get('error')}"
    text = _extract_all_assistant_text(body)
    # Real LLM output — not a policy sentinel.
    assert len(text.strip()) > 0
    assert "[Denied by policy" not in text


# ── Prompt policy (Phase 9): real LLM classifier end-to-end ─
#
# These tests exercise the production path of the
# ``prompt_policy`` builtin — the real LLM gets called with
# the framework-generated envelope + author prompt, and the
# parsed JSON verdict drives the ALLOW / DENY branch.


# The verdict comes from a real LLM-backed classifier (the deny_canada
# PromptPolicy), so an occasional misclassification or a transient
# classifier-call failure (fail-closed DENY) can flip the expected
# allow/deny outcome. Bounded reruns absorb that non-determinism without
# masking a genuine wiring regression: a real break fails all 3 attempts.
@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_prompt_policy_allow_path_reaches_llm(
    http_client: httpx.Client,
    prompt_policy_agent: str,
    live_runner_id: str,
) -> None:
    """
    Non-Canadian input → classifier ALLOWs → agent LLM runs →
    assistant text comes back. Proves the real classifier
    works end-to-end through the real LLM, the policy engine
    composes ALLOW, and the full turn completes normally.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=prompt_policy_agent, runner_id=live_runner_id
    )
    rid = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="What's 2+2? Answer with the number only.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=rid, timeout=120
    )
    assert body["status"] == "completed", f"Unexpected status: {body.get('error')}"
    text = _extract_all_assistant_text(body)
    # Real LLM answered the question — "4" must appear.
    # Stronger than a non-empty check: proves the request
    # actually reached the LLM and the LLM's output
    # propagated through the ALLOW path.
    assert "4" in text, f"Expected the LLM's answer to 2+2 ('4') in the reply.\nGot: {text!r}"
    # Policy did NOT deny — the DENY sentinel must not appear.
    assert "[Denied by policy" not in text, (
        f"ALLOW path accidentally emitted a DENY sentinel: {text!r}"
    )


# Same real-classifier non-determinism as the allow-path test: the DENY can
# occasionally not fire. Bounded reruns; a real regression fails all 3.
@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_prompt_policy_deny_path_short_circuits(
    http_client: httpx.Client,
    prompt_policy_agent: str,
    live_runner_id: str,
) -> None:
    """
    Canadian-topic input → classifier DENYs → the events endpoint
    short-circuits the turn synchronously with an inline deny
    verdict; the agent LLM never produces its normal output.

    This is the canonical reason prompt policies exist: a
    topic-level content filter an author describes in prose
    rather than a Python predicate. If the real classifier
    isn't wired (or can't reach the gateway), the policy fails
    and the verdict carries an error reason rather than the
    author's ``"mentions Canada"`` — so this test is both a
    classifier-wiring proof and a gateway-routing regression
    guard.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=prompt_policy_agent, runner_id=live_runner_id
    )
    resp = _post_user_message(http_client, session_id, "What's the capital of Canada?")
    assert resp.status_code == 202, f"unexpected status: {resp.status_code} {resp.text[:300]}"
    verdict = resp.json()
    # ``denied: true`` proves the classifier ran and the DENY
    # short-circuited the turn synchronously (no queued item).
    assert verdict.get("denied") is True, f"expected classifier DENY verdict; got {verdict}"
    # The author's prompt instructs the classifier to emit exactly
    # ``"mentions Canada"`` as the reason. Casefold-compare so model
    # capitalization variance doesn't break the test. A 401/gateway
    # error reason here means the classifier didn't reach the gateway.
    assert "canada" in verdict.get("reason", "").lower(), (
        f"DENY verdict didn't carry the expected reason ('Canada'); got {verdict}"
    )
