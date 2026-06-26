"""UI journey: the header "Agent tools & policies" popover (AgentInfo).

The chat header carries an info button (``components/AgentInfo.tsx`` →
``AgentInfoButton``) that opens a popover summarizing the bound agent: its
tools / MCP servers, session cost, and the **session policies** the user can
add and remove on the fly. The policy surface is the interactive part —
``GET /v1/policy-registry`` lists attachable handlers, ``POST`` /
``DELETE /v1/sessions/<id>/policies`` mutate the session — so this suite drives
the full add→delete loop and pins each step to the REST state behind it.

No LLM turn is involved (the popover is rail/REST state, not a function of any
model output), so this stays a fast, deterministic check. It is the companion
to the approval-card suite: that one proves a *spec-declared* policy gates a
tool call; this one proves a user can attach and detach a policy through the UI.

The load-bearing assertions are pinned to ``GET /v1/sessions/<id>/policies``:
the added handler appears with ``source == "session"`` after the dialog submits,
and is gone after the pill's Remove — proof the popover mutates real
server-side session policy, not just optimistic local state.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_AGENT_INFO_TRIGGER = '[data-testid="agent-info-trigger"]'
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ECHO_MCP_SERVER = _REPO_ROOT / "tests" / "tools" / "fixtures" / "echo_stdio_mcp_server.py"


def _callable_registry_policy(base_url: str) -> dict:
    """Return a no-parameter (``callable``) policy from ``GET /v1/policy-registry``.

    A ``callable`` handler takes no factory params, so the Add-Policy dialog
    needs only a selection + submit — keeping the UI flow deterministic. Skips
    the test if the server exposes no such handler (a registry-shape change),
    rather than guessing at factory params.

    :param base_url: Spawned server base URL.
    :returns: The chosen registry entry dict (``name``, ``handler``, ``kind``).
    """
    resp = httpx.get(f"{base_url}/v1/policy-registry", timeout=10.0)
    resp.raise_for_status()
    for entry in resp.json()["data"]:
        if entry.get("kind") == "callable":
            return entry
    # ``raise`` (vs a bare ``pytest.skip(...)`` call) makes this branch
    # explicitly non-returning, so the function has no implicit ``-> None``
    # fall-through to contradict its ``-> dict`` annotation.
    raise pytest.skip.Exception(
        "no parameter-free (callable) policy in the registry to exercise the dialog"
    )


def _registry_policy_by_handler(base_url: str, handler: str) -> dict:
    """Return the registry entry for *handler*, or skip if it is unavailable."""
    resp = httpx.get(f"{base_url}/v1/policy-registry", timeout=10.0)
    resp.raise_for_status()
    for entry in resp.json()["data"]:
        if entry.get("handler") == handler:
            return entry
    raise pytest.skip.Exception(f"policy registry entry not found for {handler}")


def _session_policies(base_url: str, session_id: str) -> list[dict]:
    """Return the session's policy rows (owner view) from the CRUD API."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/policies", timeout=10.0)
    resp.raise_for_status()
    return resp.json()["data"]


def _user_policy_names(base_url: str, session_id: str) -> set[str]:
    """Names of user-attached (``source == "session"``) policies on the session."""
    return {p["name"] for p in _session_policies(base_url, session_id) if p["source"] == "session"}


def _user_policy_by_name(base_url: str, session_id: str, name: str) -> dict | None:
    """Return a user-attached policy row by name, if present."""
    for policy in _session_policies(base_url, session_id):
        if policy["source"] == "session" and policy["name"] == name:
            return policy
    return None


def _agent_mcp_names(base_url: str, session_id: str) -> set[str]:
    """Names of MCP servers on the session's bound agent."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    resp.raise_for_status()
    return {server["name"] for server in resp.json()["mcp_servers"]}


def _post_mcp_rpc(
    base_url: str,
    session_id: str,
    method: str,
    params: dict | None = None,
) -> dict:
    """POST one JSON-RPC request to the session MCP proxy and return result."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        timeout=45.0,
    )
    resp.raise_for_status()
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


