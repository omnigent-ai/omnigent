"""E2E: the visible whitespace around a user message is symmetric.

Every chat bubble carries a hover-revealed action row (timestamp / Copy /
Fork) that is transparent at rest but still occupies layout space. The
assistant bubble's row is a direct child of ``Message`` (a ``gap-2``
column), while the user bubble nests its row inside a custom shrink-wrap
column, so the two can drift apart: when the user-side column drops the
shared content→actions spacing, a sent message sits visibly closer to the
reply below it than to the history above it, and the transcript's vertical
rhythm reads uneven around every sent message.

This test measures the *visible-at-rest* gaps (ignoring the transparent
action rows) above and below a user bubble that has an assistant message on
both sides, and asserts they are equal — for history-hydrated turns, for a
live-sent turn, and again after a reload re-renders everything from history.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_turn

_COMPOSER = "Send a message…"
_BUBBLE = '[data-testid="message-bubble"]'
_WORKING = '[data-testid="working-indicator"]'

# The at-rest visible box of each bubble: the union of descendant boxes that
# are actually visible (non-zero opacity chain and painted text/background),
# which excludes the transparent hover action rows. Gaps are measured between
# consecutive bubbles' visible boxes — exactly the whitespace a reader sees.
_VISIBLE_GAPS_JS = """
() => {
  const visibleBox = (root) => {
    let top = Infinity, bottom = -Infinity;
    const walk = (el) => {
      const style = getComputedStyle(el);
      if (style.opacity === '0' || style.display === 'none') return;
      const hasText = [...el.childNodes].some(
        (n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
      if (hasText || style.backgroundColor !== 'rgba(0, 0, 0, 0)') {
        const r = el.getBoundingClientRect();
        if (r.height > 0) {
          top = Math.min(top, r.top);
          bottom = Math.max(bottom, r.bottom);
        }
      }
      for (const c of el.children) walk(c);
    };
    walk(root);
    return { top, bottom };
  };
  const bubbles = [...document.querySelectorAll('[data-testid="message-bubble"]')];
  const rows = bubbles.map((b) => ({
    role: b.getAttribute('data-role'),
    box: visibleBox(b),
  }));
  const gaps = [];
  for (let i = 1; i < rows.length; i++) {
    gaps.push({
      from: rows[i - 1].role,
      to: rows[i].role,
      gap: Math.round(rows[i].box.top - rows[i - 1].box.bottom),
    });
  }
  return gaps;
}
"""


def _assert_symmetric_user_gaps(page: Page, label: str) -> None:
    """Every user bubble with an assistant on both sides has equal gaps."""
    gaps = page.evaluate(_VISIBLE_GAPS_JS)
    checked = 0
    for i in range(len(gaps) - 1):
        above, below = gaps[i], gaps[i + 1]
        if not (above["from"] == "assistant" and above["to"] == "user"):
            continue
        if not (below["from"] == "user" and below["to"] == "assistant"):
            continue
        checked += 1
        assert above["gap"] == below["gap"], (
            f"{label}: uneven whitespace around a user message — "
            f"{above['gap']}px above vs {below['gap']}px below (gaps: {gaps})"
        )
    assert checked > 0, f"{label}: no assistant→user→assistant triple found ({gaps})"


def test_visible_gap_above_and_below_user_messages_is_equal(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="How do I list the workspace files?",
        reply="Use the Files tab in the right rail.",
        response_id="resp_gap_symmetry_1",
    )
    seed_committed_turn(
        session_id,
        prompt="And how do I open one of them?",
        reply="Click the file row; it opens in the viewer.",
        response_id="resp_gap_symmetry_2",
    )

    # History-hydrated: the seeded second user message sits between two
    # assistant replies.
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_BUBBLE)).to_have_count(4, timeout=30_000)
    _assert_symmetric_user_gaps(page, "history-hydrated")

    # Live-sent: send through the composer and let the mock-LLM turn reply.
    # The symmetry check only needs an assistant bubble on each side of the
    # sent message, so the reply's exact body doesn't matter.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Thanks, noted.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(f'{_BUBBLE}[data-role="user"]')).to_have_count(3, timeout=30_000)
    expect(page.locator(_BUBBLE)).to_have_count(6, timeout=120_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=120_000)
    _assert_symmetric_user_gaps(page, "after live send")

    # Reload: the formerly-live turn now renders from history too.
    page.reload()
    expect(page.locator(_BUBBLE)).to_have_count(6, timeout=30_000)
    _assert_symmetric_user_gaps(page, "after reload")
