"""The composer styles Markdown live while typing, and sends the draft unchanged.

A highlight layer paints the styled draft behind the transparent-text textarea,
so every check here holds both halves to one layout: the layer's glyphs must
land on exactly the pixels the textarea's own glyphs would. Markers are hidden
(transparent, so they keep their width) except in the span being edited.
"""

from __future__ import annotations

import io
import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail, seed_committed_turn
from tests.e2e_ui.mobile.test_ios_ipad_safe_layout import _IOS_SHELL_INIT_SCRIPT
from tests.e2e_ui.sessions.test_reply_quotes_session_switch import _reply_to

_LAYER = '[data-testid="composer-highlight-overlay"]'
_TRANSPARENT = re.compile(r"(^|\s)text-transparent(\s|$)")
_TOUCH = pytest.mark.browser_context_args(has_touch=True)

# Mirrors web/src/lib/composerMarkdown.ts.
_ITALIC, _BOLD, _CODE, _FENCE, _MARKER, _INFO = 1, 2, 4, 8, 16, 128
_STRIKE, _LINK, _LINK_URL, _QUOTE = 512, 1024, 2048, 4096

_FORMATTED_LINES = [
    "Make _this_ italic, **this** bold and `this` inline code.",
    "Keep snake_case_names, file_name.py and \\*escaped\\* markers plain.",
    "```py",
    "def total(items):  # **not bold** in a fence",
    "    return sum(item.price for item in items)",
    "```",
    "Done — *thanks*!",
]
_FORMATTED_DRAFT = "\n".join(_FORMATTED_LINES)

# Wrapped prose, a long unbroken string, a fence, and enough lines to scroll
# well past the composer's height cap.
_ALIGNMENT_DRAFT = "\n".join(
    [
        "**Wrapped** prose with _emphasis_ and `code` "
        + "that keeps going to force several soft wraps across the composer. " * 4,
        "unbroken_" + "x" * 240,
        "",
        "   ",
        "```",
        *[f"line {i}: const value_{i} = compute(**kwargs)  // _not_ styled" for i in range(12)],
        "wrapped fence line " * 14,
        "```",
        *[f"{i}. item with *star emphasis* and a `span` and snake_case_{i}" for i in range(24)],
        "\ttab\tseparated\t_cells_",
        "_a slanted italic run that keeps going long enough to wrap across the composer "
        + "width more than once, with **bold inside** it_",
        "_" + "slanted" * 40 + "_",
        "~~struck~~ and ~single~ with a [link to docs](https://example.test/docs) inside",
        "> quoted with _emphasis_ and `code`",
        "lazy continuation of the quote",
        "trailing newline follows",
        "",
    ]
)

# Neutralizes paint so rendered glyphs can be compared pixel for pixel. Hidden
# halves use display:none: toggling visibility can leave stale decoration paint.
_PROBE_CSS = """
html[data-align-probe] :is([data-testid="composer-highlight-overlay"], [data-align-clone]) {
  color: rgb(20, 20, 20) !important;
  -webkit-text-fill-color: rgb(20, 20, 20) !important;
  opacity: 1 !important;
}
html[data-align-probe] [data-testid="composer-highlight-overlay"] { display: none !important; }
html[data-align-probe="textarea"] [data-align-clone] { display: none !important; }
html[data-align-probe="textarea"] textarea {
  color: rgb(20, 20, 20) !important;
  -webkit-text-fill-color: rgb(20, 20, 20) !important;
  opacity: 1 !important;
}
html[data-align-probe] textarea { caret-color: transparent !important; }
"""

# Worst per-character offset between the styled layer and a plain-text clone of
# it (same box, one text node). Chromium snaps each inline fragment to 1/64 px.
_CHARACTER_OFFSETS_JS = """textarea => {
    const field = textarea.parentElement;
    const layer = field.querySelector('[data-testid="composer-highlight-overlay"]');
    const clone = field.querySelector('[data-align-clone]');
    const rectAt = (node, offset) => {
        const range = document.createRange();
        range.setStart(node, offset);
        range.setEnd(node, offset + 1);
        return range.getClientRects()[0] ?? null;
    };
    const styled = [];
    const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        for (let i = 0; i < node.length; i++) styled.push(rectAt(node, i));
    }
    const plain = clone.firstChild;
    let worst = 0;
    let compared = 0;
    for (let i = 0; i < plain.length; i++) {
        if (plain.data[i] === "\\n") continue;
        const a = styled[i];
        const b = rectAt(plain, i);
        if (!a || !b) continue;
        compared++;
        worst = Math.max(worst, Math.abs(a.x - b.x), Math.abs(a.y - b.y),
            Math.abs(a.right - b.right), Math.abs(a.bottom - b.bottom));
    }
    return { worst, compared, total: plain.length, styledLength: styled.length };
}"""


def _compact_view(textarea: Locator) -> Locator:
    """The compact preview shown in a textarea's place while it isn't focused."""
    return textarea.locator("xpath=..").get_by_test_id("composer-compact-view")


def _styled_runs(layer: Locator) -> list[tuple[str, int]]:
    return layer.evaluate(
        "layer => Array.from(layer.querySelectorAll('[data-md]'),"
        " span => [span.textContent, Number(span.dataset.md)])"
    )


def _texts(runs: list[tuple[str, int]], flag: int, *, marker: bool = False) -> list[str]:
    return [text for text, flags in runs if flags & flag and bool(flags & _MARKER) == marker]


def _span_geometry(layer: Locator) -> list[Any]:
    """Every styled span's line boxes and computed box/typography styles."""
    return layer.evaluate("""layer => Array.from(layer.querySelectorAll('[data-md]'), span => {
        const style = getComputedStyle(span);
        return {
            rects: Array.from(span.getClientRects(), r => [r.x, r.y, r.width, r.height]),
            box: [style.paddingLeft, style.paddingRight, style.marginLeft, style.marginRight,
                  style.borderLeftWidth, style.borderRightWidth],
            font: [style.fontFamily, style.fontSize, style.fontStyle, style.fontWeight,
                   style.letterSpacing, style.lineHeight],
        };
    })""")


def _layout_pair(textarea: Locator) -> dict[str, Any]:
    """The textarea's and layer's box, typography and scroll state."""
    return textarea.evaluate("""textarea => {
        const field = textarea.parentElement;
        const layer = field.querySelector('[data-testid="composer-highlight-overlay"]');
        const read = element => {
            const style = getComputedStyle(element);
            const bounds = element.getBoundingClientRect();
            const gutter = parseFloat(style.paddingLeft);
            return {
                // The text box: the layer reaches into the input's left gutter
                // (for a block quote's bar) and pads back to the textarea's.
                box: [bounds.x + gutter, bounds.y, bounds.width - gutter, bounds.height],
                gutter: [bounds.x, gutter],
                client: [element.clientWidth - gutter, element.clientHeight],
                scrollHeight: element.scrollHeight,
                scrollTop: element.scrollTop,
                typography: [style.fontFamily, style.fontSize, style.fontWeight, style.fontStyle,
                    style.lineHeight, style.letterSpacing, style.wordSpacing, style.tabSize,
                    style.whiteSpace, style.overflowWrap, style.wordBreak, style.boxSizing,
                    style.paddingTop, style.paddingRight, style.paddingBottom,
                    style.borderTopWidth, style.borderLeftWidth, style.textIndent,
                    style.fontFeatureSettings, style.fontVariationSettings, style.fontKerning],
            };
        };
        return { textarea: read(textarea), layer: read(layer) };
    }""")


def _screenshot(page: Page, textarea: Locator, probe: str) -> Image.Image:
    page.evaluate("probe => { document.documentElement.dataset.alignProbe = probe; }", probe)
    _next_frame(page)
    image = Image.open(io.BytesIO(textarea.screenshot(animations="disabled", caret="hide")))
    return image.convert("RGB")


def _assert_layer_aligned(page: Page, textarea: Locator, tmp_path: Path, name: str) -> None:
    """Assert the layer lays its glyphs out where the textarea does.

    Two steps: a plain-text clone of the layer (same box, styles and scroll)
    renders pixel-identical to the textarea's own glyphs, and every character of
    the styled layer sits within a tenth of a pixel of the clone's.
    """
    layer = textarea.locator("xpath=..").locator(_LAYER)
    expect(layer).to_be_visible()
    expect(layer).to_have_attribute("aria-hidden", "true")
    assert layer.evaluate("layer => layer.textContent") == textarea.input_value()
    # Scroll events arrive a frame after scrollTop changes.
    _next_frame(page)
    pair = _layout_pair(textarea)
    textarea_state, layer_state = pair["textarea"], pair["layer"]
    assert textarea_state["typography"] == layer_state["typography"], pair
    # Exact, fractions included: the layer follows the textarea's used height.
    assert textarea_state["box"] == pytest.approx(layer_state["box"], abs=0.001), pair
    assert textarea_state["client"] == layer_state["client"], pair
    # The textarea has no padding; the layer's 8px gutter lies left of its text.
    assert textarea_state["gutter"][1] == 0, pair
    assert layer_state["gutter"] == pytest.approx(
        [textarea_state["gutter"][0] - 8, 8], abs=0.001
    ), pair
    assert textarea_state["scrollHeight"] == layer_state["scrollHeight"], pair
    assert textarea_state["scrollTop"] == layer_state["scrollTop"], pair

    # Paint-only styles: stripping every span's styling moves no line box.
    styled_geometry = _span_geometry(layer)
    layer_font = layer.evaluate(
        "l => { const s = getComputedStyle(l); return [s.fontFamily, s.fontSize, s.fontStyle,"
        " s.fontWeight, s.letterSpacing, s.lineHeight]; }"
    )
    for geometry in styled_geometry:
        assert geometry["box"] == ["0px"] * 6, geometry
        assert geometry["font"] == layer_font, geometry
    stripped = layer.evaluate("""layer => {
        const spans = Array.from(layer.querySelectorAll('[data-md]'));
        const classes = spans.map(span => span.className);
        spans.forEach(span => { span.className = ''; });
        const rects = Array.from(spans, span =>
            Array.from(span.getClientRects(), r => [r.x, r.y, r.width, r.height]));
        spans.forEach((span, i) => { span.className = classes[i]; });
        return rects;
    }""")
    assert stripped == [geometry["rects"] for geometry in styled_geometry]

    textarea.evaluate("""textarea => {
        const field = textarea.parentElement;
        const layer = field.querySelector('[data-testid="composer-highlight-overlay"]');
        const clone = layer.cloneNode(false);
        clone.removeAttribute('data-testid');
        clone.setAttribute('data-align-clone', '');
        clone.textContent = layer.textContent;
        layer.after(clone);
        clone.scrollTop = layer.scrollTop;
    }""")
    page.add_style_tag(content=_PROBE_CSS)
    try:
        offsets = textarea.evaluate(_CHARACTER_OFFSETS_JS)
        clone_pixels = _screenshot(page, textarea, "clone")
        textarea_pixels = _screenshot(page, textarea, "textarea")
    finally:
        textarea.evaluate("""textarea => {
            delete document.documentElement.dataset.alignProbe;
            textarea.parentElement.querySelector('[data-align-clone]')?.remove();
        }""")
    print(f"Composer layer alignment ({name}): {offsets}")
    assert offsets["styledLength"] == offsets["total"], offsets
    assert offsets["compared"] > 0, offsets
    assert offsets["worst"] <= 0.1, (name, offsets)
    diff = ImageChops.difference(clone_pixels, textarea_pixels)
    if diff.getbbox() is not None:
        clone_pixels.save(tmp_path / f"{name}-clone.png")
        textarea_pixels.save(tmp_path / f"{name}-textarea.png")
    assert diff.getbbox() is None, f"{name}: the layer's box renders text off the textarea's"