def _runner_mcp_tool_names(base_url: str, session_id: str) -> set[str]:
    """Namespaced MCP tool names visible through the runner MCP proxy."""
    result = _post_mcp_rpc(base_url, session_id, "tools/list")
    return {tool["name"] for tool in result["tools"]}


def _open_popover(page: Page) -> None:
    """Open the agent-info popover from a known-closed state, idempotently.

    Adding a policy opens a modal dialog and removing one opens a nested
    popover; both dismiss the outer popover on the interaction-outside that
    follows. Pressing Escape first guarantees we re-open from a closed state
    rather than toggling an already-open popover shut.

    :param page: Playwright page on a ``/c/<id>`` route.
    """
    page.keyboard.press("Escape")
    trigger = page.locator(_AGENT_INFO_TRIGGER)
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()
    # "Policies" section label proves the popover content mounted.
    expect(page.get_by_text("Policies", exact=True)).to_be_visible(timeout=15_000)


def test_agent_info_policy_add_and_remove(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Popover → add a policy → pill + REST reflect it → Remove → both clear."""
    base_url, session_id = seeded_session
    entry = _callable_registry_policy(base_url)
    registry_name = entry["name"]
    # The dialog stores the policy under a slugified name (see AgentInfo's
    # AddPolicyDialog.handleAdd): lowercased, whitespace runs → underscores.
    stored_name = re.sub(r"\s+", "_", registry_name.lower())

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    assert not _user_policy_names(base_url, session_id), "session started with policies already"

    # Open the popover: the Policies section starts empty.
    _open_popover(page)
    expect(page.get_by_text("No policies added")).to_be_visible()

    # Add the registry policy through the dialog.
    page.get_by_role("button", name="Add policy").click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text("Add Policy"))
    expect(dialog).to_be_visible(timeout=15_000)
    dialog.get_by_role("button").filter(has_text=registry_name).first.click()
    dialog.get_by_role("button", name="Add", exact=True).click()
    expect(dialog).to_be_hidden(timeout=15_000)

    # The server recorded the attach as a session-source policy.
    _wait_for(lambda: _user_policy_names(base_url, session_id) == {stored_name})

    # Re-open the popover: the policy now shows as a pill.
    _open_popover(page)
    pill = page.get_by_role("button", name=stored_name, exact=True)
    expect(pill).to_be_visible(timeout=15_000)

    # Open the pill's popover and remove the policy.
    pill.click()
    page.get_by_role("button", name="Remove").click()
    _wait_for(lambda: not _user_policy_names(base_url, session_id))

    # Re-open the popover: the section is empty again.
    _open_popover(page)
    expect(page.get_by_text("No policies added")).to_be_visible(timeout=15_000)


def test_agent_info_policy_integer_params_validate_and_submit(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Factory integer params reject decimals and submit browser number notation."""
    base_url, session_id = seeded_session
    entry = _registry_policy_by_handler(
        base_url,
        "omnigent.policies.builtins.safety.max_tool_calls_per_session",
    )
    registry_name = entry["name"]
    stored_name = re.sub(r"\s+", "_", registry_name.lower())

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    assert not _user_policy_names(base_url, session_id), "session started with policies already"

    _open_popover(page)
    page.get_by_role("button", name="Add policy").click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text("Add Policy"))
    expect(dialog).to_be_visible(timeout=15_000)
    dialog.get_by_placeholder("Filter policies...").fill(registry_name)
    dialog.get_by_role("button").filter(has_text=registry_name).first.click()

    limit_input = dialog.locator('input[type="number"]').first
    limit_input.fill("12.9")
    dialog.get_by_role("button", name="Add", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("limit must be an integer")
    assert _user_policy_by_name(base_url, session_id, stored_name) is None

    limit_input.fill("1e2")
    dialog.get_by_role("button", name="Add", exact=True).click()
    expect(dialog).to_be_hidden(timeout=15_000)

    _wait_for(lambda: _user_policy_by_name(base_url, session_id, stored_name) is not None)
    stored_policy = _user_policy_by_name(base_url, session_id, stored_name)
    assert stored_policy is not None
    assert stored_policy["factory_params"] == {"limit": 100}


def test_agent_info_mcp_server_add_and_remove(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Popover → manage MCP servers → add → REST reflects it → delete."""
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    assert not _agent_mcp_names(base_url, session_id), "session started with MCP servers"

    _open_popover(page)
    page.get_by_role("button", name="Manage MCP servers").click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text("Manage MCP Servers"))
    expect(dialog).to_be_visible(timeout=15_000)
    dialog.get_by_label("Name").fill("ui-search")
    dialog.get_by_label("URL").fill("https://example.com/sse")
    dialog.get_by_role("button", name="Save").click()

    _wait_for(lambda: _agent_mcp_names(base_url, session_id) == {"ui-search"})
    expect(dialog.get_by_role("button", name="Edit ui-search")).to_be_visible(timeout=15_000)

    dialog.get_by_role("button", name="Delete ui-search").click()
    _wait_for(lambda: not _agent_mcp_names(base_url, session_id))


def test_agent_info_mcp_dirty_warning_after_edit(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Restart warning appears in dialog and Tools section after MCP edit."""
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    _open_popover(page)
    page.get_by_role("button", name="Manage MCP servers").click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text("Manage MCP Servers"))
    expect(dialog).to_be_visible(timeout=15_000)

    # No warning before any edit.
    expect(dialog.get_by_text("Restart the session to apply your changes.")).to_be_hidden()

    # Add a server to trigger the dirty state.
    dialog.get_by_label("Name").fill("dirty-test")
    dialog.get_by_label("URL").fill("https://example.com/sse")
    dialog.get_by_role("button", name="Save").click()
    _wait_for(lambda: _agent_mcp_names(base_url, session_id) == {"dirty-test"})

    # Warning should now appear inside the dialog.
    expect(dialog.get_by_text("Restart the session to apply your changes.")).to_be_visible(
        timeout=15_000
    )

    # Close the dialog; warning should also appear in the Tools section.
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden(timeout=5_000)
    _open_popover(page)
    expect(page.get_by_text("Restart to apply changes")).to_be_visible(timeout=15_000)

    # Cleanup: delete the server.
    page.get_by_role("button", name="Manage MCP servers").click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text("Manage MCP Servers"))
    expect(dialog).to_be_visible(timeout=15_000)
    dialog.get_by_role("button", name="Delete dirty-test").click()
    _wait_for(lambda: not _agent_mcp_names(base_url, session_id))


