"""A gateway-backed native Claude launch must not surface the endpoint's WebSearch rejection.

The runner's provider config pins ``ANTHROPIC_BASE_URL`` at a non-Anthropic host (the
mock model server), the launch shape a Databricks AI Gateway launch has. The mock stands
in for that gateway: it calls WebSearch when offered and rejects Claude Code's nested
server-side ``web_search`` request with a region error. The user must not see that error
as the search outcome, and the launch must not route the nested request. When the launch
withholds WebSearch, the scripted model answers without searching.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm

from .test_message_render_parity import _ASSISTANT, _USER, _WORKING, _select_view_mode
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_PROMPT_TOKEN = "WEBSEARCH-GATEWAY-PROBE-7f3a"
_NESTED_TOKEN = "websearch-gateway-nested-7f3a"
_PROMPT = f"Search the web for today's weather in Paris ({_PROMPT_TOKEN})"
# The mock serves the longest matching token, so these three selectors rank the
# scripted turns: search offered > search deferred behind ToolSearch > no search.
_SEARCH_OFFERED_MATCH = f"weather in Paris ({_PROMPT_TOKEN})"
_SEARCH_DEFERRED_MATCH = f"Paris ({_PROMPT_TOKEN})"
_NO_SEARCH_MATCH = _PROMPT_TOKEN
_RESTRICTION = (
    "Web search is only available in the US: the web_search tool is not supported "
    "in this region (mock gateway US-only restriction)."
)
_RESTRICTION_MARKER = "only available in the US"
_FINAL_TEXT = "WEBSEARCH-TURN-FINISHED"
_NO_SEARCH_TEXT = (
    "I can't search the web from this session, so I can't look up today's Paris "
    f"weather; a forecast site will have the current conditions. {_FINAL_TEXT}"
)
_TURN_SETTLED = re.compile(rf"{_FINAL_TEXT}|Mock LLM response")
_TURN_TIMEOUT_S = 150.0
_APPROVAL_CARD = '[data-testid="approval-card"]'
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]|\r")
_EVIDENCE_DIR_ENV = "OMNIGENT_E2E_EVIDENCE_DIR"


def _script_gateway(mock_url: str) -> None:
    """Script the mock gateway: call WebSearch when offered, reject its nested server leg."""
    reset_mock_llm(mock_url)
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "toolu_ws_1",
                        "name": "WebSearch",
                        "arguments": json.dumps({"query": f"Paris weather today {_NESTED_TOKEN}"}),
                    }
                ]
            },
            {"text": _FINAL_TEXT},
            {"text": _FINAL_TEXT},
        ],
        match=_SEARCH_OFFERED_MATCH,
        required_tools=["WebSearch"],
    )
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "toolu_ts_1",
                        "name": "ToolSearch",
                        "arguments": json.dumps({"query": "WebSearch"}),
                    }
                ]
            },
            {"text": _NO_SEARCH_TEXT},
            {"text": _NO_SEARCH_TEXT},
        ],
        match=_SEARCH_DEFERRED_MATCH,
        required_tools=["ToolSearch"],
    )
    # Guard on Bash: the main turn always advertises it, while Claude Code's
    # tool-less background requests (title generation) must not consume the reply.
    configure_mock_llm(
        mock_url,
        [{"text": _NO_SEARCH_TEXT}] * 3,
        match=_NO_SEARCH_MATCH,
        required_tools=["Bash"],
    )
    rejection = [{"error": _RESTRICTION, "status_code": 400}] * 6
    configure_mock_llm(mock_url, rejection, key=_CLAUDE_MOCK_MODEL, required_tools=["web_search"])
    configure_mock_llm(mock_url, rejection, key="default", required_tools=["web_search"])


def _is_server_web_search_tool(tool: object) -> bool:
    return isinstance(tool, dict) and (
        tool.get("name") == "web_search" or str(tool.get("type", "")).startswith("web_search")
    )


def _nested_web_search_requests(mock_url: str) -> list[dict]:
    requests = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
    return [
        r
        for r in requests
        if isinstance(r, dict)
        and any(_is_server_web_search_tool(t) for t in (r.get("tools") or []))
    ]


def _transcript(base_url: str, session_id: str) -> list[dict]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def _pane_text(base_url: str, session_id: str) -> str:
    """Read the Claude Code pane through the server's read-only terminal attach."""
    from websockets.sync.client import connect

    resources = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0).json()
    terminals = [r for r in resources.get("data", []) if r.get("type") == "terminal"]
    if not terminals:
        return ""
    ws_base = base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    ws_url = (
        f"{ws_base}/v1/sessions/{session_id}/resources/terminals/{terminals[0]['id']}"
        "/attach?read_only=true"
    )
    chunks: list[bytes] = []
    deadline = time.monotonic() + 3.0
    with connect(ws_url, open_timeout=10.0) as ws:
        while time.monotonic() < deadline:
            try:
                frame = ws.recv(timeout=1.0)
            except TimeoutError:
                continue
            if isinstance(frame, bytes):
                chunks.append(frame)
    text = _ANSI.sub("", b"".join(chunks).decode("utf-8", errors="replace"))
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def _approve_pending_permission(page: Page) -> bool:
    card = page.locator(_APPROVAL_CARD).filter(
        has=page.get_by_role("button", name="Approve", exact=True)
    )
    if card.count() == 0:
        return False
    card.first.get_by_role("button", name="Approve", exact=True).click()
    return True


