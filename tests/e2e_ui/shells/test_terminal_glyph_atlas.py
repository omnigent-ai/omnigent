"""E2E: the Terminal view must keep drawing static rows correctly while the
pane churns through thousands of unique CJK glyphs.

A shell opened in the workspace rail draws two styled status rows once, then
keeps redrawing its body region with unique CJK glyphs in cycling colors and
weights (a long Japanese-heavy TUI session, compressed). The pane bytes for
the status rows never change, so their pixels must stay identical for the
whole run.

The WebGL glyph atlas repacks (merges/deletes) pages once it hits its page
cap, ``min(32, MAX_TEXTURE_IMAGE_UNITS)``. A repack moves pages between
texture slots; if the renderer fails to re-upload a swapped slot, the forced
full repaint draws already-correct cells — including the untouched status
rows — with the wrong glyphs. The test clamps ``MAX_TEXTURE_IMAGE_UNITS`` to
8 (a real low-end-GPU value) so repacks happen every few waves instead of
after minutes of churn.
"""

from __future__ import annotations

import io
import re
import time
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_REPO_ROOT = Path(__file__).resolve().parents[3]
_XTERM_LIB = _REPO_ROOT / "web" / "node_modules" / "@xterm" / "xterm"
_WEBGL_LIB = _REPO_ROOT / "web" / "node_modules" / "@xterm" / "addon-webgl"

# Presents a GPU with 8 texture units to the page, so the glyph atlas page cap
# is 8 and CJK churn forces page repacks within seconds.
_CLAMP_TEXTURE_UNITS_SCRIPT = """
(() => {
  const MAX_TEXTURE_IMAGE_UNITS = 0x8872;
  const orig = WebGL2RenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getParameter = function (pname) {
    const value = orig.call(this, pname);
    if (pname === MAX_TEXTURE_IMAGE_UNITS) {
      return Math.min(8, value);
    }
    return value;
  };
})();
"""

# Runs inside the rail shell. Draws two static styled status rows, then keeps
# redrawing the body region (rows 4+) forever with unique CJK glyphs in cycling
# colors and weights — cursor-addressed only, autowrap off, cursor hidden, so
# nothing scrolls or repaints the status rows on its own. The cycling colors
# and ever-fresh codepoints churn the glyph atlas so it repacks pages
# repeatedly; each repack forces a full-terminal repaint, which is when the
# static rows can flip to the wrong glyph. Writes a heartbeat file so the test
# can confirm the flood is running independent of what the browser draws.
_FLOOD_SCRIPT = r"""
import os
import sys
import time

hb = __file__ + ".hb"


def beat(text):
    with open(hb, "w", encoding="utf-8") as f:
        f.write(text)


try:
    sys.stdout.reconfigure(encoding="utf-8")
    cols, rows = os.get_terminal_size()
except Exception as exc:  # size probe / non-tty
    beat("ERROR:" + repr(exc))
    raise

w = sys.stdout.write
CSI = "\x1b["

w(CSI + "2J" + CSI + "H" + CSI + "?25l" + CSI + "?7l")
probe1 = (
    "Build · Claude Fable 5.1 (thinking/xhigh)  "
    "JSON parsing failed: Text:  227 231 tokens トークン数"
)
probe2 = (
    "How remove the facilitator-only open-items section from the participant "
    "handbook 進捗状況 差分表示"
)
w(CSI + "1;1H" + CSI + "1;38;5;39m" + probe1[: cols - 2])
w(CSI + "2;1H" + CSI + "0;2;38;5;250m" + probe2[: cols - 2] + CSI + "0m")
sys.stdout.flush()
beat(f"PROBE_DRAWN {cols}x{rows}")
time.sleep(2.5)

cp = 0
wave = 0
while True:
    color = 22 + (wave * 7) % 200
    weight = "1" if wave % 3 == 0 else ("2" if wave % 3 == 1 else "0")
    parts = []
    for row in range(4, rows):
        parts.append(f"{CSI}{row};1H{CSI}0;{weight};38;5;{color}m")
        budget = cols - 2
        cells = []
        while budget >= 2:
            cells.append(chr(0x4E00 + cp % 20000))
            cp += 1
            budget -= 2
            if cp % 13 == 0 and budget >= 1:
                cells.append(chr(0x21 + cp % 90))
                budget -= 1
        parts.append("".join(cells))
    parts.append(f"{CSI}{rows - 1};1H")
    w("".join(parts))
    sys.stdout.flush()
    wave += 1
    beat(str(wave))
    time.sleep(0.05)
"""


