"""E2E regression: agents default to their own web tooling because the
composed system prompt never steers them to the Omnigent embedded browser.

Reported journey: a user on the Omnigent desktop app (embedded Browser pane
available) asks their agent to look at a web page with a neutral prompt — no
"use the browser pane" coaching. The agent answers via its OWN web tooling
(Claude Code's WebFetch, a shell ``curl``, launching chrome) and the embedded
Browser pane never opens, even though the framework-owned ``browser_*`` tools
are advertised to every agent (``ToolManager._register_browser_tools``).

The reporter's named mechanism — and the requested fix — is the system
prompt: nothing in the composed instructions steers the model toward the
Omnigent embedded browser, so models default to their native web tooling.

Two guards, one per facet:

1. ``test_instructions_steer_model_to_embedded_browser`` — deterministic
   (mock LLM). Drives a real composer turn through the SPA against the
   spawned server and inspects the exact request the model received. The
   ``browser_*`` tools must be advertised (the precondition that makes the
   missing guidance a defect) AND the composed instructions must carry
   guidance prioritizing the embedded browser. The second assertion fails
   until the guidance lands in prompt composition; it is the deterministic
   fail→pass target.

2. ``test_agent_reaches_for_embedded_browser_on_neutral_browse_ask`` —
   behavioral (real gateway; skipped without ``LLM_API_KEY``). Boots the real
   ``claude-native`` wrapper, asks it to look at a page with the reporter's
   neutral phrasing, and asserts the canonical transcript shows the agent
   reaching for the embedded browser (``browser_navigate``). On the buggy
   build Claude answers via WebFetch/Bash and never touches the omnigent
   browser surface, so the assertion fails — the live form of the report.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
    reset_mock_llm,
)

from ..messages.test_message_render_parity import _ASSISTANT, _ensure_chat_view, _send
from .test_browser_tab import _ELECTRON_SHELL_INIT_SCRIPT

# Model baked into the seeded hello_world agent (tests/e2e_ui/conftest.py
# ``_TEST_AGENT_YAML``); keys the mock queue for the deterministic turn.
_HELLO_WORLD_MODEL = "gpt-4o-mini"

# A custom openai-agents turn is a single LLM call against the mock.
_MOCK_TURN_TIMEOUT_MS = 90_000

# Native Claude boots in the session terminal on the first turn; cold CI
# runners are slow, and a real-gateway browse turn adds model latency.
_NATIVE_TURN_TIMEOUT_MS = 240_000

# The reporter's trigger, deliberately neutral: it names no tool and never
# says "browser pane" — the bug is that WITHOUT extra prompting the agent
# defaults to its own web tooling instead of the embedded browser.
_NEUTRAL_BROWSE_ASK = "Look at https://example.com and tell me the exact text of its main heading."

# What the fix must inject: composed instructions that steer the
# model toward the Omnigent embedded browser. Matched loosely (any of the
# browser tool names, or the embedded/Omnigent browser named in prose) so a
# reasonably-worded fix passes without pinning its exact phrasing.
_BROWSER_GUIDANCE_RE = re.compile(
    r"browser_navigate|browser_snapshot|browser_click|browser_type|"
    r"browser_screenshot|embedded browser|omnigent browser",
    re.IGNORECASE,
)


def _request_instruction_text(parsed: dict[str, Any]) -> str:
    """Extract every system-side instruction string from a captured request.

    Handles both wire shapes the builtin harnesses emit: a Responses-API
    top-level ``instructions`` string, and system/developer-role items in
    ``input`` / ``messages`` lists.

    :param parsed: A captured request body from ``GET /mock/requests``.
    :returns: Newline-joined instruction text (``""`` when none).
    """
    parts: list[str] = []

    instructions = parsed.get("instructions")
    if isinstance(instructions, str):
        parts.append(instructions)

    def grab(content: object) -> None:
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(content, list):
            for block in content:
                grab(block)

    for list_key in ("input", "messages", "system"):
        value = parsed.get(list_key)
        if isinstance(value, str) and list_key == "system":
            parts.append(value)
            continue
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict) and item.get("role") in ("system", "developer"):
                grab(item.get("content"))

    return "\n".join(parts)


def _request_tool_names(parsed: dict[str, Any]) -> set[str]:
    """Collect the advertised tool names from a captured request.

    Handles both the Responses-API flat shape (``{"type": "function",
    "name": ...}``) and the chat-completions nested shape
    (``{"function": {"name": ...}}``).

    :param parsed: A captured request body from ``GET /mock/requests``.
    :returns: The set of advertised tool names.
    """
    names: set[str] = set()
    tools = parsed.get("tools")
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if isinstance(name, str):
            names.add(name)
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


def _request_user_text(parsed: dict[str, Any]) -> str:
    """Concatenate the ``role="user"`` text of a captured mock request.

    :param parsed: A captured request body from ``GET /mock/requests``.
    :returns: Space-joined user-role text (``""`` when none).
    """
    parts: list[str] = []

    def grab(content: object) -> None:
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
            if content.get("content") is not None:
                grab(content["content"])
        elif isinstance(content, list):
            for block in content:
                grab(block)

    for list_key in ("input", "messages"):
        value = parsed.get(list_key)
        if isinstance(value, str):
            grab(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("role") == "user":
                    grab(item.get("content"))
    return " ".join(parts)


def test_instructions_steer_model_to_embedded_browser(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The composed system prompt must prioritize the embedded browser.

    Journey (driven end-to-end through the SPA):

    1. open an existing session on the default agent;
    2. ask the agent, neutrally, to look at a web page;
    3. inspect the exact request the model received.

    Asserts the embedded-browser prompt contract on that request:

    - the framework-owned ``browser_*`` tools are advertised (precondition —
      the embedded-browser surface exists for this agent), and
    - the composed instructions carry guidance steering the model to the
      Omnigent embedded browser (the fix; FAILS until it lands — today the
      instructions never mention the browser at all, which is exactly why
      agents default to their own web tooling).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :param mock_llm_server_url: Mock LLM server base URL.
    """
    base_url, session_id = seeded_session

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "browse-guidance-reply: I looked at the page."}],
        key=_HELLO_WORLD_MODEL,
    )

    page.goto(f"{base_url}/c/{session_id}")
    _send(page, _NEUTRAL_BROWSE_ASK)
    expect(page.locator(_ASSISTANT, has_text="browse-guidance-reply").first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )

    captured = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0).json()["requests"]
    ours = [req for req in captured if _NEUTRAL_BROWSE_ASK in _request_user_text(req)]
    assert ours, "the browse-ask turn never reached the mock LLM"
    request = ours[-1]

    # Precondition: the embedded-browser tool surface IS advertised to the
    # model on a user-driven turn — this is what makes the missing prompt
    # guidance a defect rather than a missing feature.
    tool_names = _request_tool_names(request)
    assert "browser_navigate" in tool_names, (
        "expected the framework-owned browser_* tools to be advertised to the "
        f"model; advertised tools were {sorted(tool_names)!r}"
    )

    # The composed instructions must steer the model toward the
    # embedded browser it was just handed. Today they never mention it.
    instruction_text = _request_instruction_text(request)
    assert _BROWSER_GUIDANCE_RE.search(instruction_text), (
        "browser_* tools are advertised but the composed system "
        "prompt carries no guidance prioritizing the Omnigent embedded "
        "browser, so models default to their own web tooling. Instructions "
        f"received by the model were:\n---\n{instruction_text}\n---"
    )


