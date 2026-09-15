"""E2E: "Require Approval for File & Shell Operations" must gate MultiEdit/NotebookEdit.

The ``ask_on_os_tools`` builtin (UI name "Require Approval for File & Shell
Operations") is the primary human-in-the-loop guard for Claude Code native
tools: every file-mutating ``PreToolUse`` hook evaluation must park an
approval card in the SPA before the tool runs. Claude Code's multi-hunk edit
tool is ``MultiEdit`` (its preferred tool for the common "make two edits to
one file" case) and its notebook editor is ``NotebookEdit`` — both mutate
files, so both must ASK exactly like ``Edit`` does.

Journey (per sub-symptom): attach the builtin approval policy to a session →
Claude Code's ``PreToolUse`` hook posts the tool call to
``POST /v1/sessions/{id}/policies/evaluate`` (driven here as a synthetic hook
POST, the same fast pattern as ``test_persistent_approval.py`` — no native CLI
required) → the SPA must show a pending approval card → the user approves →
the parked evaluate returns ALLOW.

On the buggy build the ``Edit`` control leg works (card renders, approve
resolves it), but the ``MultiEdit`` / ``NotebookEdit`` legs fail: the evaluate
endpoint returns ``POLICY_ACTION_ALLOW`` immediately, no card ever appears,
and the file would be written with no approval. Root cause:
``_NATIVE_OS_TOOLS`` in ``omnigent/policies/builtins/safety.py`` lists only
``{Bash, Read, Write, Edit, Glob, Grep}``.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_CARD_TIMEOUT_MS = 15_000

# What Claude Code sends for each tool, per its PreToolUse hook contract.
# NotebookEdit addresses the target via ``notebook_path``, not ``file_path``.
_TOOL_ARGS: dict[str, dict] = {
    "Edit": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
    "MultiEdit": {
        "file_path": "a.py",
        "edits": [
            {"old_string": "x", "new_string": "y"},
            {"old_string": "p", "new_string": "q"},
        ],
    },
    "NotebookEdit": {"notebook_path": "a.ipynb", "new_source": "print(1)"},
}


def _attach_approval_policy(base_url: str, session_id: str) -> None:
    """Attach the builtin "Require Approval for File & Shell Operations" policy.

    Same registered handler the Policies UI attaches; the registry allowlist
    accepts it without any factory params.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/policies",
        json={
            "name": "require-approval-file-shell",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
        timeout=15.0,
    )
    assert resp.status_code == 200, f"policy attach failed: {resp.status_code} {resp.text}"


def _evaluate_tool_call(
    base_url: str,
    session_id: str,
    tool_name: str,
    holder: dict,
) -> None:
    """POST the PreToolUse-shaped evaluate request; park until resolved.

    Mirrors the body Claude Code's ``evaluate-policy`` hook sends
    (``PHASE_TOOL_CALL`` + tool name/arguments). Stores the response JSON
    (or the exception) in *holder* for the test to assert on.
    """
    try:
        resp = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/policies/evaluate",
            json={
                "event": {
                    "type": "PHASE_TOOL_CALL",
                    "target": "",
                    "data": {
                        "name": tool_name,
                        "arguments": _TOOL_ARGS[tool_name],
                    },
                    "context": {"harness": "claude-native"},
                },
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        holder["response"] = resp.json()
    except Exception as exc:  # surfaced by the test
        holder["error"] = exc


def _drive_gated_tool(page: Page, base_url: str, session_id: str, tool_name: str) -> dict:
    """Fire *tool_name* through the policy gate and approve its card in the UI.

    Returns the evaluate endpoint's response JSON. Fails (card timeout) when
    the policy silently ALLOWs instead of parking an approval.
    """
    holder: dict = {}
    thread = threading.Thread(
        target=_evaluate_tool_call,
        args=(base_url, session_id, tool_name, holder),
        daemon=True,
    )
    thread.start()

    # The file-mutating call must park a pending approval card. On the buggy
    # build MultiEdit/NotebookEdit return ALLOW instantly and no card renders,
    # so this expectation is the failing assertion.
    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    expect(card).to_contain_text(tool_name)

    card.get_by_role("button", name="Approve", exact=True).click()

    thread.join(timeout=60)
    assert not thread.is_alive(), f"evaluate call for {tool_name} never resolved"
    if "error" in holder:
        raise AssertionError(f"evaluate call for {tool_name} failed: {holder['error']}")
    return holder["response"]


@pytest.mark.timeout(120)
@pytest.mark.parametrize("tool_name", ["MultiEdit", "NotebookEdit"])
def test_native_file_edit_tools_require_approval_card(
    page: Page,
    seeded_session: tuple[str, str],
    tool_name: str,
) -> None:
    """A Claude Code MultiEdit/NotebookEdit call must surface an approval card.

    Control leg first: ``Edit`` parks a card and approve resolves it — proving
    the policy, the evaluate endpoint, and the card wiring all work in this
    session. Then the same journey with *tool_name* must behave identically;
    on the buggy build no card appears and the tool would write with no
    approval.
    """
    base_url, session_id = seeded_session
    _attach_approval_policy(base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    # Session surface is up once the composer renders (matched by its stable
    # aria-label; the placeholder text varies with runner/agent state).
    expect(page.get_by_label("Message the agent")).to_be_attached(timeout=30_000)

    # Control: Edit is gated today — card renders, approve resolves to ALLOW.
    edit_result = _drive_gated_tool(page, base_url, session_id, "Edit")
    assert edit_result.get("result") == "POLICY_ACTION_ALLOW", edit_result

    # The bug: the same journey with MultiEdit / NotebookEdit must also park
    # an approval card (both mutate files). Fails on the buggy build — the
    # evaluate returns ALLOW with no card.
    gated_result = _drive_gated_tool(page, base_url, session_id, tool_name)
    assert gated_result.get("result") == "POLICY_ACTION_ALLOW", gated_result
