"""UI regression: expanding a settled message must not re-render its markdown.

Journey: a turn whose process trace carries rich markdown (fenced code, a
mermaid diagram, a table) settles and folds behind the "Worked" row. The user
reloads the page (history path: the fold mounts closed) and clicks the row to
expand the trace.

Expected: the trace opens and holds still — the markdown inside was already
rendered once, so re-opening it must not visibly re-render.

Actual (the bug): the collapsible unmounts its children while closed, so every
expand rebuilds the whole markdown tree from scratch. Code blocks flash
unhighlighted for a frame, and the mermaid diagram re-renders asynchronously a
few hundred ms after the expand settles — first collapsing to its placeholder,
then popping in at full size — which jolts every element below it by hundreds
of pixels. The jank recurs on every expand (warm caches included).

The test seeds the turn deterministically through the external-item events a
native forwarder uses (no LLM round-trip), installs a per-animation-frame
sampler that tracks a marker paragraph's offset relative to the fold row, then
expands the fold and asserts the offset never shifts after the first open
frame. Today the mermaid re-render moves the marker ~250px, so this fails; it
passes once an expand no longer re-renders settled markdown (or reserves the
diagram's space).
"""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import Page, expect

_FOLD = '[data-testid="turn-worked-fold"]'

# How far (px) content below the diagram may drift after the expand's first
# open frame. The expand height animation clips the content without moving
# inner layout, so a stable renderer stays at ~0; the bug shifts it ~250px.
_MAX_POST_EXPAND_SHIFT_PX = 24

# The marker paragraph the sampler tracks, placed below the mermaid diagram.
_MARKER = "MARKER_BELOW_DIAGRAM"

_NARRATION = """Let me sketch the flow first.

```mermaid
graph TD
  A[Start] --> B{Check input}
  B -->|valid| C[Process]
  B -->|invalid| D[Reject]
  C --> E[Emit result]
  D --> E
```

```python
def helper(value: int) -> int:
    result = value * 3 + compute(value)
    return result
```

| step | action | notes |
| ---- | ------ | ----- |
| 1 | read the file | fast |
| 2 | run the tests | slow |

MARKER_BELOW_DIAGRAM paragraph that should hold still after the expand.
"""

# Per-animation-frame sampler. Measures the marker paragraph's offset
# RELATIVE to the fold's trigger row (both live in the same scroller, so
# scrolling cancels out and only real layout shifts between them register).
# Runs until the diagram has been rendered and stable for ~1s, or a hard cap.
_INSTALL_SAMPLER = """
() => {
  window.__probe = { frames: [], done: false };
  const cap = 60 * 30; // ~30s of frames, hard stop
  let stableFrames = 0;
  const tick = () => {
    const fold = document.querySelector('[data-testid="turn-worked-fold"]');
    const content = fold ? fold.querySelector('[data-slot="collapsible-content"]') : null;
    const trigger = fold ? fold.querySelector('[data-slot="collapsible-trigger"]') : null;
    let markerRelY = null;
    let diagramH = -1;
    if (content && trigger) {
      const triggerTop = trigger.getBoundingClientRect().top;
      for (const p of content.querySelectorAll('p')) {
        if ((p.textContent || '').includes('MARKER_BELOW_DIAGRAM')) {
          markerRelY = p.getBoundingClientRect().top - triggerTop;
          break;
        }
      }
      for (const svg of content.querySelectorAll('svg')) {
        const h = svg.getBoundingClientRect().height;
        if (h > diagramH) diagramH = h;
      }
    }
    window.__probe.frames.push({
      t: performance.now(),
      state: content ? content.getAttribute('data-state') : 'none',
      markerRelY,
      diagramH,
    });
    // Done once a real diagram (not an inline icon) has been on screen and
    // the layout has been stable for ~1s of frames.
    const f = window.__probe.frames;
    const last = f[f.length - 1];
    const prev = f.length > 1 ? f[f.length - 2] : null;
    const stableStep =
      prev !== null &&
      last.markerRelY !== null &&
      prev.markerRelY !== null &&
      Math.abs(last.markerRelY - prev.markerRelY) < 0.5;
    stableFrames = last.diagramH > 50 && stableStep ? stableFrames + 1 : 0;
    if (stableFrames >= 60 || f.length >= cap) {
      window.__probe.done = true;
      return;
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
"""