def _wait_for_turn(page: Page) -> bool:
    """Approve the first WebSearch permission card and wait for the turn to settle.

    Returns whether a card appeared.
    """
    deadline = time.monotonic() + _TURN_TIMEOUT_S
    approved = False
    while time.monotonic() < deadline:
        if not approved and _approve_pending_permission(page):
            approved = True
        if page.locator(_ASSISTANT, has_text=_TURN_SETTLED).count() > 0:
            return approved
        page.wait_for_timeout(1_000)
    return approved


def _evidence_path(name: str) -> Path | None:
    root = os.environ.get(_EVIDENCE_DIR_ENV)
    if not root:
        return None
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return path / name


def _show_websearch_outcome(page: Page) -> None:
    """Best-effort: unfold the turn's tool run and expand the WebSearch card for the footage."""
    try:
        for fold_name in (r"^Worked for", r"^Ran \d+ search"):
            fold = page.get_by_role("button", name=re.compile(fold_name)).first
            if fold.count() > 0 and fold.get_attribute("data-state") != "open":
                fold.click()
                page.wait_for_timeout(300)
        trigger = page.get_by_role("button", name=re.compile(r"Web search:")).first
        expect(trigger).to_be_visible(timeout=30_000)
        trigger.scroll_into_view_if_needed()
        if trigger.get_attribute("data-state") != "open":
            trigger.click()
        page.wait_for_timeout(500)
        restriction = page.get_by_text(_RESTRICTION_MARKER).first
        restriction.scroll_into_view_if_needed()
        expect(restriction).to_be_visible(timeout=10_000)
        page.wait_for_timeout(4_000)
    except Exception:  # the transcript already carries the evidence this only illustrates
        _log.warning("could not expand the WebSearch card in the chat view", exc_info=True)


@pytest.mark.nightly
@pytest.mark.timeout(400)
def test_gateway_launch_does_not_surface_websearch_region_error(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A web search in a gateway-backed native Claude session shows no US-only API error."""
    base_url, session_id = native_claude_mock_session
    _script_gateway(mock_llm_server_url)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    page.wait_for_timeout(1_500)
    _type_into_tui(page, _PROMPT)

    _select_view_mode(page, "Chat")
    expect(page.locator(_USER, has_text=_PROMPT_TOKEN).first).to_be_visible(timeout=60_000)
    approved = _wait_for_turn(page)
    # Both scripted replies end with the marker; the mock's generic fallback does not.
    expect(page.locator(_ASSISTANT, has_text=_FINAL_TEXT).first).to_be_visible(timeout=10_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)

    transcript = _transcript(base_url, session_id)
    transcript_text = json.dumps(transcript)
    nested = _nested_web_search_requests(mock_llm_server_url)
    surfaced = _RESTRICTION_MARKER in transcript_text
    if surfaced:
        _show_websearch_outcome(page)
    else:
        page.wait_for_timeout(3_000)
    if shot := _evidence_path("chat-websearch-outcome.png"):
        page.screenshot(path=str(shot), full_page=False)

    _select_view_mode(page, "Terminal")
    _wait_terminal_connected(page)
    page.wait_for_timeout(4_000)
    if shot := _evidence_path("terminal-websearch-outcome.png"):
        page.screenshot(path=str(shot), full_page=False)
    pane = ""
    try:
        pane = _pane_text(base_url, session_id)
    except Exception as exc:  # pane text is supporting evidence only
        pane = f"<pane capture unavailable: {exc}>"
    if dump := _evidence_path("observation.json"):
        dump.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "websearch_approved": approved,
                    "nested_web_search_requests": len(nested),
                    "restriction_in_transcript": surfaced,
                    "pane": pane,
                    "transcript": transcript,
                },
                indent=1,
            ),
            encoding="utf-8",
        )

    assert not surfaced, (
        "WebSearch under the gateway-backed launch surfaced the endpoint's US-only "
        f"region rejection to the user as the search outcome; pane:\n{pane[-1500:]}"
    )
    assert not nested, (
        "the launch routed Claude Code's nested server-side web_search request to the "
        f"gateway endpoint ({len(nested)} request(s)), which cannot serve it"
    )
