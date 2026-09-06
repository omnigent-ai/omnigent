"""E2E: focus highlight is not chopped in the "Add agent" dialog.

Companion to ``test_create_agent_focus_ring_clipping.py`` for the second
dialog the report names: the Agents-rail "Add agent" picker. Its body is the
same kind of ``overflow-y-auto`` scroll container whose horizontal edges
coincide exactly with the full-width agent cards, so the focus highlight
painted outside a keyboard-focused card (the global ``:focus-visible``
outline) is chopped at the container's left/right edges.

The test opens the dialog from a real session's Agents rail, keyboard-focuses
the first agent card the way a user tabbing through the dialog does, and
measures in the live page whether the painted focus highlight (box-shadow
ring or outline) fits inside its nearest clipping ancestor horizontally.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_ADD_AGENT_BUTTON = '[data-testid="add-agent-button"]'
_ADD_AGENT_DIALOG = '[data-testid="add-agent-dialog"]'

# In-page measurement of the focused element's painted focus highlight vs.
# the clip box of its nearest clipping ancestor. The highlight is whichever
# paints farther out: a non-inset box-shadow ring or the focus outline
# (outline-width + outline-offset). Returns which horizontal sides of the
# highlight are chopped off (empty list = fully visible).
_MEASURE_FOCUS_CLIP_JS = """
() => {
  const el = document.activeElement;
  if (!el || el === document.body) return { error: "nothing focused" };
  const cs = getComputedStyle(el);

  // Painted extent (px) of the focus highlight beyond the border box.
  let extent = 0;
  const shadow = cs.boxShadow || "none";
  if (shadow !== "none") {
    // Split the shadow list on top-level commas (colors contain commas too).
    const parts = [];
    let depth = 0;
    let cur = "";
    for (const ch of shadow) {
      if (ch === "(") depth++;
      if (ch === ")") depth--;
      if (ch === "," && depth === 0) {
        parts.push(cur);
        cur = "";
      } else {
        cur += ch;
      }
    }
    if (cur.trim()) parts.push(cur);
    for (const part of parts) {
      if (part.includes("inset")) continue;
      const nums = (part.match(/-?\\d+(?:\\.\\d+)?px/g) || []).map(parseFloat);
      const [ox = 0, oy = 0, blur = 0, spread = 0] = nums;
      extent = Math.max(extent, spread + blur + Math.max(Math.abs(ox), Math.abs(oy)));
    }
  }
  if (cs.outlineStyle !== "none") {
    const width = parseFloat(cs.outlineWidth) || 0;
    const offset = parseFloat(cs.outlineOffset) || 0;
    if (width > 0) extent = Math.max(extent, width + offset);
  }

  const rect = el.getBoundingClientRect();
  const ring = { left: rect.left - extent, right: rect.right + extent };

  // Nearest ancestor that clips its overflow.
  let node = el.parentElement;
  let clipper = null;
  while (node && node !== document.body) {
    const s = getComputedStyle(node);
    if (s.overflowX !== "visible" || s.overflowY !== "visible") {
      clipper = node;
      break;
    }
    node = node.parentElement;
  }
  if (!clipper) return { extent, chopped: [] };

  const cRect = clipper.getBoundingClientRect();
  const ccs = getComputedStyle(clipper);
  const clipMargin = parseFloat(ccs.overflowClipMargin) || 0;
  // clientLeft/clientWidth give the padding box (the paint clip boundary),
  // excluding borders and any classic scrollbar.
  const clip = {
    left: cRect.left + clipper.clientLeft - clipMargin,
    right: cRect.left + clipper.clientLeft + clipper.clientWidth + clipMargin,
  };

  const chopped = [];
  if (ring.left < clip.left - 0.01) chopped.push("left");
  if (ring.right > clip.right + 0.01) chopped.push("right");
  return {
    extent,
    chopped,
    ringLeft: ring.left,
    ringRight: ring.right,
    clipLeft: clip.left,
    clipRight: clip.right,
    clipper: `${clipper.tagName.toLowerCase()}.${ccs.overflowY}/${ccs.overflowX}`,
  };
}
"""


def test_add_agent_focus_highlight_not_clipped(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Keyboard-focusing an agent card paints an unclipped focus highlight."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # The Add-agent button lives in the Agents rail panel, so open the rail
    # and select that tab to mount the panel (and its dialog).
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    # The trigger is a visually-hidden hook; dispatch a DOM click so
    # visibility doesn't gate the test the way ``.click()`` would.
    add_button = page.locator(_ADD_AGENT_BUTTON)
    expect(add_button).to_be_attached(timeout=30_000)
    add_button.dispatch_event("click")

    dialog = page.locator(_ADD_AGENT_DIALOG)
    expect(dialog).to_be_visible(timeout=15_000)

    # The picker populates asynchronously (GET /v1/agents); until the query
    # resolves the dialog shows its empty-state and there is no card to Tab
    # to, so wait for the first card to attach before driving the keyboard.
    first_card = dialog.locator('[data-testid^="agent-card-"]').first
    expect(first_card).to_be_attached(timeout=15_000)

    # Tab from the dialog's initial focus until an agent card is the active
    # element — keyboard focus is what paints the outward focus-visible
    # highlight on the full-width cards.
    focused_testid: str | None = None
    for _ in range(8):
        page.keyboard.press("Tab")
        focused_testid = page.evaluate(
            "() => document.activeElement && document.activeElement.getAttribute('data-testid')"
        )
        if focused_testid and focused_testid.startswith("agent-card-"):
            break
    assert focused_testid and focused_testid.startswith("agent-card-"), (
        f"never reached an agent card via Tab; last focused: {focused_testid!r}"
    )

    # Let the focus transition settle so the painted highlight (and any
    # recording of this journey) shows the steady focused state.
    page.wait_for_timeout(400)
    result = page.evaluate(_MEASURE_FOCUS_CLIP_JS)
    assert "error" not in result, result
    # Sanity: the card must actually paint an outward highlight, otherwise
    # the clipping check would be vacuous.
    assert result["extent"] > 0, (
        f"expected an outward focus highlight on {focused_testid}, got none ({result})"
    )

    # Hold the focused state briefly so a recording of the journey ends on
    # the observable outcome.
    page.wait_for_timeout(1_200)

    assert not result["chopped"], (
        f"{focused_testid}: focus highlight chopped on {result['chopped']} "
        f"(highlight x-span [{result['ringLeft']:.1f}, {result['ringRight']:.1f}] "
        f"vs clip x-span [{result['clipLeft']:.1f}, {result['clipRight']:.1f}] "
        f"of {result['clipper']})"
    )
