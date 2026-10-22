"""Harness picker grouping and availability require only a browser.

The new-session landing picker leads with the fully supported harnesses and
keeps the rest under Other until one is selected, disables a harness the host
reports missing while its tooltip names the repair, and must offer the catalog
rows before the sessions discovery scan resolves. Every input is the
``/v1/agents`` catalog, the ``/v1/hosts`` readiness map, ``/v1/info`` and
``/v1/harnesses``, so the strict browser contract drives the built SPA directly.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from playwright.sync_api import Page, Route, expect

from tests._helpers.picker_routes import OWN_AGENTS
from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload

_HOST_NAME = "e2e-host"
_CLAUDE_AGENT_ID = "ag_claude_e2e"
_CODEX_AGENT_ID = "ag_codex_e2e"
_CURSOR_AGENT_ID = "ag_cursor_e2e"
_PI_AGENT_ID = "ag_pi_e2e"
_CODEX_HARNESS = "codex-native"

# Only the agent-discovery scan uses ``visibility=mine`` without ``pinned``.
_SCAN_RE = re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine")
# How quickly the harness rows must be offered once the composer is on screen.
_PICKER_BUDGET_S = 3.0
_PICKER_WAIT_CEILING_S = 28.0


def _agent(
    agent_id: str, name: str, display_name: str, description: str, harness: str
) -> dict[str, Any]:
    return {
        "id": agent_id,
        "name": name,
        "display_name": display_name,
        "description": description,
        "harness": harness,
        "skills": [],
    }


_CLAUDE = _agent(
    _CLAUDE_AGENT_ID,
    "claude-native-ui",
    "Claude Code",
    "Anthropic's coding agent",
    "claude-native",
)
_CODEX = _agent(
    _CODEX_AGENT_ID, "codex-native-ui", "Codex", "OpenAI's coding agent", _CODEX_HARNESS
)
_CURSOR = _agent(
    _CURSOR_AGENT_ID, "cursor-native-ui", "Cursor", "Cursor's coding agent", "cursor-native"
)
_PI = _agent(_PI_AGENT_ID, "pi-native-ui", "Pi", "Pi coding agent", "pi-native")


def _stub_picker(
    page: Page,
    chat: ChatSessionContract,
    *,
    agents: list[dict[str, Any]],
    configured_harnesses: dict[str, Any],
) -> None:
    """Feed the landing picker one online host plus the given catalog.

    Later registrations win over the chat contract's defaults, so these replace
    the single-agent catalog and host map it installs.
    """
    contract = chat.contract
    contract.json("/v1/agents", {"data": agents})
    contract.json(OWN_AGENTS, {"data": []})
    contract.json("/v1/sessions", list_payload([]))
    contract.json("/v1/skills", {"skills": []})
    contract.json(
        "/v1/hosts",
        {
            "hosts": [
                {
                    "host_id": chat.host_id,
                    "name": _HOST_NAME,
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": configured_harnesses,
                }
            ]
        },
    )
    contract.json(
        re.compile(r"/v1/hosts/[^/]+/harnesses/[^/]+/model-options(?:\?.*)?$"), {"models": []}
    )
    contract.json(f"/v1/hosts/{chat.host_id}/worktrees", {"data": []})
    # Seed a recent working directory so the composer settles on the stub host.
    recent = json.dumps({chat.host_id: ["/work/repo"]})
    page.add_init_script(
        f"localStorage.setItem('omnigent:recent-workspaces', JSON.stringify({recent}))"
    )


def _open_landing(page: Page, chat: ChatSessionContract) -> None:
    page.goto(chat.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)


def _open_picker(page: Page) -> None:
    page.get_by_test_id("new-chat-landing-agent-select").click()


def _agent_row(page: Page, agent_id: str):
    return page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")


def _renders_before(page: Page, first_agent_id: str, second_agent_id: str) -> bool:
    """Whether *first_agent_id*'s row precedes *second_agent_id*'s in document order."""
    return page.evaluate(
        """([firstId, secondId]) => {
            const sel = (id) => document.querySelector(
                `[data-testid="new-chat-landing-agent-${id}"]`
            );
            const a = sel(firstId);
            const b = sel(secondId);
            if (!a || !b) return false;
            return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
        }""",
        [first_agent_id, second_agent_id],
    )


_ALL_READY = {
    "claude-native": True,
    "codex-native": True,
    "cursor-native": True,
    "pi-native": True,
}


def test_recent_harness_remains_in_other_group(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """Recent launches do not change the primary harness order."""
    _stub_picker(
        page,
        chat_session_contract,
        agents=[_CLAUDE, _CODEX, _CURSOR, _PI],
        configured_harnesses=_ALL_READY,
    )
    page.add_init_script(
        "localStorage.setItem('omnigent:recent-harnesses', JSON.stringify(['pi-native']))"
    )
    _open_landing(page, chat_session_contract)
    _open_picker(page)

    # Recent Pi remains in Other, keeping the primary list stable.
    expect(_agent_row(page, _PI_AGENT_ID)).to_have_count(0)
    # Cursor is always primary, independently of launch history.
    expect(_agent_row(page, _CURSOR_AGENT_ID)).to_be_visible()


def test_picker_leads_with_primary_harnesses(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """Primary harnesses lead; the selected secondary harness joins them on reopen."""
    _stub_picker(
        page,
        chat_session_contract,
        agents=[_CLAUDE, _CODEX, _CURSOR, _PI],
        configured_harnesses=_ALL_READY,
    )
    _open_landing(page, chat_session_contract)

    # 1. The fully supported harnesses lead the list inline.
    _open_picker(page)
    expect(_agent_row(page, _CLAUDE_AGENT_ID)).to_be_visible(timeout=30_000)
    expect(_agent_row(page, _CODEX_AGENT_ID)).to_be_visible(timeout=30_000)

    # Cursor is primary; Pi appears only after opening Other.
    expect(_agent_row(page, _CURSOR_AGENT_ID)).to_be_visible()
    expect(_agent_row(page, _PI_AGENT_ID)).to_have_count(0)

    page.get_by_test_id("new-chat-landing-harness-more").click()
    cursor_row = _agent_row(page, _CURSOR_AGENT_ID)
    pi_row = _agent_row(page, _PI_AGENT_ID)
    expect(cursor_row).to_be_visible(timeout=30_000)
    expect(pi_row).to_be_visible()
    # Primary rows precede the Other submenu.
    assert _renders_before(page, _CURSOR_AGENT_ID, _PI_AGENT_ID), (
        "expected primary Cursor row to precede Pi in Other"
    )

    # Selecting Pi keeps its config reachable, then promotes it on reopen.
    pi_row.click()
    expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
        "aria-label", re.compile("Pi")
    )
    page.keyboard.press("Escape")
    expect(page.get_by_role("menu")).to_have_count(0)
    _open_picker(page)
    expect(page.get_by_test_id("new-chat-landing-harness-more")).to_have_count(0)
    expect(pi_row).to_be_visible()
    expect(_agent_row(page, _PI_AGENT_ID)).to_have_attribute("data-active", "true")


def test_missing_harness_is_disabled_with_repair_tooltip(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A missing harness stays unselectable and explains how to repair it."""
    chat = chat_session_contract
    contract = chat.contract
    # Claude is ready and stays the inline default, so Codex exercises the More
    # submenu; the host reports Codex missing.
    _stub_picker(
        page,
        chat,
        agents=[_CLAUDE, _CODEX],
        configured_harnesses={"claude-native": True, _CODEX_HARNESS: False},
    )
    contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": False,
            "managed_sandboxes_enabled": False,
            "sandbox_provider": None,
            "sharing_mode": "on",
            "public_sharing_enabled": True,
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
            "harness_install_enabled": True,
            "installable_harnesses": ["codex", _CODEX_HARNESS],
        },
    )
    # setup_steps is keyed by the native spelling: the shape the setup dialog reads.
    contract.json(
        "/v1/harnesses",
        {
            "data": [{"id": "codex", "label": "Codex"}],
            "setup_steps": {
                _CODEX_HARNESS: [
                    {
                        "kind": "install",
                        "title": "Install Codex",
                        "detail": "We'll install Codex on the host for you.",
                        "action": "install",
                        "command": None,
                        "status_key": "installed",
                    },
                    {
                        "kind": "auth",
                        "title": "Set up authentication",
                        "detail": (
                            "Sign in with your ChatGPT subscription, an API key, or a gateway."
                        ),
                        "action": "auth",
                        "command": "codex login",
                        "status_key": "authed",
                    },
                ]
            },
        },
    )
    install_requests: list[str] = []

    def install(route: Route) -> None:
        install_requests.append(route.request.url)
        route.fulfill(
            json={
                "object": "harness_install",
                "harness": _CODEX_HARNESS,
                "configured_harnesses": {_CODEX_HARNESS: True},
            }
        )

    contract.route(re.compile(rf"/v1/hosts/[^/]+/harnesses/{_CODEX_HARNESS}/install$"), install)

    _open_landing(page, chat)
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    expect(picker).to_have_attribute("aria-label", re.compile(r"^Claude Code,"))
    picker.click()
    page.get_by_test_id("new-chat-landing-harness-more").click()
    codex_option = _agent_row(page, _CODEX_AGENT_ID)
    expect(codex_option).to_be_visible(timeout=60_000)
    expect(codex_option).to_have_attribute("aria-disabled", "true")
    codex_option.get_by_text("Codex", exact=True).hover()
    expect(
        page.get_by_test_id(f"new-chat-landing-agent-tooltip-{_CODEX_AGENT_ID}")
    ).to_contain_text(f"Codex isn't configured on {_HOST_NAME} — run omni setup on that machine.")
    expect(picker).to_have_attribute("aria-label", re.compile(r"^Claude Code,"))
    assert install_requests == []