def test_agent_info_mcp_server_added_to_running_session_is_callable(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Add a stdio MCP after runner bind; the runner must see and call it."""
    base_url, session_id = seeded_session
    assert _ECHO_MCP_SERVER.is_file()

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    assert not _agent_mcp_names(base_url, session_id), "session started with MCP servers"

    _open_popover(page)
    page.get_by_role("button", name="Manage MCP servers").click()
    dialog = page.get_by_role("dialog").filter(has=page.get_by_text("Manage MCP Servers"))
    expect(dialog).to_be_visible(timeout=15_000)
    dialog.get_by_label("Name").fill("echo_mcp")
    dialog.get_by_label("Transport").select_option("stdio")
    dialog.get_by_label("Command").fill(sys.executable)
    dialog.get_by_label("Args").fill(str(_ECHO_MCP_SERVER))
    dialog.get_by_role("button", name="Save").click()

    _wait_for(lambda: _agent_mcp_names(base_url, session_id) == {"echo_mcp"})
    _wait_for(
        lambda: "echo_mcp__echo" in _runner_mcp_tool_names(base_url, session_id),
        timeout_s=45.0,
    )
    result = _post_mcp_rpc(
        base_url,
        session_id,
        "tools/call",
        {"name": "echo_mcp__echo", "arguments": {"text": "ui-runtime-probe"}},
    )
    assert result["content"] == [{"type": "text", "text": "echo: ui-runtime-probe"}]


def _wait_for(predicate, *, timeout_s: float = 15.0, interval_s: float = 0.25) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")