def _open_new_shell(page: Page) -> None:
    """Create a shell via the tab strip's "+" → Shell menu (mirrors test_new_shell)."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def _rgb(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _diff_pixels(a: bytes, b: bytes) -> int:
    if a == b:
        return 0
    ia, ib = _rgb(a), _rgb(b)
    if ia.size != ib.size:
        return ia.size[0] * ia.size[1]
    ba, bb = ia.tobytes(), ib.tobytes()
    return sum(1 for i in range(0, len(ba), 3) if ba[i : i + 3] != bb[i : i + 3])


def _dominant_color(png: bytes) -> tuple[int, int, int]:
    img = _rgb(png)
    colors = img.getcolors(img.size[0] * img.size[1])
    assert colors is not None
    return max(colors)[1]


def _ink_pixels(png: bytes, bg: tuple[int, int, int]) -> int:
    """Count non-background pixels (drawn glyph ink) in the capture."""
    data = _rgb(png).tobytes()
    bg_bytes = bytes(bg)
    return sum(1 for i in range(0, len(data), 3) if data[i : i + 3] != bg_bytes)


def _read_heartbeat(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def test_static_rows_survive_cjk_glyph_churn(
    page: Page,
    terminal_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Static styled rows keep their exact pixels through heavy CJK churn."""
    base_url, session_id = terminal_session

    script_path = tmp_path / "cjk_flood.py"
    heartbeat = Path(str(script_path) + ".hb")
    script_path.write_text(_FLOOD_SCRIPT, encoding="utf-8")

    page.add_init_script(_CLAMP_TEXTURE_UNITS_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    _open_new_shell(page)

    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("button", name=re.compile(r"^Close zsh · u-"))).to_be_visible(
        timeout=60_000
    )
    term = rail.get_by_test_id("terminal-view").last
    expect(term).to_be_visible(timeout=60_000)
    expect(term).to_have_attribute("data-state", "connected", timeout=20_000)

    # The defect lives in the WebGL renderer; without a GPU context xterm
    # stays on the DOM renderer and this journey exercises nothing.
    canvas = term.locator(".xterm-screen canvas").first
    expect(canvas).to_be_visible(timeout=20_000)
    box = canvas.bounding_box()
    assert box is not None, "terminal canvas has no layout box"
    # The two status rows; the flood only ever touches rows 4+.
    probe_clip = {
        "x": box["x"],
        "y": box["y"],
        "width": box["width"],
        "height": 42.0,
    }

    blank = page.screenshot(clip=probe_clip)
    bg = _dominant_color(blank)

    # Focus xterm's hidden input (a canvas click doesn't reliably focus the
    # WebGL surface in headless Chromium) before typing, per test_new_shell.
    textarea = term.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type(f"python3 {script_path}", delay=15)
    page.keyboard.press("Enter")

    # Confirm the flood actually started (independent of rendering): the
    # heartbeat reaches PROBE_DRAWN, then advances into numbered waves.
    beat = None
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        beat = _read_heartbeat(heartbeat)
        if beat is not None and (beat.startswith("PROBE_DRAWN") or beat.isdigit()):
            break
        if beat is not None and beat.startswith("ERROR:"):
            raise AssertionError(f"flood script failed to start: {beat}")
        page.wait_for_timeout(250)
    else:
        raise AssertionError(f"flood script never started; heartbeat={beat!r}")

    # Baseline: the drawn status rows once the probe row ink is present. The
    # flood never touches this region, so its bytes are fixed for the run.
    baseline = None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        shot = page.screenshot(clip=probe_clip)
        if _ink_pixels(shot, bg) > 200:
            baseline = shot
            break
        page.wait_for_timeout(250)
    assert baseline is not None, "terminal status rows never drew for a baseline"
    baseline_path = tmp_path / "probe_baseline.png"
    baseline_path.write_bytes(baseline)

    # Poll the static rows while the atlas churns. With the 8-page cap a
    # repack happens every few waves, forcing a full repaint each time; a
    # stale texture bind redraws the untouched rows with the wrong glyphs and
    # they stay wrong. A repack may glitch a single frame while glyphs
    # re-rasterize, so only corruption that survives a second sample counts.
    corrupted_path = tmp_path / "probe_corrupted.png"
    started = time.monotonic()
    corrupted_after: float | None = None
    diff_count = 0
    while time.monotonic() - started < 120.0:
        page.wait_for_timeout(250)
        shot = page.screenshot(clip=probe_clip)
        if _diff_pixels(baseline, shot) > 25:
            page.wait_for_timeout(350)
            shot = page.screenshot(clip=probe_clip)
            diff_count = _diff_pixels(baseline, shot)
            if diff_count > 25:
                corrupted_after = time.monotonic() - started
                corrupted_path.write_bytes(shot)
                break

    last_wave = _read_heartbeat(heartbeat)
    assert corrupted_after is None, (
        f"terminal drew wrong glyphs in static status rows after CJK glyph "
        f"churn: {diff_count} pixels changed ~{corrupted_after:.0f}s into the "
        f"flood (flood wave {last_wave}; baseline {baseline_path}, "
        f"corrupted {corrupted_path})"
    )