def _transcript_items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    """Fetch the canonical transcript items for *session_id*.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: The transcript item dicts, oldest first.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=1000", timeout=15.0)
    resp.raise_for_status()
    return list(resp.json()["data"])


def _tool_call_names(items: list[dict[str, Any]]) -> list[str]:
    """Extract the tool/function call names from transcript *items*.

    :param items: Transcript items from :func:`_transcript_items`.
    :returns: The call names, in transcript order.
    """
    names: list[str] = []
    for item in items:
        data = item.get("data") or {}
        if item.get("type") in ("function_call", "native_tool"):
            name = item.get("name") or data.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


@pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("tmux") is None,
    reason="claude-native e2e needs the `claude` CLI and `tmux` on PATH.",
)
@pytest.mark.skipif(
    not os.environ.get("LLM_API_KEY"),
    reason="behavioral browse turn needs real gateway credentials (LLM_API_KEY).",
)
def test_agent_reaches_for_embedded_browser_on_neutral_browse_ask(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A neutral browse ask must reach the embedded browser, unprompted.

    The live form of the reported journey, on the real ``claude-native``
    wrapper against the real gateway:

    1. open a claude-native session in the app with the Browser pane
       available (Electron bridge stubbed, as in ``test_browser_tab``) and
       the empty Browser pane showing in the right Workspace rail;
    2. ask the agent — neutrally, no tool coaching — to look at a web page;
    3. the agent answers via its OWN web tooling (WebFetch / shell) and the
       embedded browser is never driven: no ``browser_navigate`` call ever
       lands in the canonical transcript, and the pane stays empty.

    Asserts the fixed behavior — the transcript records the agent reaching
    for the embedded browser — so the test FAILS on the buggy build, naming
    the tools the agent used instead.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_claude_session(live_server, runner_id)
    try:
        page.add_init_script(_ELECTRON_SHELL_INIT_SCRIPT)
        page.goto(f"{live_server}/c/{session_id}")
        _ensure_chat_view(page)

        # Show the (empty) embedded-browser pane alongside the chat: the
        # user-visible half of the bug is that it STAYS empty while the
        # agent browses with its own tooling.
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        browser_tab = rail.get_by_role("tab", name=re.compile("Browser"))
        expect(browser_tab).to_be_visible(timeout=30_000)
        browser_tab.click()
        expect(browser_tab).to_have_attribute("aria-selected", "true")

        _send(page, _NEUTRAL_BROWSE_ASK)

        # Wait out the real turn: the working shimmer appears once the runner
        # dispatches (Claude Code may still be booting in the terminal), and
        # the turn is settled when it clears with an assistant reply landed.
        working = page.locator('[data-testid="working-indicator"]')
        expect(working.first).to_be_visible(timeout=_NATIVE_TURN_TIMEOUT_MS)
        expect(working).to_have_count(0, timeout=_NATIVE_TURN_TIMEOUT_MS)
        expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=30_000)

        # Let the transcript mirror settle, then read the canonical items.
        time.sleep(3.0)
        items = _transcript_items(live_server, session_id)
        call_names = _tool_call_names(items)
        transcript_blob = json.dumps(items)

        assert "browser_navigate" in transcript_blob, (
            "asked to look at a web page, the agent never reached "
            "for the Omnigent embedded browser (no browser_navigate call in "
            "the transcript) — it used its own web tooling instead. Tool "
            f"calls this turn were: {call_names!r}"
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)