def _region(page: Page, textarea: Locator, start: int, end: int) -> Image.Image:
    """Screenshot of the layer glyphs for draft offsets ``start``–``end`` (one line)."""
    box = textarea.evaluate(
        """(textarea, [start, end]) => {
        const field = textarea.parentElement;
        const layer = field.querySelector('[data-testid="composer-highlight-overlay"]');
        const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
        const range = document.createRange();
        let at = 0;
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            if (at <= start && start < at + node.length) range.setStart(node, start - at);
            if (at < end && end <= at + node.length) range.setEnd(node, end - at);
            at += node.length;
        }
        const rect = range.getBoundingClientRect();
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    }""",
        [start, end],
    )
    image = page.screenshot(clip=box, animations="disabled", caret="hide")
    return Image.open(io.BytesIO(image)).convert("RGB")


def _glyph_pixels(image: Image.Image) -> int:
    """Pixels that stand out from the region's dominant (background) color."""
    background = ImageStat.Stat(image).median
    return sum(
        1
        for pixel in image.get_flattened_data()
        if max(abs(channel - base) for channel, base in zip(pixel, background, strict=True)) > 60
    )


def _assert_selection_visible(unselected: Image.Image, selected: Image.Image) -> None:
    """The selection tints the region and the styled glyphs stay legible on it."""
    before = ImageStat.Stat(unselected).median
    after = ImageStat.Stat(selected).median
    assert max(abs(a - b) for a, b in zip(before, after, strict=True)) >= 4, (before, after)
    glyphs_before, glyphs_after = _glyph_pixels(unselected), _glyph_pixels(selected)
    assert glyphs_before > 50, glyphs_before
    assert glyphs_after >= glyphs_before * 0.6, (glyphs_before, glyphs_after)


def _type_lines(page: Page, lines: list[str]) -> None:
    """Type line by line, as a user would, with inserted newlines between.

    Fence lines go in whole: typing ``` keystroke by keystroke turns it into a
    code block (covered by its own test), which would move the caret.
    """
    for index, line in enumerate(lines):
        if index:
            page.keyboard.insert_text("\n")
        if line.lstrip().startswith(("```", "~~~")):
            page.keyboard.insert_text(line)
        else:
            page.keyboard.type(line)


def _next_frame(page: Page) -> None:
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => resolve(null)))")


_MARKER_STATES_JS = """layer => Array.from(layer.querySelectorAll('[data-md]'))
    .filter(span => Number(span.dataset.md) & 16)
    .map(span => ({
        text: span.textContent,
        color: getComputedStyle(span).color,
        revealed: span.hasAttribute('data-revealed'),
        width: span.getBoundingClientRect().width,
    }))"""


def _shown_markers(layer: Locator) -> list[str]:
    """The markers painted visibly; every other marker is transparent but keeps its width."""
    states = layer.evaluate(_MARKER_STATES_JS)
    assert states, "the draft should have Markdown markers"
    for state in states:
        assert (state["color"] == "rgba(0, 0, 0, 0)") != state["revealed"], states
        assert state["width"] > 0, states
    return [state["text"] for state in states if state["revealed"]]


def _move_caret(textarea: Locator, start: int, end: int | None = None) -> None:
    textarea.evaluate(
        "(t, [start, end]) => { t.focus(); t.setSelectionRange(start, end); }",
        [start, start if end is None else end],
    )
    expect(textarea).to_have_js_property("selectionEnd", start if end is None else end)
    _next_frame(textarea.page)


_CODE_BLOCK_JS = """layer => {
    const blocks = layer.querySelectorAll('[data-code-block]');
    const block = blocks[0];
    if (!block) return { count: 0 };
    const style = getComputedStyle(block);
    const edges = element => {
        const r = element.getBoundingClientRect();
        return [r.left, r.right, r.top, r.bottom];
    };
    const layerBox = layer.getBoundingClientRect();
    const lineHeight = parseFloat(getComputedStyle(layer).lineHeight);
    return {
        count: blocks.length,
        fragments: block.getClientRects().length,
        box: edges(block),
        content: [layerBox.left + parseFloat(getComputedStyle(layer).paddingLeft),
            layerBox.left + layer.clientWidth],
        lines: Array.from(block.querySelectorAll(':scope > div > div'), edges),
        lineHeight,
        background: style.backgroundColor,
        radius: style.borderTopLeftRadius,
        innerBackgrounds: Array.from(block.querySelectorAll('*'),
            element => getComputedStyle(element).backgroundColor)
            .filter(color => color !== 'rgba(0, 0, 0, 0)'),
        text: block.textContent,
    };
}"""