_HARNESS_HTML = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="file://{xterm_css}">
<script src="file://{xterm_js}"></script>
<script src="file://{webgl_js}"></script>
<style>html,body{{margin:0;background:#000}}#t{{width:960px;height:480px}}</style>
</head><body><div id="t"></div></body></html>"""

_HARNESS_SETUP = """
() => {
  const term = new Terminal({
    cols: 100, rows: 30,
    minimumContrastRatio: 4.5,
    fontFamily: 'monospace',
  });
  term.open(document.getElementById('t'));
  const addon = new WebglAddon.WebglAddon();
  term.loadAddon(addon);
  window.term = term;
  window.addon = addon;
  return true;
}
"""

_HARNESS_INSTRUMENT = """
() => {
  const renderer = window.addon._renderer;
  const atlas = renderer._charAtlas;
  const glyphRenderer = renderer._glyphRenderer.value;
  if (!atlas || !glyphRenderer) return 'not-ready';
  // Cap pages at 8 (read live by the atlas) so repacks happen within seconds.
  atlas.constructor.maxAtlasPages = 8;
  window.__uploads = {};
  window.__merges = 0;
  const origBind = glyphRenderer._bindAtlasPageTexture;
  glyphRenderer._bindAtlasPageTexture = function (gl, a, i) {
    window.__uploads[i] = a.pages[i].canvas;
    return origBind.call(this, gl, a, i);
  };
  const atlasProto = Object.getPrototypeOf(atlas);
  const origMerge = atlasProto._mergePages;
  atlasProto._mergePages = function (...args) {
    window.__merges++;
    return origMerge.apply(this, args);
  };
  // A slot is stale when its texture was uploaded from a canvas that no
  // longer occupies the slot and the version counters match, so the
  // version-only rebind rule would never refresh it.
  window.__staleSlots = () => {
    const a = window.addon._renderer._charAtlas;
    const g = window.addon._renderer._glyphRenderer.value;
    const out = [];
    for (let i = 0; i < a.pages.length; i++) {
      const uploaded = window.__uploads[i];
      if (uploaded && uploaded !== a.pages[i].canvas
          && a.pages[i].version === g._atlasTextures[i].version) {
        out.push(i);
      }
    }
    return out;
  };
  return 'ok';
}
"""

_HARNESS_CHURN = """
() => {
  let cp = 0, wave = 0;
  window.__churn = setInterval(() => {
    const parts = [];
    for (let row = 4; row < 30; row++) {
      const color = 22 + (wave * 7) % 200;
      const weight = wave % 3 === 0 ? '1' : (wave % 3 === 1 ? '2' : '0');
      let line = `\\x1b[${row};1H\\x1b[0;${weight};38;5;${color}m`;
      for (let c = 0; c < 49; c++) {
        line += String.fromCharCode(0x4e00 + (cp % 20000));
        cp++;
      }
      parts.push(line);
    }
    window.term.write(parts.join(''));
    wave++;
  }, 50);
  return true;
}
"""


def test_atlas_page_repack_rebinds_swapped_textures(page: Page, tmp_path: Path) -> None:
    """Every atlas page slot swapped by a repack gets its texture re-uploaded.

    Drives the app's bundled ``@xterm/xterm`` + ``@xterm/addon-webgl`` directly
    (no server) and churns unique CJK glyphs until the atlas has repacked many
    times, asserting no texture slot is left bound to a page that moved away.
    """
    html_path = tmp_path / "atlas_harness.html"
    html_path.write_text(
        _HARNESS_HTML.format(
            xterm_css=_XTERM_LIB / "css" / "xterm.css",
            xterm_js=_XTERM_LIB / "lib" / "xterm.js",
            webgl_js=_WEBGL_LIB / "lib" / "addon-webgl.js",
        ),
        encoding="utf-8",
    )
    page.goto(f"file://{html_path}")
    page.evaluate(_HARNESS_SETUP)
    page.wait_for_timeout(500)

    # WebGL may be unavailable (no GPU context): the addon then never builds
    # an atlas, and there is nothing to exercise.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if page.evaluate(_HARNESS_INSTRUMENT) == "ok":
            break
        page.wait_for_timeout(250)
    else:
        raise AssertionError("WebGL glyph atlas never initialized")

    page.evaluate(_HARNESS_CHURN)

    stale: list[int] = []
    merges = 0
    started = time.monotonic()
    while time.monotonic() - started < 60.0:
        page.wait_for_timeout(500)
        merges = page.evaluate("() => window.__merges")
        candidates = page.evaluate("() => window.__staleSlots()")
        if candidates:
            # A slot legitimately lags between a repack and the next rendered
            # frame; only a bind that survives further frames is stale.
            page.wait_for_timeout(250)
            stale = page.evaluate("() => window.__staleSlots()")
            if stale:
                break
        if merges >= 30:
            break

    assert not stale, (
        f"glyph atlas left texture slots {stale} bound to pages that moved "
        f"away during a repack (after {merges} page merges); those slots draw "
        f"the wrong glyphs on every repaint"
    )
    assert merges >= 5, (
        f"atlas churn produced only {merges} page merges; the repack path was not exercised"
    )


@pytest.mark.parametrize("bundle", ["js", "mjs"])
def test_shared_atlas_invalidates_each_renderer_once(page: Page, bundle: str) -> None:
    """Clearing a shared atlas refreshes both terminals once, then stays incremental."""
    page.set_content(
        "<style>.terminal { width: 960px; height: 480px; }</style>"
        '<div id="a" class="terminal"></div><div id="b" class="terminal"></div>'
    )
    page.add_style_tag(path=str(_XTERM_LIB / "css" / "xterm.css"))
    page.add_script_tag(path=str(_XTERM_LIB / "lib" / "xterm.js"))
    if bundle == "js":
        page.add_script_tag(path=str(_WEBGL_LIB / "lib" / "addon-webgl.js"))
    else:
        page.evaluate(
            """async source => {
              const url = URL.createObjectURL(new Blob([source], {type: 'text/javascript'}));
              try { window.WebglAddon = await import(url); }
              finally { URL.revokeObjectURL(url); }
            }""",
            (_WEBGL_LIB / "lib" / "addon-webgl.mjs").read_text(encoding="utf-8"),
        )

    result = page.evaluate(
        """async () => {
          const terms = [], addons = [];
          try {
            for (const id of ['a', 'b']) {
              const term = new Terminal({cols: 100, rows: 30, fontFamily: 'monospace'});
              terms.push(term);
              term.open(document.getElementById(id));
              const addon = new WebglAddon.WebglAddon();
              term.loadAddon(addon);
              addons.push(addon);
              await new Promise(resolve => term.write('Static text 日本語', resolve));
            }
            await new Promise(resolve => {
              requestAnimationFrame(() => requestAnimationFrame(resolve));
            });
            const renderers = addons.map(addon => addon._renderer);
            const shared = renderers[0]._charAtlas === renderers[1]._charAtlas;
            const calls = renderers.map(renderer => {
              const updates = [];
              const original = renderer._updateModel;
              renderer._updateModel = function(start, end) {
                updates.push([start, end]);
                return original.call(this, start, end);
              };
              return updates;
            });
            const render = () => {
              calls.forEach(updates => { updates.length = 0; });
              renderers.forEach(renderer => renderer.renderRows(10, 10));
              return calls.map(updates => updates.slice());
            };
            const before = render();
            addons[0].clearTextureAtlas();
            const afterClear = render();
            const nextFrame = render();
            addons[1].clearTextureAtlas();
            const afterSiblingClear = render();
            const nextSiblingFrame = render();
            renderers.forEach(renderer => {
              renderer._glyphRenderer.value.setAtlas(renderer._charAtlas);
            });
            const afterReattach = render();
            const nextReattachFrame = render();
            const originalUpdate = renderers[0]._updateModel;
            let clearDuringUpdate = true;
            renderers[0]._updateModel = function(start, end) {
              originalUpdate.call(this, start, end);
              if (clearDuringUpdate) {
                clearDuringUpdate = false;
                this._charAtlas.clearTexture();
              }
            };
            const afterMidFrameClear = render();
            const nextMidFrame = render();
            return {shared, before, afterClear, nextFrame, afterSiblingClear,
                    nextSiblingFrame, afterReattach, nextReattachFrame,
                    afterMidFrameClear, nextMidFrame};
          } finally {
            terms.forEach(term => term.dispose());
          }
        }"""
    )
    assert result["shared"], "the terminals must exercise the same pooled atlas"
    partial = [[[10, 10]], [[10, 10]]]
    full = [[[0, 29]], [[0, 29]]]
    assert result["before"] == partial
    assert result["afterClear"] == full
    assert result["nextFrame"] == partial, "atlas clear must not force full redraws forever"
    assert result["afterSiblingClear"] == full
    assert result["nextSiblingFrame"] == partial
    assert result["afterReattach"] == full
    assert result["nextReattachFrame"] == partial
    assert result["afterMidFrameClear"] == [[[10, 10], [0, 29]], [[0, 29]]]
    assert result["nextMidFrame"] == partial
