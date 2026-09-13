"""E2E: virtualized transcripts preserve their view across SPA session switches."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state

_TURNS = 80
_ANCHOR_TOLERANCE_PX = 24
_TARGET_PROMPT = "alpha prompt 60"


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
  const el = log?.firstElementChild;
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

_BOTTOM_DISTANCE = f"""
() => {{
  {_FIND_SCROLLER}
  return el ? el.scrollHeight - el.clientHeight - el.scrollTop : null;
}}
"""

_CAPTURE_TARGET = f"""
(node) => {{
  {_FIND_SCROLLER}
  if (!el) return null;
  return {{
    id: node.getAttribute('data-bubble-key'),
    offset: node.getBoundingClientRect().top - el.getBoundingClientRect().top,
  }};
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
    """A known user turn returns to the same viewport displacement."""
    base_url, session_a, session_b = _open_seeded_pair(page, seeded_session_pair)
    target = page.locator('[data-bubble-key^="user:"]', has_text=_TARGET_PROMPT)
    page.locator(".transcript-hide-native-scrollbar").hover()
    for _ in range(20):
        page.mouse.wheel(0, -1_000)
        page.wait_for_timeout(100)
        if target.is_visible():
            break
    expect(target).to_be_visible(timeout=30_000)
    target.evaluate("(node) => node.scrollIntoView({ block: 'start' })")
    page.wait_for_timeout(500)
    # TurnRail navigation and virtual measurements have settled. Mirror the
    # reader's final scroll event so this exact turn's offset is persisted.
    page.evaluate(
        f"""() => {{
          {_FIND_SCROLLER}
          el?.dispatchEvent(new Event('scroll'));
        }}"""
    )
    before = target.evaluate(_CAPTURE_TARGET)
    assert before is not None
    assert page.evaluate(_BOTTOM_DISTANCE) > 100

    page.locator(f'a[href="/c/{session_b}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=15_000)
    expect(page.get_by_text(f"beta reply {_TURNS - 1}").first).to_be_visible(timeout=30_000)
    page.locator(f'a[href="/c/{session_a}"]').click()
    expect(page).to_have_url(f"{base_url}/c/{session_a}", timeout=15_000)

    after = None
    for _ in range(50):
        after = target.evaluate(_CAPTURE_TARGET) if target.count() else None
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