def _assert_code_block(page: Page, layer: Locator, text: str, *, pixels: bool) -> None:
    """The fence is one tinted, rounded box across the text width, its lines stacked gap-free."""
    block = layer.evaluate(_CODE_BLOCK_JS)
    assert block["count"] == 1, block
    assert block["text"] == text, block
    assert block["fragments"] == 1, block
    assert block["background"] != "rgba(0, 0, 0, 0)", block
    assert block["radius"] not in ("0px", ""), block
    assert block["innerBackgrounds"] == [], block
    left, right, top, bottom = block["box"]
    assert [left, right] == pytest.approx(block["content"], abs=0.01), block
    lines = block["lines"]
    assert len(lines) == text.count("\n") + (0 if text.endswith("\n") else 1), block
    assert lines[0][2] == pytest.approx(top, abs=0.01), block
    assert lines[-1][3] == pytest.approx(bottom, abs=0.01), block
    for previous, line in itertools.pairwise(lines):
        assert line[2] == pytest.approx(previous[3], abs=0.01), block
    for line in lines:
        assert line[:2] == pytest.approx([left, right], abs=0.01), block
    if any(line[3] - line[2] > block["lineHeight"] * 1.5 for line in lines):
        print(f"Code block wraps a long line inside its box: {block['box']}")
    if not pixels:
        return
    # One solid tint down the box's right edge, where short code lines leave no ink.
    height = bottom - top
    radius = float(block["radius"].removesuffix("px"))
    image = Image.open(
        io.BytesIO(
            page.screenshot(
                clip={"x": left, "y": top, "width": right - left + 6, "height": height},
                animations="disabled",
                caret="hide",
            )
        )
    ).convert("RGB")
    scale = image.height / height
    column = round((right - left - 3) * scale)
    samples = [
        image.getpixel((column, y))
        for y in range(round((radius + 1) * scale), round((height - radius - 1) * scale))
    ]
    tint = samples[len(samples) // 2]
    for sample in samples:
        assert max(abs(a - b) for a, b in zip(sample, tint, strict=True)) <= 2, (sample, tint)
    outside = image.getpixel((image.width - 1, image.height // 2))
    assert max(abs(a - b) for a, b in zip(outside, tint, strict=True)) >= 4, (outside, tint)


def _assert_code_pill(layer: Locator, text: str) -> None:
    """Inline code sits in one rounded, tinted pill that spans its backticks."""
    pill = layer.locator("[data-code-pill]").filter(has_text=text).first
    expect(pill).to_have_text(text)
    style = pill.evaluate(
        "p => { const s = getComputedStyle(p); return [s.backgroundColor, s.borderTopLeftRadius,"
        " s.paddingLeft, s.paddingRight, s.marginLeft, s.borderLeftWidth]; }"
    )
    assert style[0] != "rgba(0, 0, 0, 0)", style
    assert style[1] not in ("0px", ""), style
    assert style[2:] == ["0px"] * 4, style


def _assert_formatted(layer: Locator) -> None:
    runs = _styled_runs(layer)
    assert _texts(runs, _ITALIC) == ["this", "thanks"], runs
    assert _texts(runs, _BOLD) == ["this"], runs
    assert _texts(runs, _CODE) == ["this"], runs
    assert _texts(runs, _CODE, marker=True) == ["`", "`"], runs
    fence_body = "\n" + "\n".join(_FORMATTED_LINES[3:5]) + "\n" + "\n"
    assert "".join(t for t, f in runs if f & _FENCE and not f & (_INFO | _MARKER)) == fence_body
    # Only the backticks are markers; the info string stays dimmed and visible.
    assert _texts(runs, _FENCE, marker=True) == ["```", "```"], runs
    assert [text for text, flags in runs if flags & _INFO] == ["py"], runs
    info = layer.locator(f'[data-md="{_FENCE | _INFO}"]')
    assert info.evaluate("s => getComputedStyle(s).color") != "rgba(0, 0, 0, 0)"
    assert [text for text, flags in runs if flags == _MARKER] == ["_", "_", "**", "**", "*", "*"]
    outside_fence = "".join(text for text, flags in runs if not flags & _FENCE)
    for plain in ("snake_case_names", "file_name.py", "escaped", "not bold"):
        assert plain not in outside_fence, (plain, runs)
    # Width-neutral stand-ins for bold and italic, never a different face.
    bold = layer.locator(f'[data-md="{_BOLD}"]')
    expect(bold).to_have_css("font-weight", layer.evaluate("l => getComputedStyle(l).fontWeight"))
    # The faux bold's stroke: 0.06em of the draft's font size.
    stroke, size = bold.evaluate(
        "s => { const c = getComputedStyle(s); return [c.webkitTextStrokeWidth, c.fontSize]; }"
    )
    assert float(stroke.removesuffix("px")) == pytest.approx(
        float(size.removesuffix("px")) * 0.06, abs=0.01
    ), (stroke, size)
    italic = layer.locator(f'[data-md="{_ITALIC}"]').first
    expect(italic).to_have_css("font-style", "normal")
    assert "linear-gradient" in italic.evaluate("s => getComputedStyle(s).backgroundImage")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_session_composer_styles_markdown_and_sends_it_unchanged(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
) -> None:
    """Typing Markdown styles it live; the sent message is exactly what was typed.

    The draft has no outer whitespace: trimming it on send is existing behavior
    this feature doesn't change, so the byte-for-byte check covers the interior.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(color_scheme=theme)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    layer = composer.locator("xpath=..").locator(_LAYER)

    composer.click()
    page.keyboard.type("Plain snake_case_start ")
    expect(layer).to_have_count(0)
    expect(composer).not_to_have_class(_TRANSPARENT)
    composer.fill("")
    _type_lines(page, _FORMATTED_LINES)
    expect(composer).to_have_value(_FORMATTED_DRAFT)
    expect(layer).to_be_visible()
    expect(composer).to_have_class(_TRANSPARENT)
    _assert_formatted(layer)
    # The layer is presentation only; the textarea is still the one textbox.
    expect(page.get_by_role("textbox", name="Message the agent", exact=True)).to_have_count(1)
    expect(composer).to_be_focused()
    _assert_layer_aligned(page, composer, tmp_path, f"session-{theme}")
    card = page.locator("[data-composer-card]").filter(has=composer)
    fence_text = "\n".join(_FORMATTED_LINES[2:6]) + "\n"
    _assert_code_block(page, layer, fence_text, pixels=True)
    _assert_code_pill(layer, "`this`")

    # The caret after "!" edits no span: every marker is hidden.
    assert _shown_markers(layer) == [], layer.evaluate(_MARKER_STATES_JS)
    card.screenshot(path=tmp_path / f"composer-markdown-{theme}.png", animations="disabled")
    # Pixels agree: the hidden `**` paint nothing, and the same `**` paint when revealed.
    bold = _FORMATTED_DRAFT.index("**this**")
    hidden_pixels = _glyph_pixels(_region(page, composer, bold, bold + 2))
    _move_caret(composer, bold + 4)
    revealed_pixels = _glyph_pixels(_region(page, composer, bold, bold + 2))
    assert hidden_pixels == 0 and revealed_pixels > 20, (hidden_pixels, revealed_pixels)
    assert _shown_markers(layer) == ["**", "**"]
    # Inside a span, or right beside its markers, shows that span's markers.
    _move_caret(composer, _FORMATTED_DRAFT.index("this") + 2)
    assert _shown_markers(layer) == ["_", "_"]
    _move_caret(composer, _FORMATTED_DRAFT.index("`this`") + 6)
    assert _shown_markers(layer) == ["`", "`"]
    card.screenshot(
        path=tmp_path / f"composer-markdown-{theme}-caret-inside.png", animations="disabled"
    )
    _assert_layer_aligned(page, composer, tmp_path, f"session-revealed-{theme}")
    # Anywhere in the fenced block, fence lines included, shows its backticks.
    _move_caret(composer, _FORMATTED_DRAFT.index("return"))
    assert _shown_markers(layer) == ["```", "```"]
    _move_caret(composer, _FORMATTED_DRAFT.index("```py"))
    assert _shown_markers(layer) == ["```", "```"]
    card.screenshot(
        path=tmp_path / f"composer-markdown-{theme}-fence-caret.png", animations="disabled"
    )
    _move_caret(composer, _FORMATTED_DRAFT.index("Done"))
    assert _shown_markers(layer) == []
    # Leaving the composer hides them all.
    _move_caret(composer, _FORMATTED_DRAFT.index("this") + 2)
    composer.blur()
    expect(composer).not_to_be_focused()
    _next_frame(page)
    assert _shown_markers(layer) == []
    _move_caret(composer, len(_FORMATTED_DRAFT))

    # A selection still paints over the styled glyphs, and keeps them readable.
    unselected = _region(page, composer, 5, 28)
    composer.evaluate("textarea => textarea.setSelectionRange(5, 28)")
    expect(composer).to_have_js_property("selectionEnd", 28)
    _next_frame(page)
    # A selection shows the markers of every span it touches.
    assert _shown_markers(layer) == ["_", "_", "**", "**"]
    selection = composer.evaluate("t => getComputedStyle(t, '::selection').webkitTextFillColor")
    assert selection in ("rgba(0, 0, 0, 0)", "transparent"), selection
    selected = _region(page, composer, 5, 28)
    _assert_selection_visible(unselected, selected)
    card.screenshot(
        path=tmp_path / f"composer-markdown-{theme}-selection.png", animations="disabled"
    )
    composer.evaluate("t => t.setSelectionRange(t.value.length, t.value.length)")

    # IME composition shows natively: the layer steps aside until it commits.
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.imeSetComposition", {"text": "にほん", "selectionStart": 3, "selectionEnd": 3})
    expect(layer).to_have_count(0)
    expect(composer).not_to_have_class(_TRANSPARENT)
    cdp.send("Input.insertText", {"text": "日本"})
    draft = f"{_FORMATTED_DRAFT}日本"
    expect(composer).to_have_value(draft)
    expect(layer).to_be_visible()
    expect(composer).to_have_class(_TRANSPARENT)

    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    page.route(
        events_url,
        lambda route: route.fulfill(json={"queued": True, "item_id": "ci_live_markdown"}),
    )
    with page.expect_request(events_url) as sent:
        page.get_by_role("button", name="Send", exact=True).click()
    assert sent.value.post_data_json["data"]["content"] == [{"type": "input_text", "text": draft}]
    expect(composer).to_have_value("")
    expect(layer).to_have_count(0)

    # Up-arrow history recall brings the sent draft back, styled again.
    composer.focus()
    page.keyboard.press("ArrowUp")
    expect(composer).to_have_value(draft)
    expect(layer.locator(f'[data-md="{_BOLD}"]')).to_have_text("this")


@pytest.mark.parametrize(
    ("theme", "width", "native"),
    [
        pytest.param("light", 1024, False, id="desktop-light"),
        pytest.param("dark", 1024, False, id="desktop-dark"),
        pytest.param("light", 390, True, marks=_TOUCH, id="ios-light"),
    ],
)
def test_highlight_layer_stays_aligned_while_wrapping_and_scrolling(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
    width: int,
    native: bool,
) -> None:
    """Wrapped lines, unbroken strings, fences and scrolled drafts stay aligned."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(color_scheme=theme)
    if native:
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    if native:
        expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_ALIGNMENT_DRAFT)
    expect(composer).to_have_value(_ALIGNMENT_DRAFT)
    pair = _layout_pair(composer)
    # The draft must exceed the height cap, so both halves really scroll.
    assert pair["textarea"]["scrollHeight"] > pair["textarea"]["client"][1] * 3, pair

    layer = composer.locator("xpath=..").locator(_LAYER)
    fence_start = _ALIGNMENT_DRAFT.index("```")
    fence_end = _ALIGNMENT_DRAFT.index("```\n", fence_start + 3) + 4
    max_scroll = composer.evaluate("t => t.scrollHeight - t.clientHeight")
    for fraction in (0.0, 0.37, 1.0):
        composer.evaluate("(t, top) => { t.scrollTop = top; }", round(max_scroll * fraction))
        _assert_layer_aligned(page, composer, tmp_path, f"scroll-{theme}-{width}-{fraction}")
        _assert_code_block(page, layer, _ALIGNMENT_DRAFT[fence_start:fence_end], pixels=False)

    # Typing at the end keeps the caret line in view, and the layer follows it.
    composer.focus()
    composer.evaluate("t => t.setSelectionRange(t.value.length, t.value.length)")
    page.keyboard.type("more **bold** at the end")
    page.keyboard.insert_text("\n\n")
    expect(composer).to_have_value(f"{_ALIGNMENT_DRAFT}more **bold** at the end\n\n")
    _assert_layer_aligned(page, composer, tmp_path, f"typed-{theme}-{width}")


def test_reply_blocks_style_markdown_and_send_interleaved(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Reply-quote text blocks get the same layer, inside the scrolling input area."""
    base_url, session_id = seeded_session
    quote = "A point worth quoting."
    seed_committed_turn(session_id, prompt="Make a point.", reply=quote)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    paragraph = page.locator('[data-role="assistant"]').get_by_text(quote, exact=True)
    expect(paragraph).to_be_visible(timeout=30_000)

    intro = "Intro with **bold** and snake_case_name"
    composer.fill(intro)
    _reply_to(page, paragraph)
    reply = page.get_by_role("textbox", name="Reply text before quote 1", exact=True)
    expect(reply).to_have_value(intro)
    expect(composer).to_be_focused()
    tail = "Tail with `code` and _em_"
    page.keyboard.insert_text(tail)
    # The block not being edited shows compact, its markers taking no space.
    expect(_compact_view(reply)).to_have_text("Intro with bold and snake_case_name")
    for textarea, name in ((reply, "reply-block"), (composer, "reply-tail")):
        textarea.focus()
        runs = _styled_runs(textarea.locator("xpath=..").locator(_LAYER))
        assert any(flags & (_BOLD | _CODE) for _, flags in runs), (name, runs)
        _assert_layer_aligned(page, textarea, tmp_path, name)

    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    page.route(
        events_url, lambda route: route.fulfill(json={"queued": True, "item_id": "ci_reply_md"})
    )
    with page.expect_request(events_url) as sent:
        page.get_by_role("button", name="Send", exact=True).click()
    assert sent.value.post_data_json["data"]["content"] == [
        {"type": "input_text", "text": f"{intro}\n\n> {quote}\n\n{tail}"}
    ]


def test_edited_markdown_repaints_cleanly_and_restores_with_the_draft(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Unstyling text leaves no stale paint, and a restored draft renders the same."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    layer = composer.locator("xpath=..").locator(_LAYER)
    composer.click()
    page.keyboard.type("Keep _this_ and **that** in `code`")
    expect(layer.locator(f'[data-md="{_ITALIC}"]')).to_have_text("this")
    # Break the italic and the code span so their styled spans become plain text.
    page.keyboard.press("Backspace")
    composer.evaluate("t => t.setSelectionRange(11, 11)")
    page.keyboard.press("Backspace")
    draft = "Keep _this and **that** in `code"
    expect(composer).to_have_value(draft)
    expect(layer.locator(f'[data-md="{_ITALIC}"]')).to_have_count(0)
    expect(layer.locator(f'[data-md="{_CODE}"]')).to_have_count(0)
    expect(layer.locator(f'[data-md="{_BOLD}"]')).to_have_text("that")
    _next_frame(page)
    edited = Image.open(io.BytesIO(composer.screenshot(animations="disabled", caret="hide")))

    page.reload()
    expect(composer).to_have_value(draft, timeout=30_000)
    # Restored unfocused, it shows compact; editing it again, exactly as before.
    expect(_compact_view(composer)).to_have_text("Keep _this and that in `code")
    composer.focus()
    composer.evaluate("t => t.setSelectionRange(11, 11)")
    expect(layer.locator(f'[data-md="{_BOLD}"]')).to_have_text("that")
    _next_frame(page)
    restored = Image.open(io.BytesIO(composer.screenshot(animations="disabled", caret="hide")))
    assert ImageChops.difference(edited.convert("RGB"), restored.convert("RGB")).getbbox() is None


def test_restored_crlf_draft_stays_aligned(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """A restored draft with CRLF line breaks lays out as the textarea shows it."""
    base_url, session_id = seeded_session
    draft = "Restored _draft_\r\n```\r\ncode **not bold**\r\n```\r\n**after** the fence\r\nend"
    drafts = json.dumps({session_id: draft})
    page.add_init_script(f"sessionStorage.setItem('omnigent.sessionDrafts', {json.dumps(drafts)})")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    # textarea.value normalizes every line break to LF.
    expect(composer).to_have_value(draft.replace("\r\n", "\n"), timeout=30_000)
    expect(_compact_view(composer)).to_have_text(
        "Restored draft\ncode **not bold**\nafter the fence\nend", use_inner_text=True
    )
    composer.focus()
    layer = composer.locator("xpath=..").locator(_LAYER)
    runs = _styled_runs(layer)
    assert _texts(runs, _BOLD) == ["after"], runs
    assert (
        "".join(t for t, f in runs if f & _FENCE and not f & _MARKER) == "\ncode **not bold**\n\n"
    )
    _assert_layer_aligned(page, composer, tmp_path, "crlf-restore")


_MEASURE_KEYSTROKES = """async count => {
    const times = [];
    for (let i = 0; i < count; i++) {
        const start = performance.now();
        document.execCommand('insertText', false, 'x');
        await new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)));
        times.push(performance.now() - start);
    }
    times.sort((a, b) => a - b);
    return { median: times[Math.floor(count / 2)], p90: times[Math.floor(count * 0.9)] };
}"""


# Moves the caret into a line's `**strong**`, then onto plain text, near the
# middle of the draft; each move is timed to the next painted frame and must
# reveal exactly that token's two markers (or none) when the layer is shown.
_MEASURE_CARET_MOVES = """async ([count, lineStarts, inside, outside]) => {
    const t = document.activeElement;
    const layer = t.parentElement.querySelector('[data-testid="composer-highlight-overlay"]');
    const times = [];
    let wrong = 0;
    for (let i = 0; i < count; i++) {
        const line = lineStarts[2500 + (i >> 1) * 7];
        const into = i % 2 === 0;
        const at = line + (into ? inside : outside);
        const start = performance.now();
        t.setSelectionRange(at, at);
        await new Promise(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)));
        times.push(performance.now() - start);
        if (layer) {
            const shown = layer.querySelectorAll('[data-revealed]').length;
            if (shown !== (into ? 2 : 0)) wrong++;
        }
    }
    times.sort((a, b) => a - b);
    return { median: times[Math.floor(count / 2)], p90: times[Math.floor(count * 0.9)], wrong };
}"""


# ArrowDown pressed through the real keyboard handler: each press is timed in
# the page from its keydown to the frame after it paints.
_WATCH_KEYS = """() => {
    window.__keyTimes = [];
    document.addEventListener('keydown', () => {
        const start = performance.now();
        requestAnimationFrame(() => setTimeout(() => {
            window.__keyTimes.push(performance.now() - start);
        }, 0));
    }, { capture: true });
}"""
_KEY_TIMES = """() => {
    const times = [...window.__keyTimes].sort((a, b) => a - b);
    const at = share => times[Math.floor(times.length * share)];
    return { median: at(0.5), p90: at(0.9), count: times.length };
}"""


def test_large_markdown_draft_keeps_typing_responsive(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A 5,000-line styled draft stays responsive, near the bare textarea's speed."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    results = {}
    caret_moves = {}
    arrow_keys = {}
    page.evaluate(_WATCH_KEYS)
    for label, line in (
        ("plain", "line {i} with em strong code and snake case {i}"),
        ("markdown", "line {i} with _em_ **strong** `code` snake_case_{i}"),
    ):
        lines = [line.format(i=i) for i in range(5000)]
        draft = "\n".join(lines)
        composer.evaluate(
            """(t, v) => {
            const proto = HTMLTextAreaElement.prototype;
            const set = Object.getOwnPropertyDescriptor(proto, 'value').set;
            t.focus(); set.call(t, v); t.dispatchEvent(new Event('input', { bubbles: true }));
        }""",
            draft,
        )
        expect(composer).to_have_value(draft)
        if label == "markdown":
            layer = composer.locator("xpath=..").locator(_LAYER)
            expect(layer.locator(f'[data-md="{_BOLD}"]').first).to_have_text("strong")
        line_starts = [0]
        for text in lines[:-1]:
            line_starts.append(line_starts[-1] + len(text) + 1)
        sample = line.format(i=2500)
        inside, outside = sample.index("strong") + 2, sample.index("with")
        composer.evaluate("t => t.focus()")
        page.evaluate(_MEASURE_CARET_MOVES, [4, line_starts, inside, outside])
        caret_moves[label] = page.evaluate(
            _MEASURE_CARET_MOVES, [40, line_starts, inside, outside]
        )
        # Real ArrowDown presses, through the composer's keyboard handler.
        start = line_starts[2500] + inside
        composer.evaluate("(t, at) => { t.focus(); t.setSelectionRange(at, at); }", start)
        for _ in range(3):
            page.keyboard.press("ArrowDown")
        settle = "() => new Promise(r => requestAnimationFrame(() => setTimeout(r, 50)))"
        page.evaluate(settle)
        page.evaluate("() => { window.__keyTimes = []; }")
        for _ in range(30):
            page.keyboard.press("ArrowDown")
        page.evaluate(settle)
        arrow_keys[label] = page.evaluate(_KEY_TIMES)
        assert arrow_keys[label]["count"] == 30, arrow_keys
        # Each press moved the caret down a line and left the draft alone.
        expect(composer).to_have_value(draft)
        caret = composer.evaluate("t => t.selectionStart")
        assert line_starts[2533] <= caret < line_starts[2534], (caret, line_starts[2533])
        middle = len(draft) // 2
        composer.evaluate("(t, at) => { t.focus(); t.setSelectionRange(at, at); }", middle)
        page.evaluate(_MEASURE_KEYSTROKES, 3)
        results[label] = page.evaluate(_MEASURE_KEYSTROKES, 25)
        # Every measured keystroke really edited the draft, at the caret.
        expect(composer).to_have_value(draft[:middle] + "x" * 28 + draft[middle:])
    print(f"Composer keystroke latency, 5,000 lines (~{len(draft) // 1000}k chars): {results}")
    print(f"Composer caret-move latency, 5,000 lines: {caret_moves}")
    print(f"Composer ArrowDown latency, 5,000 lines: {arrow_keys}")
    # ArrowDown reuses the current parse: it costs about what the bare textarea's does.
    keys, plain_keys = arrow_keys["markdown"], arrow_keys["plain"]
    assert keys["median"] < 100 and keys["p90"] < 250, arrow_keys
    assert keys["median"] <= 2 * plain_keys["median"] + 30, arrow_keys
    # Each caret move revealed exactly the touched token's markers, or none.
    assert caret_moves["markdown"]["wrong"] == 0, caret_moves
    # A caret move toggles one token; it must not cost like rebuilding the layer.
    moves, plain_moves = caret_moves["markdown"], caret_moves["plain"]
    assert moves["median"] < 100 and moves["p90"] < 250, caret_moves
    assert moves["median"] <= 2 * plain_moves["median"] + 30, caret_moves
    markdown, plain = results["markdown"], results["plain"]
    assert markdown["median"] < 500 and markdown["p90"] < 1000, results
    # The layer may cost more than the bare textarea, but within a generous bound.
    assert markdown["median"] <= 3 * plain["median"] + 150, results


def test_ordinary_markdown_draft_types_at_textarea_speed(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """In an everyday 40-line draft, a styled keystroke costs about a plain one."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    templates = {
        "plain": (
            "Line {i} has some italic words here and bold text with code too",
            "  quoted {i} with struck text and a link to example.test/{i}",
            "A plain sentence {i}, long enough to wrap across a narrower composer width",
        ),
        "markdown": (
            "Line {i} has _some italic words here_ and **bold text** with `code` too",
            "> quoted {i} with ~~struck text~~ and a [link](https://example.test/{i})",
            "A plain sentence {i}, long enough to wrap across a *narrower* composer width",
        ),
    }
    page.evaluate(_WATCH_KEYS)
    results = {}
    for label, lines in templates.items():
        draft = "\n".join(lines[i % 3].format(i=i) for i in range(40))
        composer.fill(draft)
        expect(composer).to_have_value(draft)
        if label == "markdown":
            layer = composer.locator("xpath=..").locator(_LAYER)
            expect(layer.locator("[data-slanted]").first).to_be_attached()
        # Type inside an italic run (its words, in the plain draft) mid-draft.
        at = draft.index("italic words", draft.index("Line 21 "))
        _move_caret(composer, at)
        page.keyboard.type("xyz")
        settle = "() => new Promise(r => requestAnimationFrame(() => setTimeout(r, 50)))"
        page.evaluate(settle)
        page.evaluate("() => { window.__keyTimes = []; }")
        for _ in range(40):
            page.keyboard.press("x")
        page.evaluate(settle)
        results[label] = page.evaluate(_KEY_TIMES)
        assert results[label]["count"] == 40, results
        expect(composer).to_have_value(draft[:at] + "xyz" + "x" * 40 + draft[at:])
        if label == "markdown":
            assert layer.locator("[data-slanted]").count() > 0
    print(f"Composer keystroke latency, 40-line draft: {results}")
    markdown, plain = results["markdown"], results["plain"]
    # A typical keystroke is within one frame (16ms) of the bare textarea's; the
    # slow tail (layout of the edited row and its copies, GC) within three.
    assert markdown["median"] <= plain["median"] + 16, results
    assert markdown["p90"] <= plain["p90"] + 50, results
    # And absolutely, so a slow bare textarea can't excuse a slow layer: a
    # typical keystroke paints within four frames at 60Hz, the tail within eight.
    assert markdown["median"] <= 66, results
    assert markdown["p90"] <= 133, results


