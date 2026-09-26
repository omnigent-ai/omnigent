r"""UI journey: thinking shown in the terminal must reach the chat.

A ``claude-native`` session is terminal-first: the real ``claude`` CLI runs in
the session terminal, and the SPA's Chat view renders the conversation the
native bridge mirrors OUT of Claude Code's transcript. When the model streams
an extended-thinking block, the TUI surfaces the thought (live "thinking"
status; ctrl+o expands the settled thought) and Claude Code persists it as a
``thinking`` content block in its transcript JSONL — so the chat must surface
the same reasoning context as an expandable section on the assistant turn, or
web users lose context the terminal plainly has.

Guards the bridge's transcript mirror
(``_assistant_transcript_items_from_entry`` in
``omnigent/harnesses/claude_native/bridge.py``): when it drops ``thinking``
blocks, the web chat shows the assistant text with no reasoning dropdown at
all and the thought is nowhere in the chat transcript.

Journey (all real product surfaces; the mock LLM scripts the model turn):

1. open a claude-native session; the runner boots the real Claude Code TUI
2. send a prompt from the web composer; the scripted model turn streams a
   thinking block before its final answer
3. the TUI runs the thinking turn and Claude Code writes the thought to its
   transcript JSONL — the "terminal side has it" half
4. switch to Chat view: the assistant turn must offer an expandable
   "Thought for …" section revealing that thought — the half that is broken

Requires the mock LLM lane (``LLM_API_KEY`` unset): the turn scripts a
thinking block deterministically, which a real backend cannot guarantee.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

# Shared web-surface helpers — both suites render from the same transcript.
from .test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _select_view_mode,
    _send,
)
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _pane_text,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    bool(os.environ.get("LLM_API_KEY")),
    reason="requires the mock LLM lane: the turn scripts a thinking block "
    "deterministically, which a real backend cannot guarantee",
)

# How long the scripted turn gets to land its assistant message in the
# canonical transcript (covers TUI injection + the paced thinking stream).
_TURN_ITEM_TIMEOUT_S = 90.0
# How long Claude Code gets to persist the turn's thinking block into its
# transcript JSONL after the answer lands.
_TRANSCRIPT_SETTLE_TIMEOUT_S = 30.0
# Seconds between scripted SSE events — paces the thinking stream so the TUI
# visibly renders the thinking phase ("(Ns · thinking)") as it arrives.
_CHUNK_DELAY_S = 0.4


def _configure_thinking_turn(
    mock_url: str,
    *,
    match: str,
    thinking: str,
    text: str,
) -> None:
    """Script the matched turn: a thinking block, then the final text.

    Routed by content (``match``) so only requests carrying this test's
    user marker draw from the queue. Several copies are queued so a stray
    background request that echoes the conversation (e.g. title
    generation) cannot drain the turn's response.

    :param mock_url: Mock LLM server base URL.
    :param match: Content-routing token (the turn's unique user marker).
    :param thinking: Thought text streamed in the thinking block.
    :param text: Final assistant text streamed after the thought.
    """
    resp = httpx.post(
        f"{mock_url}/mock/configure",
        json={
            "key": _CLAUDE_MOCK_MODEL,
            "match": match,
            "responses": [{"thinking": thinking, "text": text, "chunk_delay": _CHUNK_DELAY_S}] * 4,
        },
        timeout=5.0,
    )
    resp.raise_for_status()


def _transcript_has_thinking(marker: str) -> bool:
    """Whether any Claude Code transcript holds a thinking block with *marker*.

    Claude Code appends each assistant message — thinking blocks included —
    to ``~/.claude/projects/**/*.jsonl``. The spawned runner shares this
    machine and HOME, so this reads the same transcript the native bridge
    mirrors (and the TUI's thought view renders), proving the harness-side
    reasoning context genuinely exists.

    :param marker: Unique token embedded in the scripted thought.
    :returns: ``True`` when a ``thinking`` content block carries *marker*.
    """
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return False
    for path in root.glob("**/*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if marker not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = entry.get("message")
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "thinking"
                            and marker in str(block.get("thinking", ""))
                        ):
                            return True
        except OSError:
            continue
    return False


def _assistant_item_texts(base_url: str, session_id: str) -> list[str]:
    """Joined text of every assistant message item in the canonical transcript.

    :param base_url: Spawned server base URL.
    :param session_id: The session/conversation id.
    :returns: One joined string per assistant message item.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    texts: list[str] = []
    for item in resp.json().get("data", []):
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        texts.append(
            " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
        )
    return texts


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_native_claude_thinking_surfaces_as_reasoning_in_chat(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Thinking the terminal has must render as an expandable reasoning section in chat."""
    base_url, session_id = native_claude_mock_session
    _log.info("native-claude mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Claude Code TUI attached (terminal-view connected)")

    nonce = uuid.uuid4().hex[:8]
    user_marker = f"usr{nonce}"
    assistant_token = f"ast{nonce}"
    thinking_marker = f"thought{nonce}"
    # Pad with prose so the thought streams over several deltas; repeat the
    # marker so it stays findable wherever the surface wraps or truncates.
    thinking_text = (
        f"{thinking_marker} I am weighing how to answer this turn. "
        f"The user asked for one exact token and nothing else, so I will "
        f"reply with it verbatim. {thinking_marker} confirming the token "
        f"one more time before answering. {thinking_marker}"
    )

    reset_mock_llm(mock_llm_server_url)
    # Background/untagged requests (boot probes, title generation) get a
    # plain text response; only the marker-tagged turn gets the thought.
    set_fallback_mock_llm(mock_llm_server_url, "default", assistant_token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, assistant_token)
    _configure_thinking_turn(
        mock_llm_server_url,
        match=user_marker,
        thinking=thinking_text,
        text=assistant_token,
    )

    # --- The user's turn: send from the web composer. ---
    _ensure_chat_view(page)
    _send(
        page,
        f"Context marker {user_marker}. "
        f"Reply with exactly this token and nothing else: {assistant_token}",
    )

    # --- Terminal half: the real TUI runs the thinking turn. ---
    # Watch the TUI while the turn streams (the live "thinking" phase), and
    # wait for the assistant answer to land in the canonical transcript.
    _select_view_mode(page, "Terminal")
    _wait_terminal_connected(page)
    pane_showed_thinking = False
    answer_item_landed = False
    deadline = time.monotonic() + _TURN_ITEM_TIMEOUT_S
    while time.monotonic() < deadline:
        pane = _pane_text(base_url, session_id)
        # Live thinking status ("(3s · thinking)") or the raw thought text.
        if "· thinking" in pane or thinking_marker in pane:
            pane_showed_thinking = True
        if any(assistant_token in text for text in _assistant_item_texts(base_url, session_id)):
            answer_item_landed = True
            break
        page.wait_for_timeout(150)
    _log.info(
        "turn observation: pane_showed_thinking=%s answer_item_landed=%s",
        pane_showed_thinking,
        answer_item_landed,
    )

    # --- Chat half: the mirrored turn renders in the web transcript. ---
    _ensure_chat_view(page)
    bubble = page.locator(_ASSISTANT, has_text=assistant_token).first
    expect(bubble).to_be_visible(timeout=_MOCK_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    # Precondition guard (holds before AND after the fix): the harness side
    # really produced the thought. Claude Code persists thinking blocks to
    # its transcript JSONL — the exact surface the bridge mirrors and the
    # TUI renders. If it never appears, the model turn did not think and the
    # chat-side assertions below would be vacuous.
    transcript_deadline = time.monotonic() + _TRANSCRIPT_SETTLE_TIMEOUT_S
    transcript_has_thinking = False
    while time.monotonic() < transcript_deadline:
        if _transcript_has_thinking(thinking_marker):
            transcript_has_thinking = True
            break
        time.sleep(0.5)
    assert transcript_has_thinking, (
        "precondition failed: the scripted thinking block never reached "
        "Claude Code's transcript JSONL, so the harness side has no "
        "reasoning context to mirror — the model turn did not think"
    )

    # --- The bug: the chat must offer the same reasoning context. ---
    # A settled turn may fold its pre-answer trace behind the "Worked for"
    # row; expand it so a correctly-mirrored thought is reachable.
    fold = bubble.get_by_test_id("turn-worked-fold")
    if fold.count() > 0:
        fold.first.click()

    # The assistant turn offers an expandable reasoning section. When no
    # reasoning item is mirrored at all, there is no dropdown to open —
    # this is where the unfixed mirror fails.
    reasoning_trigger = bubble.get_by_role("button", name=re.compile(r"Thought for "))
    expect(reasoning_trigger.first).to_be_visible(timeout=10_000)

    # Expanding it reveals the reasoning context the terminal side holds.
    reasoning_trigger.first.click()
    expect(bubble.get_by_text(thinking_marker).first).to_be_visible(timeout=10_000)
