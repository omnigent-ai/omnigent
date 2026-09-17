"""E2E: a sub-agent must inherit its root session's approval-policy gate.

Attaching "Require Approval for File & Shell Operations" (the
``ask_on_os_tools`` builtin) to a session promises a human approval card
before ANY file/shell tool runs — including tools run by the sub-agents
that session spawns: session policies are inherited from the root
conversation (``build_policy_engine`` merges the root's session policies
into every descendant's pipeline).

Journey: attach the approval policy to a parent session → the parent's own
``Bash`` call parks an approval card (control: the gate is live) → the
parent delegates: a sub-agent session is created under it (plain
``POST /v1/sessions`` with ``parent_session_id`` — the same call the
``sys_session_create`` spawn path proxies; the child declares no policies
of its own) → the sub-agent's harness fires a ``Bash`` PreToolUse
evaluation (the exact ``POST /policies/evaluate`` wire path a native
harness takes for every tool call, driven here as a synthetic hook POST
like ``test_native_edit_tools_approval_card.py`` — no native CLI required)
→ the SPA must park a pending approval card on the child session before
the tool runs.

On the buggy build the child leg fails: ``any_policies_apply`` — the
fast-path guard in front of ``POST /policies/evaluate`` — checks the agent
spec, the sub-agent's own spec, the server/store defaults, and the
session's OWN policies, but never the ROOT session's. A child with no
policies of its own therefore short-circuits to ``POLICY_ACTION_ALLOW``
without ever reaching the engine that would have inherited the parent's
gate: no card renders and the shell command runs unattended.

The companion test proves the isolation: give the child one unrelated
no-op policy (``block_skills([])``, which cannot gate a ``Bash`` call) and
the very same journey parks the inherited card — the engine inherits fine;
only the guard in front of it forgets the root.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
# Generous: card delivery to a freshly-loaded page can lag under CI load.
_CARD_TIMEOUT_MS = 60_000

# The builtin the Policies UI attaches as "Require Approval for File &
# Shell Operations" — ASKs before any file/shell tool call.
_ASK_ON_SHELL_POLICY = {
    "name": "require-approval-file-shell",
    "type": "python",
    "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
}

# A no-op the child can carry: blocks ZERO skills, so it can never gate a
# Bash call. Its only effect is making the child's own policy list
# non-empty, which forces the evaluate fast path to build the real engine.
_NOOP_CHILD_POLICY = {
    "name": "noop-block-no-skills",
    "type": "python",
    "handler": "omnigent.policies.builtins.safety.block_skills",
    "factory_params": {"blocked": []},
}


def _attach_policy(base_url: str, session_id: str, payload: dict) -> None:
    """Attach a registered builtin policy to *session_id* via the API."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/policies",
        json=payload,
        timeout=15.0,
    )
    assert resp.status_code < 400, f"policy attach failed: {resp.status_code} {resp.text}"


def _create_subagent_session(base_url: str, parent_session_id: str) -> str:
    """Create a sub-agent session under *parent_session_id*, no own policies.

    Plain JSON ``POST /v1/sessions`` with ``parent_session_id`` reusing the
    parent's agent — the request shape the ``sys_session_create`` spawn
    path proxies (the server records the row as ``kind="sub_agent"``).
    """
    agent_resp = httpx.get(
        f"{base_url}/v1/sessions/{parent_session_id}/agent",
        timeout=10.0,
    )
    agent_resp.raise_for_status()
    agent_id = agent_resp.json()["id"]

    child_resp = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent_session_id,
            "title": "impl:delegated-task",
        },
        timeout=10.0,
    )
    child_resp.raise_for_status()
    return str(child_resp.json()["id"])


def _fire_bash_evaluate(base_url: str, session_id: str, holder: dict) -> threading.Thread:
    """POST the PreToolUse-shaped ``Bash('pwd')`` evaluate in a thread.

    The exact body a native harness's hook posts before running a shell
    command. On an ASK verdict the server parks the request until a human
    resolves the approval card; on ALLOW it returns immediately. The
    response JSON (or the exception) lands in *holder*.
    """

    def _post() -> None:
        try:
            resp = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/policies/evaluate",
                json={
                    "event": {
                        "type": "PHASE_TOOL_CALL",
                        "target": "",
                        "data": {"name": "Bash", "arguments": {"command": "pwd"}},
                        "context": {"harness": "claude-native"},
                    },
                },
                timeout=120.0,
            )
            resp.raise_for_status()
            holder["response"] = resp.json()
        except Exception as exc:  # surfaced by the caller's assertions
            holder["error"] = exc

    thread = threading.Thread(target=_post, daemon=True)
    thread.start()
    return thread