def _publish_status(
    base_url: str, session_id: str, status: str, response_id: str | None = None
) -> None:
    """Publish a session status edge through the native-forwarder event route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: ``"running"`` or ``"idle"``.
    :param response_id: Turn id the edge belongs to.
    :returns: None.
    """
    data: dict[str, object] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def _seed_item(
    base_url: str,
    session_id: str,
    item_type: str,
    item_data: dict,
    response_id: str,
) -> None:
    """Mirror one native conversation item onto the session.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param item_type: ``"message"`` / ``"function_call"`` / ``"function_call_output"``.
    :param item_data: The item payload a native forwarder would emit.
    :param response_id: Turn id the item belongs to.
    :returns: None.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {"item_type": item_type, "item_data": item_data, "response_id": response_id},
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _seed_settled_markdown_turn(base_url: str, session_id: str) -> None:
    """Seed one settled turn whose process trace carries rich markdown.

    The narration (code + mermaid + table) lands BEFORE the tool step and the
    final answer, so once the turn settles it folds into the "Worked" row and
    the markdown lives inside the collapsed trace.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :returns: None.
    """
    thread = "resp_md_expand_jank"
    _seed_item(
        base_url,
        session_id,
        "message",
        {"role": "user", "content": [{"type": "input_text", "text": "Inspect the code."}]},
        thread,
    )
    _publish_status(base_url, session_id, "running", response_id=thread)
    _seed_item(
        base_url,
        session_id,
        "message",
        {
            "role": "assistant",
            "agent": "claude-native-ui",
            "content": [{"type": "output_text", "text": _NARRATION}],
        },
        thread,
    )
    _seed_item(
        base_url,
        session_id,
        "function_call",
        {
            "agent": "claude-native-ui",
            "name": "shell",
            "arguments": json.dumps({"command": "ls"}),
            "call_id": "call_md_expand_1",
        },
        thread,
    )
    _seed_item(
        base_url,
        session_id,
        "function_call_output",
        {"call_id": "call_md_expand_1", "output": "README.md\n"},
        thread,
    )
    _seed_item(
        base_url,
        session_id,
        "message",
        {
            "role": "assistant",
            "agent": "claude-native-ui",
            "content": [{"type": "output_text", "text": "All done - the answer is 42."}],
        },
        thread,
    )
    _publish_status(base_url, session_id, "idle", response_id=thread)


def test_expanding_settled_fold_does_not_rerender_markdown(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Expanding a settled turn's fold must not jolt the markdown inside it.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _seed_settled_markdown_turn(base_url, session_id)

    # History path: navigate AFTER the turn settled so the fold mounts closed
    # and its contents start unmounted (the state every reopened session is in).
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

    fold = page.locator(_FOLD)
    expect(fold.first).to_be_visible(timeout=20_000)
    # The final answer is outside the fold; the markdown is inside it.
    expect(page.get_by_text("All done - the answer is 42.")).to_be_visible()

    # Give the page the beat a real reader spends on the answer before
    # digging into the trace. This wait is not part of the assertion — the
    # buggy renderer fails identically with or without it (the jolt happens
    # on expand regardless) — it only de-flakes the fixed behavior on slow
    # CI, where the folded trace's hidden diagram pre-render can lag the
    # page load by a couple of seconds.
    page.wait_for_timeout(3_000)

    # Sampler first, then the user's click, so the very first open frame is
    # captured — the late diagram re-render lands a few hundred ms after it.
    page.evaluate(_INSTALL_SAMPLER)
    fold.locator('[data-slot="collapsible-trigger"]').first.click()

    page.wait_for_function("() => window.__probe && window.__probe.done", timeout=30_000)
    frames = page.evaluate("() => window.__probe.frames")

    open_frames = [f for f in frames if f["state"] == "open" and f["markerRelY"] is not None]
    assert open_frames, "fold never opened with the marker paragraph rendered"

    # Sanity: the diagram must actually render, otherwise a broken mermaid
    # pipeline would make the stability assertion pass vacuously.
    assert any(f["diagramH"] > 50 for f in frames), (
        "the mermaid diagram never rendered inside the expanded fold; "
        "the journey did not reach the state under test"
    )

    baseline = open_frames[0]["markerRelY"]
    worst = max(open_frames, key=lambda f: abs(f["markerRelY"] - baseline))
    shift = abs(worst["markerRelY"] - baseline)
    assert shift <= _MAX_POST_EXPAND_SHIFT_PX, (
        f"expanding the settled fold re-rendered its markdown: content below "
        f"the diagram shifted {shift:.0f}px after the first open frame "
        f"(baseline relY={baseline:.0f}, worst relY={worst['markerRelY']:.0f} "
        f"at t={worst['t']:.0f}ms) — the expand must not visibly re-render "
        f"already-settled markdown"
    )