_EDIT_ABOVE_VIEWPORT = """(textarea, prefix) => {
    const field = textarea.parentElement;
    const layer = field.querySelector('[data-testid="composer-highlight-overlay"]');
    const before = textarea.scrollTop;
    const caret = textarea.value.length;
    const proto = HTMLTextAreaElement.prototype;
    const set = Object.getOwnPropertyDescriptor(proto, 'value').set;
    set.call(textarea, prefix + textarea.value);
    textarea.setSelectionRange(caret + prefix.length, caret + prefix.length);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    // React commits the discrete input synchronously; reading forces layout.
    const sync = [textarea.scrollTop, layer.scrollTop];
    return new Promise(resolve => requestAnimationFrame(() => resolve({
        before, sync, frame: [textarea.scrollTop, layer.scrollTop],
    })));
}"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_layer_keeps_the_textarea_scroll_when_text_changes_above_it(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
) -> None:
    """An edit above the viewport never scrolls the layer on its own."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1024, "height": 844})
    page.emulate_media(color_scheme=theme)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    draft = "\n".join(f"{i}. line with *emphasis* and `code` in it" for i in range(120))
    composer.fill(draft)
    expect(composer).to_have_value(draft)
    layer = composer.locator("xpath=..").locator(_LAYER)
    expect(layer).to_be_visible()
    composer.evaluate("t => { t.scrollTop = 900; }")
    _next_frame(page)
    assert composer.evaluate("t => t.scrollTop") == 900
    assert layer.evaluate("l => l.scrollTop") == 900

    # A dictation update, say: wrapping text lands above the visible lines
    # while the textarea's own scroll position stays put.
    prefix = "**Dictated** text that wraps across the composer width, " * 6 + "\n"
    result = composer.evaluate(_EDIT_ABOVE_VIEWPORT, prefix)
    print(f"Scroll after an edit above the viewport ({theme}): {result}")
    assert result["before"] == 900, result
    assert result["sync"][0] == 900 and result["frame"][0] == 900, result
    assert result["sync"][1] == result["sync"][0], result
    assert result["frame"][1] == result["frame"][0], result
    expect(composer).to_have_value(prefix + draft)
    assert layer.evaluate("l => getComputedStyle(l).overflowAnchor") == "none"
    _assert_layer_aligned(page, composer, tmp_path, f"edit-above-{theme}")
    assert layer.evaluate("l => l.scrollTop") == composer.evaluate("t => t.scrollTop") == 900


# Each slanted span's line fragments, as first-glyph rects, and the copies drawn
# for them (skew removed) in the same order.
_SLANT_GEOMETRY_JS = """layer => {
    const rect = (node, i) => { const r = document.createRange(); r.setStart(node, i);
        r.setEnd(node, i + 1); return r.getClientRects()[0]; };
    const glyphs = [];
    for (const span of layer.querySelectorAll('[data-slanted]')) {
        const text = span.firstChild; let top = null;
        for (let i = 0; i < text.length; i++) { const q = rect(text, i);
            if (!q || text.data[i] === '\\n') continue;
            if (top === null || Math.abs(q.top - top) >= 1) {
                glyphs.push([q.left, q.top]);
                top = q.top;
            } }
    }
    const copies = Array.from(layer.querySelectorAll('.md-slant'), copy => {
        copy.style.transform = 'none'; const r = copy.getBoundingClientRect();
        copy.style.transform = ''; return [r.left, r.top, getComputedStyle(copy).transform]; });
    return { glyphs, copies };
}"""


