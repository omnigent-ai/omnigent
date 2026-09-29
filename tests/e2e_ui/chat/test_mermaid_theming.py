"""E2E: mermaid diagrams follow the app theme and use the available width.

A mermaid fence renders with a static light palette regardless of the app
theme: in dark mode the sequence-diagram message labels (near-black fill) sit
on the dark canvas and are illegible, the diagram canvas paints an opaque
background inside the block, a live light->dark switch leaves an open diagram
with its stale colors, and a wide diagram collapses to a narrow canvas instead
of using the message column. Seeded assistant bubbles / files, no LLM turn.
"""

from __future__ import annotations

import re
import time

import httpx
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"

# Wide enough (six participants) that its natural width exceeds the chat
# column, and with several arrow labels drawn directly on the canvas.
_WIDE_SEQUENCE_MESSAGE = (
    "Login flow:\n\n"
    "```mermaid\n"
    "sequenceDiagram\n"
    "    participant Client\n"
    "    participant Gateway\n"
    "    participant AuthService\n"
    "    participant SessionService\n"
    "    participant Database\n"
    "    participant Cache\n"
    "    Client->>Gateway: POST /login\n"
    "    Gateway->>AuthService: validate credentials\n"
    "    AuthService->>Database: SELECT user\n"
    "    Database-->>AuthService: row\n"
    "    AuthService->>SessionService: create session\n"
    "    SessionService->>Cache: store token\n"
    "    Cache-->>SessionService: ok\n"
    "    SessionService-->>Gateway: session token\n"
    "    Gateway-->>Client: 200 OK\n"
    "```\n"
)

_DIAGRAM_SVG = '[data-streamdown="mermaid-block"] svg[aria-roledescription]'

# Fill of a message label and the composited page background behind the
# diagram (nearest-to-farthest ancestor backgrounds blended over white, the
# browser's default canvas).
_LABEL_AND_BACKGROUND_JS = """
() => {
  const svg = document.querySelector('__DIAGRAM_SVG__');
  const label = svg?.querySelector('text.messageText, .messageText');
  if (!svg || !label) return null;
  const layers = [];
  for (let el = svg; el; el = el.parentElement) {
    const c = getComputedStyle(el).backgroundColor;
    const m = c.match(/rgba?\\(([^)]+)\\)/);
    if (!m) continue;
    const [r, g, b, a = "1"] = m[1].split(",").map((v) => v.trim());
    if (Number(a) > 0) layers.push([Number(r), Number(g), Number(b), Number(a)]);
  }
  let bg = [255, 255, 255];
  for (const [r, g, b, a] of layers.reverse()) {
    bg = [r * a + bg[0] * (1 - a), g * a + bg[1] * (1 - a), b * a + bg[2] * (1 - a)];
  }
  return { labelFill: getComputedStyle(label).fill, background: bg };
}
""".replace("__DIAGRAM_SVG__", _DIAGRAM_SVG)


def _seed_diagram_message(base_url: str, session_id: str, text: str) -> None:
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_assistant_message", "data": {"agent": _AGENT_NAME, "text": text}},
        timeout=10.0,
    ).raise_for_status()


def _parse_rgb(color: str) -> tuple[float, float, float]:
    match = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", color)
    assert match, f"unparseable color: {color!r}"
    return (float(match.group(1)), float(match.group(2)), float(match.group(3)))


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        scaled = value / 255.0
        return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    lighter, darker = sorted((_relative_luminance(a), _relative_luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _label_contrast(page: Page) -> float:
    probe = page.evaluate(_LABEL_AND_BACKGROUND_JS)
    assert probe is not None, "no rendered mermaid message label found"
    label = _parse_rgb(probe["labelFill"])
    background = tuple(float(v) for v in probe["background"])
    return _contrast_ratio(label, background)


def test_dark_mode_message_labels_are_legible(page: Page, seeded_session: tuple[str, str]) -> None:
    """In dark mode the sequence-diagram arrow labels must contrast with the canvas."""
    base_url, session_id = seeded_session
    _seed_diagram_message(base_url, session_id, _WIDE_SEQUENCE_MESSAGE)

    page.emulate_media(color_scheme="dark")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_DIAGRAM_SVG)).to_be_visible(timeout=30_000)

    contrast = _label_contrast(page)
    assert contrast >= 4.5, (
        f"dark-mode message labels are illegible: contrast {contrast:.2f}:1 "
        "(light-theme text on the dark canvas), expected at least 4.5:1"
    )


