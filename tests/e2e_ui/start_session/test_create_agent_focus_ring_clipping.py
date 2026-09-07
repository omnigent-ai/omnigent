"""E2E: focus ring is not chopped in the "Create custom agent" dialog.

Covers the user journey of opening the create-agent dialog from the
new-session landing page and focusing its form fields. Each field paints a
focus highlight (a ring drawn outside the input's border box). The dialog
body is a scroll container whose horizontal edges coincide exactly with the
full-width fields, so a ring drawn outside the field gets clipped at the
container's left/right edges — the highlight border appears chopped.

The test focuses each text field and measures, in the live page, whether the
painted focus ring fits inside its nearest clipping ancestor horizontally.
Vertical clipping at scroll extremes is inherent to any scroll container and
is not asserted; the horizontal chop is visible at every scroll position and
is the defect.

Uses the same route-stubbing approach as ``test_create_custom_agent.py``:
hosts/agents are faked so the test doesn't need a real host.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections.abc import Coroutine
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# Stubbed host the composer auto-selects.
_HOST_ID = "host_e2e"

# Text fields of the create-agent form that show a focus ring when focused.
_FOCUSABLE_FIELDS = [
    "create-agent-name",
    "create-agent-description",
    "create-agent-model",
    "create-agent-instructions",
]

# In-page measurement of the focused element's painted focus ring vs. the
# clip box of its nearest clipping ancestor. Returns which horizontal sides
# of the ring are chopped off (empty list = fully visible).
_MEASURE_RING_CLIP_JS = """
(testid) => {
  const el = document.querySelector(`[data-testid="${testid}"]`);
  if (!el) return { error: `no element for testid ${testid}` };
  if (document.activeElement !== el) return { error: `element ${testid} is not focused` };

  // Painted extent (px) of the focus highlight beyond the border box: the
  // largest outward reach among the element's non-inset box shadows.
  const shadow = getComputedStyle(el).boxShadow || "none";
  let extent = 0;
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

  const rect = el.getBoundingClientRect();
  const ring = {
    left: rect.left - extent,
    right: rect.right + extent,
    top: rect.top - extent,
    bottom: rect.bottom + extent,
  };

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
  const cs = getComputedStyle(clipper);
  const clipMargin = parseFloat(cs.overflowClipMargin) || 0;
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
    clipper: `${clipper.tagName.toLowerCase()}.${cs.overflowY}/${cs.overflowX}`,
  };
}
"""


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* in a dedicated thread with its own event loop."""
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _agents_body() -> str:
    """Single Claude Code agent for the stub."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": None,
                    "skills": [],
                }
            ]
        }
    )


def _hosts_body() -> str:
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": "e2e-host",
                    "owner": "e2e",
                    "status": "online",
                }
            ]
        }
    )


async def _register_routes(page) -> None:
    """Stub hosts/agents and neutralize agent discovery.

    With no custom agents discovered, "Create custom agent" stays a top-level
    row in the agent picker (it folds into a submenu once custom agents
    exist), so the test can click it directly.
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_hosts_body())

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


async def _seed_workspace(page) -> None:
    """Seed a recent workspace so the landing composer is fully enabled."""
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


def test_create_agent_focus_ring_not_clipped(live_server: str) -> None:
    """Focusing each create-agent form field paints an unclipped focus ring."""
    _run_in_fresh_loop(_drive_focus_ring(live_server))


async def _drive_focus_ring(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Fixed viewport for deterministic geometry. When a recording is
        # requested, record at full viewport size so the (3px) focus ring is
        # crisp in the footage instead of downscaled away.
        page_kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 720}}
        record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
        if record_dir:
            page_kwargs["record_video_dir"] = record_dir
            page_kwargs["record_video_size"] = {"width": 1280, "height": 720}
        page = await browser.new_page(**page_kwargs)
        try:
            await _register_routes(page)
            await _seed_workspace(page)

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Open the agent picker and click "Create custom agent".
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-create-agent").click()

            dialog = page.get_by_test_id("create-agent-dialog")
            await expect(dialog).to_be_visible(timeout=5_000)

            # Focus each text field the way a user does and check the painted
            # focus highlight is not chopped at the dialog body's edges.
            failures: list[str] = []
            for testid in _FOCUSABLE_FIELDS:
                field = page.get_by_test_id(testid)
                await field.click()
                await expect(field).to_be_focused()
                # Let the focus transition settle so the painted ring (and any
                # recording of this journey) shows the steady focused state.
                await page.wait_for_timeout(400)
                result = await page.evaluate(_MEASURE_RING_CLIP_JS, testid)
                assert "error" not in result, result
                # Sanity: the field must actually paint an outward highlight,
                # otherwise the clipping check would be vacuous.
                assert result["extent"] > 0, (
                    f"{testid}: expected an outward focus ring, got none ({result})"
                )
                if result["chopped"]:
                    failures.append(
                        f"{testid}: focus ring chopped on {result['chopped']} "
                        f"(ring x-span [{result['ringLeft']:.1f}, {result['ringRight']:.1f}] "
                        f"vs clip x-span [{result['clipLeft']:.1f}, {result['clipRight']:.1f}] "
                        f"of {result['clipper']})"
                    )

            # Hold the final focused state briefly so a recording of the
            # journey ends on the observable outcome.
            await page.wait_for_timeout(1_200)

            assert not failures, (
                "Focus highlight is chopped in the create-agent form:\n" + "\n".join(failures)
            )
        finally:
            # Close the page before the browser so a recording (when
            # OMNIGENT_E2E_RECORD_DIR is set) is flushed to disk even when the
            # journey's assertions fail.
            await page.close()
            await browser.close()
