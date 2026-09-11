"""E2E: each sidebar session row is marked with the coding agent it runs.

The sidebar row resolves its agent from the authoritative ``omnigent.wrapper``
label and renders that vendor's icon ahead of the title. Forks inherit
``"Fork of <original>"``, so two rows for the same work are otherwise
indistinguishable — the icon is the only thing separating the Claude Code row
from the Codex one.

This exercises the real chain the unit tests mock out: live session list ->
label on the persisted row -> sidebar render -> labelled icon. No LLM turn and
no native terminal are needed, because the icon is derived from the label
rather than from a running CLI, so this skips the nightly/real-agent markers
the approval suites carry.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

_CLAUDE_WRAPPER = "claude-code-native-ui"
_CODEX_WRAPPER = "codex-native-ui"


def _seed_row(base_url: str, session_id: str, title: str, wrapper: str) -> None:
    """Title a session and stamp the wrapper label its row reads."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title, "labels": {"omnigent.wrapper": wrapper}},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_sidebar_rows_carry_their_coding_agent_icon(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Two identically-titled rows are told apart by their agent icons."""
    base_url, session_a, session_b = seeded_session_pair
    # Same title on both, as a fork pair would have.
    _seed_row(base_url, session_a, "Fork of login redirect", _CLAUDE_WRAPPER)
    _seed_row(base_url, session_b, "Fork of login redirect", _CODEX_WRAPPER)

    page.goto(f"{base_url}/c/{session_a}")

    row_a = page.locator("li").filter(has=page.locator(f'a[href="/c/{session_a}"]'))
    row_b = page.locator("li").filter(has=page.locator(f'a[href="/c/{session_b}"]'))
    expect(row_a).to_be_visible(timeout=30_000)
    expect(row_b).to_be_visible()

    # Each row carries its own vendor's icon, and only its own.
    expect(row_a.get_by_role("img", name="Claude Code")).to_be_visible()
    expect(row_b.get_by_role("img", name="Codex")).to_be_visible()
    expect(row_a.get_by_role("img", name="Codex")).to_have_count(0)
    expect(row_b.get_by_role("img", name="Claude Code")).to_have_count(0)