def _approve_pending_bash_card(page: Page) -> None:
    """Wait for the pending Bash approval card on the open page; approve it."""
    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    expect(card).to_contain_text("Bash")
    card.get_by_role("button", name="Approve", exact=True).click()
    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_CARD_TIMEOUT_MS)


def _settle(thread: threading.Thread, holder: dict, leg: str) -> dict:
    """Join the evaluate thread and return its response JSON."""
    thread.join(timeout=60)
    assert not thread.is_alive(), f"{leg}: policy evaluate never settled"
    if "error" in holder:
        raise AssertionError(f"{leg}: policy evaluate failed: {holder['error']}")
    return holder["response"]


@pytest.mark.timeout(240)
def test_subagent_inherits_root_session_ask_policy(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A policy-free sub-agent's shell call must park the inherited approval card.

    Control first: the parent's own Bash call parks a card, proving the
    policy is attached and live on this tree. Then the same shell call
    from a freshly spawned sub-agent with no policies of its own must park
    a card too — the root session's policies govern its children. On the
    buggy build the child leg returns ``POLICY_ACTION_ALLOW`` immediately
    and no card ever renders: the sub-agent runs ungated.
    """
    base_url, parent_id = seeded_session
    _attach_policy(base_url, parent_id, _ASK_ON_SHELL_POLICY)

    # Control: the parent's own shell call is gated by the policy.
    page.goto(f"{base_url}/c/{parent_id}")
    parent_holder: dict = {}
    parent_thread = _fire_bash_evaluate(base_url, parent_id, parent_holder)
    _approve_pending_bash_card(page)
    parent_result = _settle(parent_thread, parent_holder, "parent control leg")
    assert parent_result["result"] == "POLICY_ACTION_ALLOW", parent_result

    # The parent delegates: a sub-agent session with no policies of its own.
    child_id = _create_subagent_session(base_url, parent_id)
    try:
        page.goto(f"{base_url}/c/{child_id}")
        child_holder: dict = {}
        child_thread = _fire_bash_evaluate(base_url, child_id, child_holder)

        # The inherited gate: the child's shell call must park a card here
        # BEFORE the tool runs. This is the assertion that fails on the
        # buggy build (evaluate short-circuits to ALLOW, no card renders).
        card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
        try:
            expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
        except AssertionError:
            child_thread.join(timeout=10)
            verdict = child_holder.get("response", {}).get("result", "<never settled>")
            raise AssertionError(
                "sub-agent ran ungated: no approval card was parked on the "
                f"child session and its Bash evaluate returned {verdict!r} "
                "immediately — the root session's ask-on-shell policy was "
                "never applied to the sub-agent"
            ) from None

        expect(card).to_contain_text("Bash")
        card.get_by_role("button", name="Approve", exact=True).click()
        child_result = _settle(child_thread, child_holder, "sub-agent leg")
        assert child_result["result"] == "POLICY_ACTION_ALLOW", child_result
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


@pytest.mark.timeout(240)
def test_subagent_with_unrelated_own_policy_reaches_inherited_gate(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Isolation leg: one unrelated no-op child policy re-arms the inherited gate.

    ``block_skills([])`` cannot gate a Bash call — its only effect is
    making the child's own policy list non-empty so the evaluate fast path
    builds the real engine. The very same shell call then parks the
    PARENT's approval card on the child, proving root inheritance works
    whenever the engine actually runs: a failure in the sibling test is
    specifically the fast-path guard in front of the engine.
    """
    base_url, parent_id = seeded_session
    _attach_policy(base_url, parent_id, _ASK_ON_SHELL_POLICY)

    child_id = _create_subagent_session(base_url, parent_id)
    try:
        _attach_policy(base_url, child_id, _NOOP_CHILD_POLICY)

        page.goto(f"{base_url}/c/{child_id}")
        holder: dict = {}
        thread = _fire_bash_evaluate(base_url, child_id, holder)
        _approve_pending_bash_card(page)
        result = _settle(thread, holder, "no-op-policy child leg")
        assert result["result"] == "POLICY_ACTION_ALLOW", result
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
