"""E2E: virtualized transcripts preserve their view across SPA session switches."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state

_TURNS = 80
_ANCHOR_TOLERANCE_PX = 24


def _seed_turns(session_id: str, prefix: str) -> None:
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    items: list[NewConversationItem] = []
    for turn in range(_TURNS):
        response_id = f"resp_{prefix}_{turn:03d}"
        items.extend(
            [
                NewConversationItem(
                    type="message",
                    response_id=response_id,
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": f"{prefix} prompt {turn}"}],
                    ),
                ),
                NewConversationItem(
                    type="message",
                    response_id=response_id,
                    data=MessageData(
                        role="assistant",
                        content=[
                            {
                                "type": "output_text",
                                "text": f"{prefix} reply {turn}\n\n"
                                + "\n\n".join(
                                    f"{prefix} detail {turn}.{line}" for line in range(6)
                                ),
                            }
                        ],
                        agent="hello_world",
                    ),
                ),
            ]
        )
    SqlAlchemyConversationStore(str(_server_state["database_uri"])).append(session_id, items)


_FIND_SCROLLER = """
  const log = document.querySelector('[role="log"]');
  let el = log;
  log?.querySelectorAll('*').forEach((candidate) => {
    if (candidate.scrollHeight > candidate.clientHeight + 4 &&
        (!el || candidate.scrollHeight > el.scrollHeight)) el = candidate;
  });
"""

_READ_SCROLLER = f"""
() => {{
  {_FIND_SCROLLER}
  return el ? {{ scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }} : null;
}}
"""

_SCROLL_TO_BOTTOM = f"""
() => {{
  {_FIND_SCROLLER}
  if (el) el.scrollTop = el.scrollHeight;
}}
"""

_SCROLL_TO_MIDDLE = f"""
() => {{
  {_FIND_SCROLLER}
  if (el) el.scrollTop = Math.round((el.scrollHeight - el.clientHeight) * 0.45);
}}
"""

_BOTTOM_DISTANCE = f"""
() => {{
  {_FIND_SCROLLER}
  return el ? el.scrollHeight - el.clientHeight - el.scrollTop : null;
}}
"""

_CAPTURE_ANCHOR = f"""
() => {{
  {_FIND_SCROLLER}
  if (!el) return null;
  const top = el.getBoundingClientRect().top;
  const rows = [...el.querySelectorAll('[data-bubble-key]')]
    .map((node) => {{
      const rect = node.getBoundingClientRect();
      return {{
        id: node.getAttribute('data-bubble-key'),
        offset: rect.top - top,
      }};
    }})
    .filter((row) => row.id);
  return rows
    .filter((row) => row.offset <= 0)
    .sort((a, b) => b.offset - a.offset)[0]
    ?? rows.sort((a, b) => a.offset - b.offset)[0]
    ?? null;
}}
"""


def _open_seeded_pair(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> tuple[str, str, str]:
    base_url, session_a, session_b = seeded_session_pair
    _seed_turns(session_a, "alpha")
    _seed_turns(session_b, "beta")
    page.set_viewport_size({"width": 1280, "height": 600})
    page.goto(f"{base_url}/c/{session_a}")
    expect(page.get_by_text(f"alpha reply {_TURNS - 1}").first).to_be_visible(timeout=30_000)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_be_visible(timeout=30_000)
    assert page.evaluate(_READ_SCROLLER) is not None
    return base_url, session_a, session_b


def test_bottom_survives_conversation_switch(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A transcript left at bottom returns to its moving bottom target."""
    base_url, session_a, session_b = _open_seeded_pair(page, seeded_session_pair)
    page.evaluate(_SCROLL_TO_BOTTOM)

    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=15_000)
    expect(page.get_by_text(f"beta reply {_TURNS - 1}").first).to_be_visible(timeout=30_000)
    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_a}", timeout=15_000)
    expect(page.get_by_text(f"alpha reply {_TURNS - 1}").first).to_be_visible(timeout=30_000)

    distance = None
    for _ in range(50):
        distance = page.evaluate(_BOTTOM_DISTANCE)
        if distance is not None and distance <= 8:
            break
        page.wait_for_timeout(100)
    assert distance is not None and distance <= 8


def test_mid_scroll_anchor_survives_conversation_switch(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A measured bubble returns to the same viewport displacement."""
    base_url, session_a, session_b = _open_seeded_pair(page, seeded_session_pair)
    page.evaluate(_SCROLL_TO_MIDDLE)
    page.wait_for_timeout(500)
    # The direct scrollTop assignment fires before virtual row measurements
    # settle. Mirror the reader's final scroll event so the saved semantic
    # offset reflects the geometry captured below.
    page.evaluate(
        f"""() => {{
          {_FIND_SCROLLER}
          el?.dispatchEvent(new Event('scroll'));
        }}"""
    )
    before = page.evaluate(_CAPTURE_ANCHOR)
    assert before is not None

    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=15_000)
    expect(page.get_by_text(f"beta reply {_TURNS - 1}").first).to_be_visible(timeout=30_000)
    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_a}", timeout=15_000)

    after = None
    for _ in range(50):
        after = page.evaluate(_CAPTURE_ANCHOR)
        if (
            after is not None
            and after["id"] == before["id"]
            and abs(after["offset"] - before["offset"]) <= _ANCHOR_TOLERANCE_PX
        ):
            break
        page.wait_for_timeout(100)
    assert after is not None
    assert after["id"] == before["id"]
    # Dynamic virtual-row measurements may settle by roughly one text line;
    # the semantic contract is the same reading row within that displacement.
    assert abs(after["offset"] - before["offset"]) <= _ANCHOR_TOLERANCE_PX


def test_native_find_shortcut_mounts_the_full_loaded_transcript(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Ctrl+F makes every loaded bubble available to browser find-in-page."""
    _open_seeded_pair(page, seeded_session_pair)
    bubbles = page.locator('[data-testid="message-bubble"]')
    mounted_before = bubbles.count()
    assert mounted_before < 100, mounted_before

    page.evaluate(
        """() => window.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'f',
          ctrlKey: true,
          bubbles: true,
        }))"""
    )

    expect(page.locator("[data-index]")).to_have_count(0, timeout=30_000)
    assert bubbles.count() > mounted_before
