"""E2E: an opened session paints at its end in every frame that shows transcript rows."""

from __future__ import annotations

import random

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_items

_TURNS = 70
_VIEWPORT = {"width": 1280, "height": 800}
# A frame with rows on screen may sit this far above the end.
_SLACK_PX = 2
_WORDS = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
]

# Records (time, scrollTop, distance from the end, rows on screen) once per frame.
# The observer is created in the frame's rAF, so its callback runs after the frame's
# layout and after the app's observers: it sees what the frame is about to paint.
_SAMPLE_FRAMES = """
(() => {
  if (window.__openFrames) return;
  window.__openFrames = [];
  const findScroller = () => {
    const log = document.querySelector('[role="log"]');
    if (!log) return null;
    let best = null;
    for (const el of [log, ...log.querySelectorAll('div')]) {
      const taller = !best || el.scrollHeight > best.scrollHeight;
      if (el.scrollHeight > el.clientHeight + 4 && taller) best = el;
    }
    return best;
  };
  let scroller = null;
  const tick = () => {
    if (!scroller || !scroller.isConnected) scroller = findScroller();
    const sc = scroller;
    if (sc) {
      const observer = new ResizeObserver(() => {
        observer.disconnect();
        const view = sc.getBoundingClientRect();
        let rows = 0;
        for (const row of sc.querySelectorAll('[data-bubble-key]')) {
          const box = row.getBoundingClientRect();
          if (box.bottom > view.top && box.top < view.bottom) rows += 1;
        }
        const distance = sc.scrollHeight - sc.clientHeight - sc.scrollTop;
        window.__openFrames.push([performance.now(), sc.scrollTop, distance, rows]);
      });
      observer.observe(sc);
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})();
"""


def _reply(kind: str, token: str, rng: random.Random) -> str:
    def words(n: int) -> str:
        return " ".join(rng.choice(_WORDS) for _ in range(n))

    if kind == "short":
        return f"Done. {token}"
    if kind == "code":
        body = "\n".join(
            f"    value_{i} = compute({i}, factor={i * 3})  # step {i}"
            for i in range(rng.randint(20, 45))
        )
        return (
            f"{words(25)}.\n\n```python\ndef run():\n{body}\n    return value_0\n```\n\n"
            f"Done {token}"
        )
    if kind == "table":
        rows = "\n".join(
            f"| row {i} | {rng.randint(0, 9999)} | {words(9)} |"
            for i in range(rng.randint(12, 28))
        )
        return (
            f"{words(20)}:\n\n| name | value | description |\n|---|---|---|\n{rows}\n\nEnd {token}"
        )
    paragraphs = "\n\n".join(f"{words(rng.randint(30, 60))}." for _ in range(rng.randint(5, 9)))
    return f"## {words(4)}\n\n{paragraphs}\n\n- {words(6)}\n- {words(8)}\n\n{token}"


def _seed_session(session_id: str, name: str) -> str:
    """Seed ``_TURNS`` mixed exchanges; returns the newest reply's text."""
    from omnigent.entities import MessageData, NewConversationItem

    rng = random.Random(name)
    items: list[NewConversationItem] = []
    newest = f"{name} newest reply"
    for turn in range(_TURNS):
        response_id = f"resp_{name}_{turn:03d}"
        kind = rng.choice(["short", "prose", "code", "table"])
        token = newest if turn == _TURNS - 1 else f"{name} turn {turn}"
        if turn == _TURNS - 1:
            kind = "prose"
        items += [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": f"{name} prompt {turn} ({kind})"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": _reply(kind, token, rng)}],
                    agent="hello_world",
                ),
            ),
        ]
    seed_committed_items(session_id, items)
    return newest


def _frame_count(page: Page) -> int:
    return page.evaluate("() => window.__openFrames.length")


def _frames_until_settled(page: Page, start: int) -> list[list[float]]:
    """Frames recorded from ``start`` until the transcript holds still for 20 polls."""
    frames: list[list[float]] = []
    last = None
    stable = 0
    for _ in range(80):
        frames = page.evaluate("(start) => window.__openFrames.slice(start)", start)
        current = frames[-1][1:3] if frames else None
        if frames and frames[-1][3] > 0 and current == last:
            stable += 1
            if stable >= 20:
                break
        else:
            stable = 0
        last = current
        page.wait_for_timeout(100)
    # A transcript still moving at the cutoff would be judged on a partial sample.
    assert stable >= 20, f"transcript never settled; last frame {frames[-1] if frames else None}"
    return frames


def test_opening_a_session_paints_at_its_bottom(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Every open shows the transcript at its end from the first frame with rows.

    Opens: a cold load, then two switches in the SPA (away and back), then a cold
    load of the other session. Each open is sampled from navigation until it settles.
    """
    base_url, session_a, session_b = seeded_session_pair
    newest_a = _seed_session(session_a, "alpha")
    newest_b = _seed_session(session_b, "beta")
    page.set_viewport_size(_VIEWPORT)
    page.add_init_script(_SAMPLE_FRAMES)

    opens: dict[str, list[list[float]]] = {}
    page.goto(f"{base_url}/c/{session_a}")
    expect(page.get_by_text(newest_a).first).to_be_visible(timeout=30_000)
    opens["cold load of alpha"] = _frames_until_settled(page, 0)

    for label, target, newest in (
        ("switch to beta", session_b, newest_b),
        ("switch back to alpha", session_a, newest_a),
    ):
        start = _frame_count(page)
        page.locator(f'a[href="/c/{target}"]').click()
        expect(page).to_have_url(f"{base_url}/c/{target}", timeout=15_000)
        expect(page.get_by_text(newest).first).to_be_visible(timeout=30_000)
        opens[label] = _frames_until_settled(page, start)

    page.goto(f"{base_url}/c/{session_b}")
    expect(page.get_by_text(newest_b).first).to_be_visible(timeout=30_000)
    opens["cold load of beta"] = _frames_until_settled(page, 0)

    unpinned = {
        label: [frame for frame in frames if frame[3] > 0 and frame[2] > _SLACK_PX]
        for label, frames in opens.items()
    }
    assert all(any(frame[3] > 0 for frame in frames) for frames in opens.values())
    assert {label: frames for label, frames in unpinned.items() if frames} == {}, {
        label: frames[:3] for label, frames in unpinned.items() if frames
    }