# Hide the copies and let the in-flow italics paint, then undo that.
_SHOW_IN_FLOW_ITALICS_JS = """layer => {
    layer.querySelector('[data-italic-overlay]').style.display = 'none';
    for (const span of layer.querySelectorAll('[data-slanted]')) {
        span.dataset.probe = '';
        span.removeAttribute('data-slanted');
    }
}"""
_RESTORE_SLANTED_ITALICS_JS = """layer => {
    layer.querySelector('[data-italic-overlay]').style.display = '';
    for (const span of layer.querySelectorAll('[data-probe]')) {
        span.setAttribute('data-slanted', '');
        span.removeAttribute('data-probe');
    }
}"""


def _assert_italics_slanted(page: Page, textarea: Locator, tmp_path: Path, name: str) -> None:
    """Italic text is drawn by skewed copies that sit exactly on its in-flow glyphs."""
    layer = textarea.locator("xpath=..").locator(_LAYER)
    _next_frame(page)
    geometry = layer.evaluate(_SLANT_GEOMETRY_JS)
    glyphs, copies = geometry["glyphs"], geometry["copies"]
    assert copies, "italic text near the viewport should get slanted copies"
    assert len(glyphs) == len(copies), geometry
    lifts = [copy[1] - glyph[1] for glyph, copy in zip(glyphs, copies, strict=True)]
    for glyph, copy in zip(glyphs, copies, strict=True):
        assert abs(copy[0] - glyph[0]) <= 0.1, (name, glyph, copy)
        # A skew only: matrix(1, 0, tan(angle), 1, 0, 0).
        assert copy[2].startswith("matrix(1, 0, -0.2") and copy[2].endswith(", 1, 0, 0)"), copy
    # One vertical offset for every copy: the line box's half-leading.
    assert max(lifts) - min(lifts) <= 0.1, (name, lifts)
    # Unskewed, the copies paint exactly what the in-flow text would.
    # (The dotted underline is the fallback look, so it's off for the comparison.)
    page.add_style_tag(
        content="html[data-slant-probe] .md-slant { transform: none !important; }"
        " html[data-slant-probe] .md-em { --md-italic: none !important; }"
    )
    page.evaluate("() => { document.documentElement.dataset.slantProbe = ''; }")
    try:
        _next_frame(page)
        copied = Image.open(io.BytesIO(textarea.screenshot(caret="hide"))).convert("RGB")
        layer.evaluate(_SHOW_IN_FLOW_ITALICS_JS)
        _next_frame(page)
        in_flow = Image.open(io.BytesIO(textarea.screenshot(caret="hide"))).convert("RGB")
    finally:
        layer.evaluate(_RESTORE_SLANTED_ITALICS_JS)
        page.evaluate("() => { delete document.documentElement.dataset.slantProbe; }")
    # Showing or hiding the overlay can shift a translucent background's blend by
    # a level or two; a misplaced glyph differs by far more at its edges.
    diff = ImageChops.difference(copied, in_flow)
    worst = max(max(pixel) for pixel in diff.get_flattened_data())
    if worst > 16:
        copied.save(tmp_path / f"{name}-copies.png")
        in_flow.save(tmp_path / f"{name}-in-flow.png")
    assert worst <= 16, f"{name}: unskewed copies don't match the in-flow italics ({worst})"
    print(f"Composer italic slant ({name}): {len(copies)} copies, lift {lifts[0]:.3f}px")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_italics_are_slanted_without_moving_a_glyph(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
) -> None:
    """Wrapped, unbroken and nested italics slant while caret geometry stays exact."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1024, "height": 844})
    page.emulate_media(color_scheme=theme)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    draft = (
        "Plain then _a slanted run long enough to wrap across the composer width, with "
        "**bold inside** and a [link](https://example.test) in it_, then "
        + "_"
        + "unbroken" * 30
        + "_ and *starred*."
    )
    composer.fill(draft)
    expect(composer).to_have_value(draft)
    layer = composer.locator("xpath=..").locator(_LAYER)
    expect(layer.locator("[data-slanted]").first).to_be_attached()
    _assert_layer_aligned(page, composer, tmp_path, f"slant-{theme}")
    _assert_italics_slanted(page, composer, tmp_path, f"slant-{theme}")
    # Revealing markers inside the italic run re-slants them with their new style.
    _move_caret(composer, draft.index("bold inside") + 2)
    _assert_italics_slanted(page, composer, tmp_path, f"slant-revealed-{theme}")
    card = page.locator("[data-composer-card]").filter(has=composer)
    card.screenshot(path=tmp_path / f"composer-italic-{theme}.png", animations="disabled")
    # The overlay is presentation only.
    overlay = layer.locator("[data-italic-overlay]")
    expect(overlay).to_have_css("pointer-events", "none")
    assert layer.evaluate("l => l.getAttribute('aria-hidden')") == "true"


# Narrows the composer card (1/64px steps, layout's unit) to the widest width
# at which the italic run wraps; returns it, the next width up (where it fits),
# and the layer's integer clientWidth at each.
_WRAP_BOUNDARY_JS = """textarea => {
    const card = textarea.closest('[data-composer-card]');
    const layer = textarea.parentElement.querySelector(
        '[data-testid="composer-highlight-overlay"]');
    const wraps = width => {
        card.style.width = `${width}px`;
        const lines = new Set();
        for (const span of layer.querySelectorAll('.md-em'))
            for (const rect of span.getClientRects()) lines.add(Math.round(rect.top));
        return { wrapped: lines.size > 1, clientWidth: layer.clientWidth };
    };
    let low = 64 * 120, high = 64 * Math.ceil(card.getBoundingClientRect().width);
    if (!wraps(low / 64).wrapped || wraps(high / 64).wrapped) return null;
    while (high - low > 1) {
        const mid = (low + high) >> 1;
        if (wraps(mid / 64).wrapped) low = mid; else high = mid;
    }
    const at = [wraps(low / 64), wraps(high / 64)];
    return { wrap: low / 64, fit: high / 64, clientWidths: at.map(a => a.clientWidth),
        layerWidths: [low, high].map(w => { card.style.width = `${w / 64}px`;
            return layer.getBoundingClientRect().width; }) };
}"""


def _set_card_width(page: Page, textarea: Locator, width: float) -> None:
    textarea.evaluate(
        "(t, w) => { t.closest('[data-composer-card]').style.width = `${w}px`; }", width
    )
    _next_frame(page)
    _next_frame(page)


def test_italic_slant_follows_a_fractional_resize(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """A sub-pixel resize that moves a wrap moves the copies; clientWidth doesn't change."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1024, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    layer = composer.locator("xpath=..").locator(_LAYER)
    boundary = None
    # A wrap point whose two sides round to one clientWidth; another prefix
    # moves the point if this one's sides straddle a rounding edge.
    for prefix in ("x ", "xy ", "x, ", "xyz "):
        composer.fill(f"{prefix}_alpha beta gamma delta_")
        expect(layer.locator("[data-slanted]").first).to_be_attached()
        _move_caret(composer, 0)
        boundary = composer.evaluate(_WRAP_BOUNDARY_JS)
        assert boundary is not None, "the italic run should wrap in a narrow card"
        if boundary["clientWidths"][0] == boundary["clientWidths"][1]:
            break
    assert boundary and boundary["clientWidths"][0] == boundary["clientWidths"][1], boundary
    print(f"Composer slant wrap boundary: {boundary}")
    for step, width in (("fits", boundary["fit"]), ("wraps", boundary["wrap"])):
        _set_card_width(page, composer, width)
        _assert_italics_slanted(page, composer, tmp_path, f"resize-{step}")
    # And back: the copies rejoin the first line.
    _set_card_width(page, composer, boundary["fit"])
    _assert_italics_slanted(page, composer, tmp_path, "resize-fits-again")
    lines = {round(copy.bounding_box()["y"]) for copy in layer.locator(".md-slant").all()}
    assert len(lines) == 1, f"the whole run fits on one line again: {lines}"


