r"""UI journey: two agents chat about The Hitchhiker's Guide across two rounds.

::

       o                                        .-----------------.
      /|\   Arthur Dent                         |   ___________   |
      / \   (top-level agent,                   |  |    42.    |  |  Deep Thought
            session /c/<parent>)                |  |___________|  |  (deep_thought
                                                |_________________|  sub-agent,
                                                                     session /c/<child>)

    PARENT TRANSCRIPT                           CHILD TRANSCRIPT
    .--------------------------------.
    | user                           |
    |   Ask Deep Thought for the     |
    |   Answer to the Ultimate       |
    |   Question of Life, the        |
    |   Universe, and Everything.    |
    '--------------------------------'
         |
         |  sys_session_send                    .---------------------------.
         +------------------------------------> | user (sent by Arthur)     |
         |    (dispatch turn parks)             |   What is the Answer...?  |
         |                                      '---------------------------'
         |                                      .---------------------------.
         |                                      | deep_thought              |
         |         inbox auto-wake              |   The Answer is 42.       |
         + <----------------------------------- |   Verification code:      |
         v                                      |   vogon-<nonce>           |
    .--------------------------------.          '---------------------------'
    | arthur                         |
    |   The Answer is 42.            |
    |   Verification code:           |
    |   vogon-<nonce>                |
    '--------------------------------'

    Round 2 repeats the shape ("what is the Ultimate Question?"), but MUST
    continue the SAME child session: still exactly one Agents-rail row, and
    the child transcript accumulates both replies (babelfish-<nonce> joins
    vogon-<nonce>).

The user asks Arthur (the session's top-level agent) to find out the Answer
to the Ultimate Question of Life, the Universe, and Everything. Arthur's
prompt forbids answering from his own knowledge; he must `sys_session_send`
the question to his `deep_thought` sub-agent. The sub-agent runs AFTER the
dispatch turn ends, then the inbox auto-wakes Arthur in a continuation turn
that relays the reply into the SPA (the same async dispatch contract
documented in tests/e2e/test_named_sub_agent_persistence.py).

Beyond the relay itself, this journey covers the SPA's multi-agent surfaces,
none of which any other UI test touches:

- the `sys_session_send` tool call rendering in the transcript
  (ap-web/src/components/blocks/ToolCard.tsx);
- the right-rail Agents tab and SubagentsPanel child row + status dot
  (ap-web/src/shell/SubagentsPanel.tsx);
- navigation into the child's own `/c/<child-id>` session and back;
- round 2: a follow-up relayed to the SAME child (named continuation —
  one child row after two rounds, the D6 ambient-hint behavior that was
  previously only API-tested), with the child transcript accumulating
  both exchanges.

The load-bearing assertions are the per-run nonces, which exist ONLY in the
sub-agent's prompt (embedded at registration time by the
`two_agent_chat_session` fixture): `verification_code` in its round-1 Answer
reply and `question_code` in its round-2 Question reply. Any model can say
"42" from world knowledge; the nonces can reach the parent's bubbles only via
the real parent -> sub-agent -> inbox -> parent -> SSE -> UI pipeline.

"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import TwoAgentChatSession, open_right_rail

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_SUBAGENT_STATUS_DOT = '[data-testid="subagent-status-dot"]'

# One relay = dispatch turn + sub-agent turn + auto-wake continuation,
# three serial LLM calls, so nonce assertions get a generous budget.
_RELAY_TIMEOUT_MS = 240_000


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _expect_relayed_reply(page: Page, nonce: str) -> None:
    """Assert an assistant bubble carrying *nonce* rendered in the parent chat.

    The nonce only exists in deep_thought's prompt, so its presence in a
    parent assistant bubble proves the full two-agent round trip rendered
    in the UI. Failure means dispatch, the sub-agent turn, the inbox
    auto-wake, or the SPA's continuation-turn rendering broke (or Arthur
    answered from world knowledge, which his prompt forbids).

    :param page: The Playwright page, on the parent session.
    :param nonce: The per-run code expected verbatim in the reply,
        e.g. ``"vogon-3a7f9c2e1b"``.
    """
    expect(page.locator(_ASSISTANT, has_text=nonce).first).to_be_visible(timeout=_RELAY_TIMEOUT_MS)


def _expect_dispatch_tool_call_rendered(page: Page) -> None:
    """Assert the `sys_session_send` dispatch shows as a transcript tool call.

    Completed turns fold their tool calls into a collapsed "See N steps"
    group (ToolCard.tsx ToolGroupSummary), so every group is expanded
    first. The trigger renders toolTitle.ts's raw-name fallback
    ("sys_session_send(...)"), not the friendly "Start child session:"
    verb: sessionTitle() reads `tool`/`session` args while the named
    spawn schema (omnigent/tools/builtins/spawn.py) sends `agent`/`title`,
    so the formatter never matches. If this starts failing with the
    friendly verb present instead, that mismatch was fixed and this
    should assert "Start child session:".

    :param page: The Playwright page, on the parent session.
    """
    step_groups = page.get_by_text(re.compile(r"^See \d+ steps?$"))
    expect(step_groups.first).to_be_visible()
    for group in step_groups.all():
        group.click()
    expect(page.get_by_text(re.compile(r"sys_session_send")).first).to_be_visible()


def _expect_single_deep_thought_row(page: Page) -> str:
    """Open the Agents tab and assert exactly one deep_thought child row.

    The right rail defaults closed per session, so it is expanded first
    via :func:`open_right_rail`. Lookups are scoped to the desktop
    "Workspace" rail so they don't match the hidden mobile drawer that
    mirrors the same testids. A row count of 0 means the child session
    never surfaced in the rail (useChildSessions polling or
    parent_session_id threading broke); 2+ means a duplicate spawn
    instead of continuing the existing child. The tab label is "Agents"
    plus a count badge, so match the prefix.

    :param page: The Playwright page, on the parent session.
    :returns: The child session id from the row's
        ``data-child-session-id`` attribute.
    """
    # Idempotent: expands the rail when collapsed and is a no-op when
    # round 2's return to the parent restores the remembered open state.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    rows = rail.locator(_SUBAGENT_ROW)
    expect(rows).to_have_count(1, timeout=30_000)
    expect(rows.first).to_contain_text("deep_thought")
    expect(rows.first.locator(_SUBAGENT_STATUS_DOT)).to_be_visible()
    child_session_id = rows.first.get_attribute("data-child-session-id")
    assert child_session_id, "subagent row is missing data-child-session-id"
    return child_session_id


def _expect_child_transcript(page: Page, child_session_id: str, nonces: list[str]) -> None:
    """Enter the child session via its panel row and assert its own transcript.

    The child is a real, navigable session: its `/c/<child-id>` page
    hydrates Deep Thought's transcript from the child's own snapshot, so
    every nonce in *nonces* must appear in an assistant bubble there. A
    missing earlier nonce means a continuation replaced rather than
    extended the child's history; a missing latest nonce means the
    follow-up never reached the child.

    :param page: The Playwright page, with the Agents tab open.
    :param child_session_id: The child session to enter, e.g. ``"conv_abc"``.
    :param nonces: Every code expected in the child transcript, oldest first.
    """
    rail = page.get_by_role("complementary", name="Workspace")
    rail.locator(_SUBAGENT_ROW).first.click()
    page.wait_for_url(re.compile(re.escape(f"/c/{child_session_id}")))
    for nonce in nonces:
        expect(page.locator(_ASSISTANT, has_text=nonce).first).to_be_visible(timeout=30_000)


# Nightly: six serial real-LLM turns (two rounds of dispatch + sub-agent +
# auto-wake continuation), so it is too heavy and 429-sensitive for the PR
# gate. The 600s budget overrides the suite-wide 300s default for the same
# reason tests/e2e/test_named_sub_agent_persistence.py uses it: FMAPI
# backoff stacks multiplicatively across the serial turns.
@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_two_agents_discuss_hitchhikers_guide(
    page: Page,
    two_agent_chat_session: TwoAgentChatSession,
) -> None:
    chat = two_agent_chat_session
    parent_url = f"{chat.base_url}/c/{chat.session_id}"
    page.goto(parent_url)

    # Round 1: Arthur relays the Answer from Deep Thought.
    _send(
        page,
        "Let's talk about The Hitchhiker's Guide to the Galaxy. Ask Deep "
        "Thought for the Answer to the Ultimate Question of Life, the "
        "Universe, and Everything, then tell me exactly what it said, "
        "including its verification code.",
    )
    _expect_relayed_reply(page, chat.verification_code)
    # Deep Thought's Answer itself rendered too — the relay was verbatim.
    expect(page.locator(_ASSISTANT, has_text="42").first).to_be_visible()
    _expect_dispatch_tool_call_rendered(page)

    # Deep Thought appeared in the Agents rail, and his session is real:
    # entering it shows his own copy of the round-1 reply.
    child_session_id = _expect_single_deep_thought_row(page)
    _expect_child_transcript(page, child_session_id, [chat.verification_code])

    # Round 2: a follow-up relayed through Deep Thought again.
    page.goto(parent_url)
    _send(
        page,
        "Now ask Deep Thought what the Ultimate Question actually IS, and "
        "report back exactly what it says, including any code.",
    )
    _expect_relayed_reply(page, chat.question_code)

    # Same single Deep Thought — round 2 continued the round-1 child
    # instead of spawning a second one — and his transcript accumulated
    # both exchanges.
    assert _expect_single_deep_thought_row(page) == child_session_id, (
        "round 2 should continue the round-1 deep_thought session, but the "
        "panel row points at a different child session"
    )
    _expect_child_transcript(page, child_session_id, [chat.verification_code, chat.question_code])

    # Back in the parent, the continuation turns fully settled; guards
    # against the auto-wake turn wedging open after the reply streamed.
    page.goto(parent_url)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