def test_diagram_canvas_is_transparent_inside_the_block(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Nothing between the block chrome and the SVG may paint an opaque canvas."""
    base_url, session_id = seeded_session
    _seed_diagram_message(base_url, session_id, _WIDE_SEQUENCE_MESSAGE)

    page.emulate_media(color_scheme="dark")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_DIAGRAM_SVG)).to_be_visible(timeout=30_000)

    opaque = page.evaluate(
        """() => {
      const svg = document.querySelector('__DIAGRAM_SVG__');
      const block = svg.closest('[data-streamdown="mermaid-block"]');
      const painted = [];
      for (let el = svg; el && el !== block; el = el.parentElement) {
        const bg = getComputedStyle(el).backgroundColor;
        if (bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
          painted.push(`${el.tagName}.${el.className?.baseVal ?? el.className}: ${bg}`);
        }
      }
      return painted;
    }""".replace("__DIAGRAM_SVG__", _DIAGRAM_SVG)
    )
    assert opaque == [], f"diagram canvas is not transparent, painted layers: {opaque}"


def test_live_theme_change_recolors_an_open_diagram(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """An open diagram must become legible when the theme flips light -> dark."""
    base_url, session_id = seeded_session
    _seed_diagram_message(base_url, session_id, _WIDE_SEQUENCE_MESSAGE)

    page.emulate_media(color_scheme="light")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_DIAGRAM_SVG)).to_be_visible(timeout=30_000)
    assert _label_contrast(page) >= 4.5, "diagram is expected to be legible in light mode"

    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_class(re.compile(r"\bdark\b"), timeout=10_000)

    # Poll: a compliant renderer may re-render the SVG asynchronously.
    deadline = time.monotonic() + 10.0
    contrast = 0.0
    while time.monotonic() < deadline:
        expect(page.locator(_DIAGRAM_SVG)).to_be_visible(timeout=10_000)
        contrast = _label_contrast(page)
        if contrast >= 4.5:
            return
        page.wait_for_timeout(500)
    raise AssertionError(
        f"diagram kept its light-theme colors after switching to dark mode: "
        f"label contrast {contrast:.2f}:1, expected at least 4.5:1"
    )


def test_wide_diagram_fills_the_message_column(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A diagram wider than the column must use the available message width."""
    base_url, session_id = seeded_session
    _seed_diagram_message(base_url, session_id, _WIDE_SEQUENCE_MESSAGE)

    page.set_viewport_size({"width": 1720, "height": 960})
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_DIAGRAM_SVG)).to_be_visible(timeout=30_000)

    widths = page.evaluate(
        """() => {
      const svg = document.querySelector('__DIAGRAM_SVG__');
      const bubble = svg.closest('[data-testid="message-bubble"]');
      return {
        svg: svg.getBoundingClientRect().width,
        natural: parseFloat(getComputedStyle(svg).maxWidth) || Infinity,
        bubble: bubble.getBoundingClientRect().width,
      };
    }""".replace("__DIAGRAM_SVG__", _DIAGRAM_SVG)
    )
    assert widths["natural"] > widths["bubble"], (
        f"precondition: diagram (natural {widths['natural']}px) should be wider than "
        f"the column ({widths['bubble']}px); widen the seeded diagram"
    )
    assert widths["svg"] >= 0.75 * widths["bubble"], (
        f"wide diagram shrank to {widths['svg']:.0f}px inside a "
        f"{widths['bubble']:.0f}px message column"
    )


_PREVIEW_FILE = "diagram_preview.md"

_PREVIEW_CONTENT = (
    "# Flow\n\n"
    "```mermaid\n"
    "sequenceDiagram\n"
    "    Client->>Server: request\n"
    "    Server-->>Client: response\n"
    "```\n"
)


def test_file_preview_diagram_is_legible_in_dark_mode(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The read-only file preview shares the renderer and must theme its labels too."""
    base_url, session_id = seeded_session
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_PREVIEW_FILE}",
        json={"content": _PREVIEW_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    ).raise_for_status()

    page.emulate_media(color_scheme="dark")
    page.add_init_script(
        """
        window.localStorage.setItem(
          "omnigent:file-view-preferences",
          JSON.stringify({
            diffActive: false,
            diffLayout: "unified",
            previewableViewMode: "preview",
            hideWhitespace: false,
            wrapLines: false,
          }),
        );
        """
    )
    page.goto(f"{base_url}/c/{session_id}?file={_PREVIEW_FILE}")

    preview = page.locator('[data-testid="file-viewer"]:visible [data-testid="mermaid-preview"]')
    diagram = preview.locator("svg[aria-roledescription]")
    expect(diagram).to_be_visible(timeout=30_000)

    probe = page.evaluate(
        """() => {
      const svg = document.querySelector(
        '[data-testid="mermaid-preview"] svg[aria-roledescription]');
      const label = svg?.querySelector('text.messageText, .messageText');
      if (!label) return null;
      const layers = [];
      for (let el = svg; el; el = el.parentElement) {
        const c = getComputedStyle(el).backgroundColor;
        const m = c.match(/rgba?\\(([^)]+)\\)/);
        if (!m) continue;
        const [r, g, b, a = "1"] = m[1].split(",").map((v) => v.trim());
        if (Number(a) > 0) layers.push([Number(r), Number(g), Number(b), Number(a)]);
      }
      let bg = [255, 255, 255];
      for (const [r, g, b, a] of layers.reverse()) {
        bg = [r * a + bg[0] * (1 - a), g * a + bg[1] * (1 - a), b * a + bg[2] * (1 - a)];
      }
      return { labelFill: getComputedStyle(label).fill, background: bg };
    }"""
    )
    assert probe is not None, "no rendered mermaid message label found in the preview"
    contrast = _contrast_ratio(
        _parse_rgb(probe["labelFill"]), tuple(float(v) for v in probe["background"])
    )
    assert contrast >= 4.5, (
        f"dark-mode preview labels are illegible: contrast {contrast:.2f}:1, "
        "expected at least 4.5:1"
    )