def test_auto_paired_markers_show_and_send_real_text(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Typed openers get their closers; the box shows and sends exactly that text."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    sent: list[str] = []

    def capture(route: Any) -> None:
        sent.append(route.request.post_data_json["data"]["content"][0]["text"])
        route.fulfill(json={"queued": True, "item_id": "ci_md_pairs"})

    page.route(f"{base_url}/v1/sessions/{session_id}/events", capture)
    caret = lambda: composer.evaluate("t => t.selectionStart")  # noqa: E731
    composer.click()
    # An opener gets its closer after the caret; one undo takes just the closer.
    page.keyboard.type("say *")
    expect(composer).to_have_value("say **")
    assert caret() == 5
    page.keyboard.press("ControlOrMeta+z")
    expect(composer).to_have_value("say *")
    composer.fill("")

    # `**bold*`: the second `*` grows the pair, one `*` steps over its closer.
    page.keyboard.type("**bold*")
    expect(composer).to_have_value("**bold**")
    assert caret() == len("**bold**")
    page.keyboard.type(" and `code`")
    draft = "**bold** and `code`"
    expect(composer).to_have_value(draft)
    layer = composer.locator("xpath=..").locator(_LAYER)
    runs = _styled_runs(layer)
    assert _texts(runs, _BOLD) == ["bold"], runs
    assert _texts(runs, _CODE) == ["code"], runs
    _assert_layer_aligned(page, composer, tmp_path, "pairs")
    card = page.locator("[data-composer-card]").filter(has=composer)
    card.screenshot(path=tmp_path / "composer-autopair.png", animations="disabled")
    page.keyboard.press("Enter")
    for _ in range(50):
        if sent:
            break
        page.wait_for_timeout(100)
    assert sent == [draft], sent
    expect(composer).to_have_value("")

    # A closer typed in full by habit isn't doubled; undoing a step-over brings
    # back the typed `*` before the closer.
    page.keyboard.type("**x** then *hi*")
    expect(composer).to_have_value("**x** then *hi*")
    page.keyboard.press("ControlOrMeta+z")
    expect(composer).to_have_value("**x** then *hi**")

    # A backtick pairs on an empty line too: `code` there is inline code, and
    # three make the code block, whose first undo leaves ``` as typed.
    composer.fill("")
    page.keyboard.type("`code`")
    expect(composer).to_have_value("`code`")
    page.keyboard.press("ControlOrMeta+z")
    expect(composer).to_have_value("`code``")
    composer.fill("")
    page.keyboard.type("```")
    expect(composer).to_have_value("```\n\n```")
    assert caret() == len("```\n")
    for undone in ("```", "`````", "````"):
        page.keyboard.press("ControlOrMeta+z")
        expect(composer).to_have_value(undone)


def test_code_block_in_a_quoted_list_item_stays_code(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """A fence after `> - ` is code: Enter adds its lines, never sends, and nothing tidies."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    sent: list[str] = []

    def capture(route: Any) -> None:
        sent.append(route.request.post_data_json["data"]["content"][0]["text"])
        route.fulfill(json={"queued": True, "item_id": "ci_md_list_fence"})

    page.route(f"{base_url}/v1/sessions/{session_id}/events", capture)
    composer.click()
    # ``` typed after the item's marker makes a block whose lines continue the item.
    page.keyboard.type("> - ```")
    expect(composer).to_have_value("> - ```\n>   \n>   ```")
    page.keyboard.type("** not bold**")
    body = "> - ```\n>   ** not bold**\n>   ```"
    expect(composer).to_have_value(body)
    # Deleting and retyping the closing `*` leaves the code as typed.
    page.keyboard.press("Backspace")
    page.keyboard.type("*")
    expect(composer).to_have_value(body)
    layer = composer.locator("xpath=..").locator(_LAYER)
    runs = _styled_runs(layer)
    assert _texts(runs, _BOLD) == [], runs
    assert "** not bold**" in "".join(t for t, f in runs if f & _FENCE), runs
    _assert_layer_aligned(page, composer, tmp_path, "list-fence")
    # Enter inside `not bold` adds a line in the block (in the item), and sends nothing.
    _move_caret(composer, body.index("not") + 3)
    page.keyboard.press("Enter")
    split = "> - ```\n>   ** not\n>    bold**\n>   ```"
    expect(composer).to_have_value(split)
    # From its empty last line, Enter leaves the block, staying in the item.
    _move_caret(composer, split.index(" bold**") + len(" bold**"))
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")
    expect(composer).to_have_value(split + "\n>   ")
    page.wait_for_timeout(300)
    assert sent == [], sent


def test_alt_enter_adds_lines_in_code_blocks_and_quotes(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Alt+Enter is a line break everywhere: in a quoted block it keeps the quote, never exits."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    sent: list[str] = []

    def capture(route: Any) -> None:
        sent.append(route.request.post_data_json["data"]["content"][0]["text"])
        route.fulfill(json={"queued": True, "item_id": "ci_md_alt_enter"})

    page.route(f"{base_url}/v1/sessions/{session_id}/events", capture)
    composer.click()
    page.keyboard.type("> ```")
    page.keyboard.type("code")
    expect(composer).to_have_value("> ```\n> code\n> ```")
    # In a quoted block the new line keeps the quote, and one undo takes it back.
    page.keyboard.press("Alt+Enter")
    expect(composer).to_have_value("> ```\n> code\n> \n> ```")
    page.keyboard.press("ControlOrMeta+z")
    expect(composer).to_have_value("> ```\n> code\n> ```")
    page.keyboard.press("Alt+Enter")
    # On the empty last line Alt+Enter adds another line; Enter still exits there.
    page.keyboard.press("Alt+Enter")
    expect(composer).to_have_value("> ```\n> code\n> \n> \n> ```")
    page.keyboard.press("Enter")
    quoted = "> ```\n> code\n> \n> ```\n> "
    expect(composer).to_have_value(quoted)
    # In the quote's text, Alt+Enter is a bare line break, as Shift+Enter is.
    page.keyboard.type("said")
    page.keyboard.press("Alt+Enter")
    page.keyboard.type("more")
    draft = quoted + "said\nmore"
    expect(composer).to_have_value(draft)
    _assert_layer_aligned(page, composer, tmp_path, "alt-enter")
    page.wait_for_timeout(300)
    assert sent == [], sent
    page.keyboard.press("Enter")
    for _ in range(50):
        if sent:
            break
        page.wait_for_timeout(100)
    assert sent == [draft], sent


def test_typing_code_blocks_and_tidying_emphasis(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Typed ``` makes a block, Enter and ArrowDown leave it, and stray spaces tidy undoably."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    events_url = f"{base_url}/v1/sessions/{session_id}/events"
    sent: list[str] = []

    def capture(route: Any) -> None:
        sent.append(route.request.post_data_json["data"]["content"][0]["text"])
        route.fulfill(json={"queued": True, "item_id": "ci_md_edits"})

    page.route(events_url, capture)
    caret = lambda: composer.evaluate("t => t.selectionStart")  # noqa: E731
    composer.click()

    # ``` on an empty line becomes a block with the caret inside; Enter stays in it.
    page.keyboard.type("Intro")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("```")
    expect(composer).to_have_value("Intro\n```\n\n```")
    assert caret() == len("Intro\n```\n")
    page.keyboard.type("print(1)")
    page.keyboard.press("Enter")
    expect(composer).to_have_value("Intro\n```\nprint(1)\n\n```")
    assert sent == []
    # Enter on the empty last line exits below the closing fence.
    page.keyboard.press("Enter")
    expect(composer).to_have_value("Intro\n```\nprint(1)\n```\n")
    assert caret() == len("Intro\n```\nprint(1)\n```\n")
    # One undo returns to what was typed before the exit.
    page.keyboard.press("Control+z")
    expect(composer).to_have_value("Intro\n```\nprint(1)\n\n```")
    page.keyboard.press("Control+Shift+z")
    expect(composer).to_have_value("Intro\n```\nprint(1)\n```\n")
    # Arrow up onto the opening fence to add a language tag.
    composer.evaluate("t => t.setSelectionRange(9, 9)")
    page.keyboard.type("py")
    expect(composer).to_have_value("Intro\n```py\nprint(1)\n```\n")
    layer = composer.locator("xpath=..").locator(_LAYER)
    expect(layer.locator(f'[data-md="{_FENCE | _INFO}"]')).to_have_text("py")

    # ArrowDown on a block's last line leaves it, adding a line at the end.
    composer.fill("")
    page.keyboard.type("```")
    page.keyboard.type("x = 1")
    page.keyboard.press("ArrowDown")
    expect(composer).to_have_value("```\nx = 1\n```\n")
    assert caret() == len("```\nx = 1\n```\n")

    # Inside a quote, a code block keeps the quote on every line it adds.
    composer.fill("")
    page.keyboard.type("> ```")
    expect(composer).to_have_value("> ```\n> \n> ```")
    page.keyboard.type("x = **not bold**")
    page.keyboard.press("Enter")
    expect(composer).to_have_value("> ```\n> x = **not bold**\n> \n> ```")
    assert sent == []
    page.keyboard.press("Enter")
    expect(composer).to_have_value("> ```\n> x = **not bold**\n> ```\n> ")
    runs = _styled_runs(layer)
    assert all(not flags & _BOLD for _, flags in runs), runs

    # A stray space inside bold tidies as the first closing `*` completes it
    # (the second is already there, so it does nothing); undo restores the `*`
    # that was typed.
    composer.fill("")
    page.keyboard.type("** Is this Bold?!**")
    expect(composer).to_have_value("**Is this Bold?!**")
    expect(layer.locator(f'[data-md="{_BOLD}"]')).to_have_text("Is this Bold?!")
    page.keyboard.press("Control+z")
    expect(composer).to_have_value("** Is this Bold?!*")
    # Spaces on both sides trim too, and one undo restores exactly what was typed.
    composer.fill("")
    page.keyboard.type("say ** both sides **")
    expect(composer).to_have_value("say **both sides**")
    page.keyboard.press("Control+z")
    expect(composer).to_have_value("say ** both sides *")
    # A bullet inside a quote is never an opener.
    composer.fill("")
    page.keyboard.type("> * item*")
    expect(composer).to_have_value("> * item*")
    # Arithmetic is never tidied.
    composer.fill("")
    page.keyboard.type("2 ** 3 ** 4 and 2 * 3 * 4")
    expect(composer).to_have_value("2 ** 3 ** 4 and 2 * 3 * 4")
    # Nor is a paste.
    composer.fill("")
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("() => navigator.clipboard.writeText('** pasted**')")
    page.keyboard.press("Control+v")
    expect(composer).to_have_value("** pasted**")

    # What's sent is exactly the textarea's content, tidy included.
    composer.fill("")
    page.keyboard.type("Say __ this__ now")
    expect(composer).to_have_value("Say __this__ now")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(composer).to_have_value("")
    assert sent == ["Say __this__ now"], sent
    page.unroute(events_url)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_strikethrough_links_and_quotes(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
) -> None:
    """New styles paint without moving glyphs; their markers hide until edited."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(color_scheme=theme)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    draft = (
        "~~Old plan~~ is out; read [the docs](https://example.test/docs) first.\n"
        "> Quoted with **bold** and _emphasis_\n"
        "lazy line still quoted\n\n"
        "After the quote."
    )
    composer.fill(draft)
    layer = composer.locator("xpath=..").locator(_LAYER)
    _move_caret(composer, len(draft))
    _assert_layer_aligned(page, composer, tmp_path, f"styles-{theme}")
    shown = _shown_markers(layer)
    assert shown == [], shown
    url = layer.locator(f'[data-md="{_LINK_URL}"]')
    expect(url).to_have_text("https://example.test/docs")
    assert url.evaluate("s => getComputedStyle(s).color") != "rgba(0, 0, 0, 0)"
    strike = layer.locator(f'[data-md="{_STRIKE}"]')
    assert "linear-gradient" in strike.evaluate("s => getComputedStyle(s).backgroundImage")
    link = layer.locator(f'[data-md="{_LINK}"]')
    assert "linear-gradient" in link.evaluate("s => getComputedStyle(s).backgroundImage")
    # The `>` line and its lazy line are one block quote.
    rows = layer.locator(".composer-quote")
    expect(rows).to_have_count(2)
    assert [row.get_attribute("data-quote") for row in rows.all()] == ["first", "last"]
    expect(rows.last).to_contain_text("lazy line still quoted")
    card = page.locator("[data-composer-card]").filter(has=composer)
    card.screenshot(path=tmp_path / f"composer-styles-{theme}.png", animations="disabled")
    # The caret in a token reveals just its markers: the link's brackets, the quote's `>`.
    _move_caret(composer, draft.index("the docs") + 3)
    assert _shown_markers(layer) == ["[", "](", ")"]
    _move_caret(composer, draft.index("Quoted") + 2)
    assert _shown_markers(layer) == [">"]
    _move_caret(composer, draft.index("Old") + 1)
    assert _shown_markers(layer) == ["~~", "~~"]
    _assert_layer_aligned(page, composer, tmp_path, f"styles-revealed-{theme}")


# Each quote row's box (and the reply quote's blockquote, when there is one):
# edges, the styles it borrows, and the layer's text column.
_QUOTE_BLOCK_JS = """layer => {
    const edges = element => {
        const r = element.getBoundingClientRect();
        return [r.left, r.right, r.top, r.bottom];
    };
    const look = element => {
        const style = getComputedStyle(element);
        return { bar: [style.borderLeftWidth, style.borderLeftStyle, style.borderLeftColor],
            tint: style.backgroundColor,
            corners: [style.borderTopLeftRadius, style.borderBottomLeftRadius] };
    };
    const rows = Array.from(layer.querySelectorAll('.composer-quote'), row => {
        const box = row.querySelector('.composer-quote-box');
        // Plain quoted text (not a marker, bold or a slanted italic).
        const text = Array.from(row.querySelectorAll('[data-md]'))
            .find(span => Number(span.dataset.md) === 4096);
        return { place: row.dataset.quote, row: edges(row), box: edges(box), look: look(box),
            color: getComputedStyle(text).color };
    });
    const reply = document.querySelector('[data-testid="composer-reply-quote"] blockquote');
    const layerBox = layer.getBoundingClientRect();
    const gutter = parseFloat(getComputedStyle(layer).paddingLeft);
    return { rows, reply: reply && { ...look(reply), color: getComputedStyle(reply).color },
        text: [layerBox.left + gutter, layerBox.left + layer.clientWidth],
        lineHeight: parseFloat(getComputedStyle(layer).lineHeight) };
}"""

_QUOTE_DRAFT = (
    "Intro line.\n"
    "> First quoted line with **bold** and _emphasis_.\n"
    "> A second quoted line, long enough to wrap across the composer width: "
    + "more words to wrap "
    * 8
    + "\nlazy continuation line\n\n"
    "After the quote."
)


def _assert_quote_block(page: Page, textarea: Locator) -> dict[str, Any]:
    """The quote's rows form one box: a bar in the gutter, a seamless tint, rounded ends."""
    layer = textarea.locator("xpath=..").locator(_LAYER)
    _next_frame(page)
    block = layer.evaluate(_QUOTE_BLOCK_JS)
    rows = block["rows"]
    assert [row["place"] for row in rows] == ["first", "middle", "last"], block
    text_left, text_right = block["text"]
    for row in rows:
        left, right, top, bottom = row["box"]
        # It covers its row (every wrapped line) from 6px into the gutter to
        # the text column's right edge.
        assert left == pytest.approx(text_left - 6, abs=0.01), block
        assert right == pytest.approx(text_right, abs=0.01), block
        assert [top, bottom] == pytest.approx(row["row"][2:], abs=0.01), block
        assert row["look"]["bar"][:2] == ["2px", "solid"], block
    assert rows[1]["box"][3] - rows[1]["box"][2] > block["lineHeight"] * 1.5, "wrapped row"
    for previous, row in itertools.pairwise(rows):
        assert row["box"][2] == pytest.approx(previous["box"][3], abs=0.01), block
    corners = [row["look"]["corners"] for row in rows]
    assert corners[0][0] != "0px" and corners[0][1] == "0px", corners
    assert corners[1] == ["0px", "0px"], corners
    assert corners[2][0] == "0px" and corners[2][1] != "0px", corners
    # One tint down the gutter between bar and text, against the card just left
    # of the box (its own gradient cancels out): no seam between rows.
    left, top, bottom = rows[0]["box"][0], rows[0]["box"][2], rows[-1]["box"][3]
    radius = float(corners[0][0].removesuffix("px"))
    strip = Image.open(
        io.BytesIO(
            page.screenshot(
                clip={
                    "x": left - 3,
                    "y": top + radius,
                    "width": 8,
                    "height": bottom - top - 2 * radius,
                }
            )
        )
    ).convert("RGB")
    scale = strip.width / 8
    outside, inside = round(1 * scale), round(7 * scale)
    lifts = [
        tuple(
            a - b
            for a, b in zip(strip.getpixel((inside, y)), strip.getpixel((outside, y)), strict=True)
        )
        for y in range(strip.height)
    ]
    # The dark card dithers a level or two; a seam (a gap, or two tints
    # overlapping) would step the lift by far more on its row.
    medians = [sorted(channel)[len(channel) // 2] for channel in zip(*lifts, strict=True)]
    worst = max(abs(value - medians[c]) for lift in lifts for c, value in enumerate(lift))
    assert max(abs(median) for median in medians) >= 3, f"the tint shows: {medians}"
    assert worst <= 3, (worst, medians, sorted(set(lifts))[:8])
    return block


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_block_quote_wears_the_reply_quote_look(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
) -> None:
    """A typed quote is one reply-quote-styled block; caret and selection stay exact in it."""
    base_url, session_id = seeded_session
    reply_text = "A reply quote, picked from the transcript."
    seed_committed_turn(session_id, prompt="Say something to quote.", reply=reply_text)
    page.set_viewport_size({"width": 1024, "height": 844})
    page.emulate_media(color_scheme=theme)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_QUOTE_DRAFT)
    layer = composer.locator("xpath=..").locator(_LAYER)
    _move_caret(composer, len(_QUOTE_DRAFT))
    _assert_quote_block(page, composer)
    _assert_layer_aligned(page, composer, tmp_path, f"quote-{theme}")
    card = page.locator("[data-composer-card]").filter(has=composer)
    card.screenshot(path=tmp_path / f"composer-quote-{theme}.png", animations="disabled")

    # The caret in the quote shows its `>`, and every glyph stays on the textarea's.
    _move_caret(composer, _QUOTE_DRAFT.index("First") + 3)
    assert _shown_markers(layer) == [">"]
    _assert_quote_block(page, composer)
    _assert_layer_aligned(page, composer, tmp_path, f"quote-caret-{theme}")
    # A selection across the quote's lines paints over its glyphs, still aligned.
    start, end = _QUOTE_DRAFT.index("quoted line with"), _QUOTE_DRAFT.index("lazy") + 4
    unselected = _region(page, composer, start, end)
    composer.evaluate("(t, [s, e]) => t.setSelectionRange(s, e)", [start, end])
    expect(composer).to_have_js_property("selectionEnd", end)
    _next_frame(page)
    _assert_selection_visible(unselected, _region(page, composer, start, end))
    card.screenshot(path=tmp_path / f"composer-quote-selection-{theme}.png", animations="disabled")

    # Beside a real reply quote, the typed quote wears its bar, tint and text color.
    # (Replying moves a draft above the quote, so reply first, then type.)
    composer.fill("")
    _reply_to(page, page.locator('[data-role="assistant"]').get_by_text(reply_text, exact=True))
    expect(page.get_by_test_id("composer-reply-quote")).to_be_visible()
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    composer.fill(_QUOTE_DRAFT)
    expect(composer).to_have_value(_QUOTE_DRAFT)
    _move_caret(composer, len(_QUOTE_DRAFT))
    block = _assert_quote_block(page, composer)
    reply = block["reply"]
    assert reply is not None, block
    for row in block["rows"]:
        assert row["look"]["bar"] == reply["bar"], (row, reply)
        assert row["look"]["tint"] == reply["tint"], (row, reply)
        assert row["color"] == reply["color"], (row, reply)
    assert block["rows"][0]["look"]["corners"][0] == reply["corners"][0], block
    assert block["rows"][-1]["look"]["corners"][1] == reply["corners"][1], block
    _assert_layer_aligned(page, composer, tmp_path, f"quote-beside-reply-{theme}")
    card.screenshot(
        path=tmp_path / f"composer-quote-beside-reply-{theme}.png", animations="disabled"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_right_to_left_italics_fall_back_without_drift(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
) -> None:
    """Hebrew, Arabic and mixed-direction italics keep the dotted underline and stay aligned."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1024, "height": 844})
    page.emulate_media(color_scheme=theme)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    draft = "\n".join(
        [
            "before _שלום עולם_ after",
            "قبل _مرحبا بالعالم_ بعد",
            "mixed _hello שלום_ direction",
            "an LTR word _slanted_ beside עברית on one line",
            "and a plain left-to-right _slanted line_ here",
        ]
    )
    composer.fill(draft)
    layer = composer.locator("xpath=..").locator(_LAYER)
    expect(layer.locator("[data-slanted]").first).to_be_attached()
    _assert_layer_aligned(page, composer, tmp_path, f"rtl-{theme}")
    italics = layer.evaluate(_ITALIC_STATES_JS)
    by_text = {span["text"]: span for span in italics}
    # Any line with right-to-left text keeps the fallback, its glyphs visible.
    for text in ("שלום עולם", "مرحبا بالعالم", "hello שלום", "slanted"):
        span = by_text[text]
        assert not span["slanted"] and span["dots"], span
        assert span["color"] != "rgba(0, 0, 0, 0)", span
    assert by_text["slanted line"]["slanted"], italics
    # The one slanted copy sits exactly on its glyphs.
    _assert_italics_slanted(page, composer, tmp_path, f"rtl-{theme}")
    card = page.locator("[data-composer-card]").filter(has=composer)
    card.screenshot(path=tmp_path / f"composer-rtl-{theme}.png", animations="disabled")


_ITALIC_STATES_JS = """layer => Array.from(layer.querySelectorAll('.md-em'), span => ({
    text: span.textContent,
    slanted: span.hasAttribute('data-slanted'),
    color: getComputedStyle(span).color,
    dots: getComputedStyle(span).backgroundImage.includes('linear-gradient'),
}))"""


def test_landing_and_side_chat_composers_style_markdown(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """The New Chat landing and side-chat composers share the live highlight layer."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/")
    landing = page.get_by_test_id("new-chat-landing-input")
    expect(landing).to_be_visible(timeout=30_000)
    landing.click(position={"x": 8, "y": 8})
    _type_lines(page, _FORMATTED_LINES)
    expect(landing).to_have_value(_FORMATTED_DRAFT)
    _assert_formatted(landing.locator("xpath=..").locator(_LAYER))
    _assert_layer_aligned(page, landing, tmp_path, "landing")

    # Otto's eyes still follow the caret through the transparent textarea.
    pupil = page.locator("g.otto-pupil").first
    expect(pupil).to_be_attached()
    landing.evaluate("t => { t.focus(); t.setSelectionRange(0, 0); }")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowLeft")
    _next_frame(page)
    page.wait_for_timeout(200)
    at_start = pupil.evaluate("p => p.style.transform")
    page.keyboard.press("Control+End")
    page.wait_for_function(
        "([p, before]) => p.style.transform !== before",
        arg=[pupil.element_handle(), at_start],
    )

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new", exact=True).click()
    page.get_by_role("menuitem", name="Side chat", exact=True).click()
    side = page.get_by_test_id("side-chat-input")
    expect(side).to_be_enabled(timeout=30_000)
    side.fill(_FORMATTED_DRAFT)
    _assert_formatted(side.locator("xpath=..").locator(_LAYER))
    _assert_layer_aligned(page, side, tmp_path, "side-chat")


# Where one character of a view's text starts and ends, by its offset in the
# view's own text (the textarea layer's or the compact preview's).
_CHAR_RECT_JS = """([root, at]) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (at < node.length) {
            const range = document.createRange();
            range.setStart(node, at);
            range.setEnd(node, at + 1);
            const r = range.getClientRects()[0];
            return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
        }
        at -= node.length;
    }
    return null;
}"""


def _char_rect(view: Locator, text: str, needle: str, offset: int = 0) -> dict[str, float]:
    """The box of ``needle[offset]`` in ``view``, whose text content is ``text``."""
    rect = view.evaluate(
        _CHAR_RECT_JS.replace("([root, at])", "(root, at)"), text.index(needle) + offset
    )
    assert rect is not None, (needle, text)
    return rect


def _blur_composer(page: Page) -> None:
    """Click away from the composer on the transcript, as a reader does."""
    box = page.locator('[role="log"]').bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + 8, box["y"] + box["height"] / 2)
    _next_frame(page)
    _next_frame(page)


_COMPACT_DRAFT = "Make **bold** and `code` then _it_ now.\n```\nx = 1\n```\n> quoted line\nend"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_compact_preview_closes_marker_gaps_and_clicks_place_the_caret(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
    theme: str,
) -> None:
    """Not editing, markers take no space; a click edits exactly where it lands."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(color_scheme=theme)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_COMPACT_DRAFT)
    layer = composer.locator("xpath=..").locator(_LAYER)
    view = _compact_view(composer)
    expect(view).to_have_count(0)
    card = page.locator("[data-composer-card]").filter(has=composer)
    card.screenshot(path=tmp_path / f"composer-editing-{theme}.png", animations="disabled")
    source = _COMPACT_DRAFT

    def gaps(root: Locator, text: str) -> dict[str, float]:
        return {
            # From a bold word's last letter to the next word's first.
            "bold": _char_rect(root, text, " and", 1)["left"]
            - _char_rect(root, text, "bold", 3)["right"],
            # From the code span's last letter to the next word.
            "code": _char_rect(root, text, " then", 1)["left"]
            - _char_rect(root, text, "code", 3)["right"],
        }

    editing = gaps(layer, source)
    code_box = layer.locator("[data-code-block]")
    editing_box = code_box.bounding_box()
    line_height = composer.evaluate("t => parseFloat(getComputedStyle(t).lineHeight)")

    _blur_composer(page)
    expect(view).to_be_visible()
    shown = "Make bold and code then it now.\nx = 1\nquoted line\nend"
    assert view.evaluate("v => v.textContent") == shown
    # Not lined up with the textarea, so bold is the sent message's semibold,
    # with no faux-bold stroke; italic is the italic face.
    faces = view.evaluate(
        """v => Object.fromEntries(Array.from(v.querySelectorAll('[data-md]'), s => {
            const c = getComputedStyle(s);
            return [s.textContent, [c.fontWeight, c.fontStyle, c.webkitTextStrokeWidth]];
        }))"""
    )
    assert faces["bold"] == ["600", "normal", "0px"], faces
    assert faces["it"] == ["400", "italic", "0px"], faces
    card.screenshot(path=tmp_path / f"composer-compact-{theme}.png", animations="disabled")
    compact = gaps(view, shown)
    space = _char_rect(view, shown, " and")["right"] - _char_rect(view, shown, " and")["left"]
    # The hidden `**` widths are gone: just the space remains. The code pill's
    # own padding (0.2em) stands in for its narrower-than-backtick edge.
    assert editing["bold"] > space + 8, (editing, compact, space)
    assert compact["bold"] == pytest.approx(space, abs=0.5), (compact, space)
    assert compact["code"] < editing["code"], (editing, compact)
    # The code box holds only its code: no empty fence lines above or below.
    box = view.locator("[data-code-block]").bounding_box()
    assert editing_box and box
    assert editing_box["height"] >= line_height * 3 - 1, editing_box
    assert box["height"] <= line_height + 10, (box, line_height)
    # Only its padding (and the line's half-leading) above and below the code.
    code = _char_rect(view, shown, "x = 1")
    gaps_around = [code["top"] - box["y"], box["y"] + box["height"] - code["bottom"]]
    assert max(gaps_around) < line_height / 2, (gaps_around, line_height)

    # A click on the preview edits the character under it.
    target = _char_rect(view, shown, "code", 2)
    page.mouse.click(target["left"] + 1, (target["top"] + target["bottom"]) / 2)
    expect(composer).to_be_focused()
    expect(view).to_have_count(0)
    assert composer.evaluate("t => t.selectionStart") == source.index("code") + 2
    page.keyboard.type("Z")
    expected = source.replace("code", "coZde", 1)
    expect(composer).to_have_value(expected)

    # After a bold word: past its `**`, so typing continues plain.
    _blur_composer(page)
    shown = shown.replace("code", "coZde", 1)
    after = _char_rect(view, shown, "bold", 3)
    page.mouse.click(after["right"] - 0.5, (after["top"] + after["bottom"]) / 2)
    expect(composer).to_be_focused()
    assert composer.evaluate("t => t.selectionStart") == expected.index("** and") + 2
    # In the quote, on its text: after the hidden `> `.
    _blur_composer(page)
    quoted = _char_rect(view, shown, "quoted")
    page.mouse.click(quoted["left"] + 0.5, (quoted["top"] + quoted["bottom"]) / 2)
    assert composer.evaluate("t => t.selectionStart") == expected.index("quoted")

    # Sent byte for byte as typed.
    sent: list[str] = []

    def capture(route: Any) -> None:
        sent.append(route.request.post_data_json["data"]["content"][0]["text"])
        route.fulfill(json={"queued": True, "item_id": "ci_md_compact"})

    page.route(f"{base_url}/v1/sessions/{session_id}/events", capture)
    _blur_composer(page)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(composer).to_have_value("")
    assert sent == [expected], sent


def test_compact_preview_keeps_the_transcript_pinned_and_the_draft_scroll(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Its height change neither jumps the transcript nor loses the draft's scroll."""
    base_url, session_id = seeded_session
    reply = "\n\n".join(f"Paragraph {i} of a long reply to scroll." for i in range(40))
    seed_committed_turn(session_id, prompt="Say a lot.", reply=reply)
    page.set_viewport_size({"width": 1280, "height": 700})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(page.get_by_text("Paragraph 39 of a long reply to scroll.")).to_be_visible(
        timeout=30_000
    )
    lines = [f"line {i} with **bold** and `code`" for i in range(30)]
    composer.fill("\n".join(lines))
    composer.evaluate("t => { t.scrollTop = t.scrollHeight; }")
    assert page.evaluate(_TAG_TRANSCRIPT_SCROLLER)
    _next_frame(page)
    before = page.evaluate(_TRANSCRIPT_FROM_BOTTOM)
    assert before <= 2, before
    composer_top = composer.bounding_box()["y"]

    _blur_composer(page)
    view = _compact_view(composer)
    expect(view).to_be_visible()
    # Pinned to the bottom through the height change, and the preview opens at
    # the draft's own scroll position (the end), within its height cap.
    assert page.evaluate(_TRANSCRIPT_FROM_BOTTOM) <= 2
    assert view.evaluate("v => v.scrollHeight - v.clientHeight - v.scrollTop") <= 1
    assert view.evaluate("v => v.clientHeight") <= composer.evaluate("t => t.clientHeight") + 1

    # Keyboard focus comes back to the same scroll and caret.
    caret = composer.evaluate("t => t.selectionStart")
    composer.focus()
    expect(view).to_have_count(0)
    _next_frame(page)
    assert page.evaluate(_TRANSCRIPT_FROM_BOTTOM) <= 2
    assert composer.evaluate("t => t.scrollHeight - t.clientHeight - t.scrollTop") <= 1
    assert composer.evaluate("t => t.selectionStart") == caret
    assert composer.bounding_box()["y"] == pytest.approx(composer_top, abs=1)


_TAG_TRANSCRIPT_SCROLLER = """() => {
    const log = document.querySelector('[role="log"]');
    let best = null;
    log.querySelectorAll('*').forEach((el) => {
        const scrolls = el.scrollHeight > el.clientHeight + 4;
        if (scrolls && (!best || el.scrollHeight > best.scrollHeight)) best = el;
    });
    (best || log).setAttribute('data-pw-transcript', '');
    return (best || log).scrollHeight > (best || log).clientHeight + 4;
}"""
_TRANSCRIPT_FROM_BOTTOM = """() => {
    const el = document.querySelector('[data-pw-transcript]');
    return el.scrollHeight - el.clientHeight - el.scrollTop;
}"""


@_TOUCH
def test_compact_preview_first_tap_focuses_with_the_caret_there(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """On a touch screen the first tap on the preview edits where it lands."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    draft = "Tap **bold** or `code` here"
    composer.fill(draft)
    composer.evaluate("t => t.blur()")
    view = _compact_view(composer)
    expect(view).to_be_visible()
    shown = "Tap bold or code here"
    target = _char_rect(view, shown, "here", 2)
    page.touchscreen.tap(target["left"] + 1, (target["top"] + target["bottom"]) / 2)
    expect(composer).to_be_focused()
    assert composer.evaluate("t => t.selectionStart") == draft.index("here") + 2


_HUGE_COMPACT_DRAFT = "\n".join(
    f"{i}. item with **bold**, `code` and _em_" if i % 50 else f"```\ncode {i}\n```"
    for i in range(5000)
)


def _set_draft(composer: Locator, draft: str) -> None:
    """Load a large draft at once (typing or fill would take minutes)."""
    composer.evaluate(
        """(t, v) => {
        const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
        t.focus(); set.call(t, v); t.dispatchEvent(new Event('input', { bubbles: true }));
    }""",
        draft,
    )
    expect(composer).to_have_value(draft)


# The text and draft line of the preview row near its top or bottom edge.
_PREVIEW_ROW_AT_JS = """([view, edge]) => {
    const box = view.getBoundingClientRect();
    const y = edge === 'top' ? box.top + 3 : box.bottom - 3;
    const row = document.elementFromPoint(box.left + 40, y)?.closest('[data-row]');
    if (!row || !view.contains(row)) return null;
    return [row.textContent, row.dataset.line ? Number(row.dataset.line) : null];
}"""


@pytest.mark.parametrize("where", ["middle", "end"])
def test_compact_preview_opens_at_the_line_the_textarea_showed(
    page: Page,
    seeded_session: tuple[str, str],
    where: str,
) -> None:
    """Blurring a long, scrolled draft keeps the same lines in view."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    draft = _HUGE_COMPACT_DRAFT
    _set_draft(composer, draft)
    lines = draft.split("\n")
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    line_height = composer.evaluate("t => parseFloat(getComputedStyle(t).lineHeight)")
    if where == "middle":
        # An item line near the middle at the textarea's top (no line wraps here).
        top = next(k for k in range(len(lines) // 2, len(lines)) if lines[k][0].isdigit())
        composer.evaluate("(t, y) => { t.scrollTop = y; }", top * line_height)
    else:
        composer.evaluate("t => { t.scrollTop = t.scrollHeight; }")
    _next_frame(page)
    composer.evaluate("t => t.blur()")
    view = _compact_view(composer)
    expect(view).to_be_visible()
    _next_frame(page)
    _next_frame(page)
    if where == "middle":
        row = view.evaluate(_PREVIEW_ROW_AT_JS.replace("([view, edge])", "(view, edge)"), "top")
        number = lines[top].split(".")[0]
        assert row == [f"{number}. item with bold, code and em\n", starts[top]], lines[top]
    else:
        row = view.evaluate(_PREVIEW_ROW_AT_JS.replace("([view, edge])", "(view, edge)"), "bottom")
        assert row == ["4999. item with bold, code and em", starts[-1]], lines[-1]
        assert view.evaluate("v => v.scrollHeight - v.clientHeight - v.scrollTop") <= 1


def test_compact_preview_toggles_quickly_on_a_huge_draft(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Focus changes on a 5,000-line draft switch views in well under a second."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    expect(composer).to_be_visible(timeout=30_000)
    draft = _HUGE_COMPACT_DRAFT
    _set_draft(composer, draft)
    # Where a writer is: caret at the end, scrolled to it (a focus would jump
    # there otherwise, and time the jump's paint rather than the switch).
    composer.evaluate("t => { t.setSelectionRange(t.value.length, t.value.length); }")
    composer.evaluate("t => { t.scrollTop = t.scrollHeight; }")
    _next_frame(page)
    timings = composer.evaluate(
        """async t => {
            const frames = () => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const field = t.parentElement;
            const until = async (check) => {
                while (!check()) await new Promise(resolve => setTimeout(resolve, 0));
            };
            const out = { blur: [], focus: [] };
            for (let i = 0; i < 3; i++) {
                let start = performance.now();
                t.blur();
                await until(() => field.hasAttribute('data-compact'));
                await frames();
                out.blur.push(performance.now() - start);
                start = performance.now();
                t.focus();
                await until(() => !field.hasAttribute('data-compact'));
                await frames();
                out.focus.push(performance.now() - start);
            }
            return out;
        }"""
    )
    print(f"Compact preview toggle, 5,000 lines: {timings}")
    # Generous bounds for a loaded CI host; the numbers are reported above.
    assert max(timings["blur"]) < 2000, timings
    assert max(timings["focus"]) < 1000, timings
    expect(composer).to_have_value(draft)
