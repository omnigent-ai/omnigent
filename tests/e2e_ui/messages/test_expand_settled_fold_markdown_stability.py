"""Verify that expanding settled rich markdown does not shift its layout."""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import Page, expect

_FOLD = '[data-testid="turn-worked-fold"]'

# Maximum drift after the first open frame; the regression shifted about 250px.
_MAX_POST_EXPAND_SHIFT_PX = 24

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

# Measuring relative to the trigger cancels scrolling within the shared container.
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
    """Publish a native-forwarder session status event."""
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
    """Mirror one native conversation item onto the session."""
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
    """Seed rich narration that folds behind a settled turn's Worked row."""
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
    """Expanding a settled turn must not jolt its markdown."""
    base_url, session_id = seeded_session
    _seed_settled_markdown_turn(base_url, session_id)

    # Navigate after settlement so the history fold mounts closed.
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

    fold = page.locator(_FOLD)
    expect(fold.first).to_be_visible(timeout=20_000)
    expect(page.get_by_text("All done - the answer is 42.")).to_be_visible()

    # Allow hidden diagram pre-rendering to finish on slow CI.
    page.wait_for_timeout(3_000)

    # Install before the click to capture the first open frame.
    page.evaluate(_INSTALL_SAMPLER)
    fold.locator('[data-slot="collapsible-trigger"]').first.click()

    page.wait_for_function("() => window.__probe && window.__probe.done", timeout=30_000)
    frames = page.evaluate("() => window.__probe.frames")

    open_frames = [f for f in frames if f["state"] == "open" and f["markerRelY"] is not None]
    assert open_frames, "fold never opened with the marker paragraph rendered"

    # Prevent a broken Mermaid pipeline from passing vacuously.
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