def test_harness_picker_not_blocked_by_slow_session_scan(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """Harness rows must appear promptly even when the discovery scan is slow.

    The catalog and host requests resolve immediately; only the
    ``visibility=mine`` sessions scan is outstanding. The picker must offer the
    catalog harnesses within ``_PICKER_BUDGET_S`` instead of sitting disabled
    ("No agents") until the scan returns.
    """
    chat = chat_session_contract
    _stub_picker(
        page,
        chat,
        agents=[{**_CLAUDE, "builtin": True}, {**_CODEX, "builtin": True}],
        configured_harnesses={"claude-native": True, _CODEX_HARNESS: True},
    )
    # The scan itself succeeds; it is merely slow. A sync route handler cannot
    # sleep, so hold the request and answer it once the picker has been judged.
    held_scans: list[Route] = []

    def hold_scan(route: Route) -> None:
        held_scans.append(route)

    chat.contract.route(_SCAN_RE, hold_scan)
    try:
        _open_landing(page, chat)
        picker = page.get_by_test_id("new-chat-landing-agent-select")
        picker.wait_for(state="attached", timeout=10_000)

        # Clock starts when the composer is interactive: from here the user is
        # looking at the picker waiting for harnesses to show up.
        start = time.monotonic()
        deadline = start + _PICKER_WAIT_CEILING_S
        enabled_after_s: float | None = None
        while time.monotonic() < deadline:
            if picker.is_enabled():
                enabled_after_s = time.monotonic() - start
                break
            page.wait_for_timeout(100)

        assert enabled_after_s is not None, (
            "harness picker never offered any agents: it stayed disabled "
            f"('No agents') for {_PICKER_WAIT_CEILING_S:.0f}s even though "
            "GET /v1/agents returned the harness catalog immediately"
        )

        # Journey sanity: once enabled, the picker really offers the catalog harnesses.
        picker.click()
        expect(_agent_row(page, _CLAUDE_AGENT_ID)).to_be_visible(timeout=10_000)
        expect(_agent_row(page, _CODEX_AGENT_ID)).to_be_visible(timeout=10_000)

        assert enabled_after_s <= _PICKER_BUDGET_S, (
            f"harnesses took {enabled_after_s:.1f}s to show up in the "
            "new-session composer's picker (disabled, 'No agents') even "
            "though GET /v1/agents answered instantly — the picker is "
            "hostage to the sessions discovery scan (still outstanding here); "
            f"catalog rows should render within {_PICKER_BUDGET_S:.0f}s "
            "without waiting for the scan"
        )
    finally:
        for route in held_scans:
            route.fulfill(json=list_payload([]))
