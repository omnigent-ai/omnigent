"""E2E: the composer geometry contract shared by the landing and live surfaces.

This module records the layout contract the composer layout normalization
must satisfy, plus a few passing characterizations of the current layout
that must keep holding afterwards.

The target contract (asserted here, enabled when the layout primitives
land):

- The composer cards on the landing and live-chat surfaces share a max-width
  and horizontal centering; each card's left/right insets are symmetric
  within 1 CSS px.
- The input text, attachment rows, mention-chip rows, feedback/error rows,
  and action-row content align to shared left/right inset lines within 1 px.
- Controls in the same row share a vertical center within 1 px; nothing
  overlaps, clips, or creates horizontal overflow.
- Label collapse (``data-labels=collapsed``) changes visibility only: the
  Send/Stop button does not shift and row centering is preserved.
- Composer-owned lists (attachment rows, mention chips, slash-completion
  suggestions, queued-message rows) follow the row grid
  ``| icon/status | primary label (flex) | metadata | trailing action |``:
  corresponding columns across sibling rows share x within 1 px, row
  padding/gaps are identical, single-line rows share height, wrapped rows
  keep first-line baseline and trailing-action alignment, and
  focus/hover/selected backgrounds have equal left/right insets.

Mention chips are custom spans rendered inside ``ComposerChipRow`` (not
``ComposerAttachments`` file tiles), so an explicit mention-chip test owns
their geometry; the vitest parity suites own mention behavior.

The workspace bar is a tray docked above the card, not a content row: it
nests inside the card's outer edges by the same 12px inset the card's
content rows align to (symmetric on both sides), and its chips sit on the
card's content inset line. The queued-messages strip shares the tray's
inset column. This nesting is the documented intent encoded in the
workspace-bar tests below.

Harness notes:

- Live-chat geometry uses the standard ``seeded_session`` and, where a long
  model/effort label is needed, the same session-snapshot route patch as
  ``mobile/test_composer_model_label_stop_overlap.py``. The queued-row grid
  holds a turn open with the mock LLM's ``block`` gate so rows genuinely
  accumulate in the strip.
- Landing geometry drives a fresh async browser (route-patched create flow)
  exactly like ``_drive_landing_collapse`` in the mobile module; the create
  POST is stubbed to return the seeded session id so no real session spawns.
"""

from __future__ import annotations

import json
import re
from itertools import pairwise
from urllib.parse import urlparse

from playwright.async_api import Page as AsyncPage
from playwright.async_api import async_playwright
from playwright.async_api import expect as async_expect
from playwright.sync_api import FloatRect, Page, Route, expect

from tests.e2e_ui.conftest import (
    configure_mock_llm,
    fetch_with_retry,
    reset_mock_llm,
)
from tests.e2e_ui.mobile.test_composer_model_label_stop_overlap import (
    _assert_same_row,
    _box,
    _intersection,
    _patch_session_as_databricks_claude_native,
    _release_gates,
    _screenshot,
)
from tests.e2e_ui.start_session.test_model_flows_prelaunch import _CLAUDE_HOST_ROWS
from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _register_common_routes,
    _run_in_fresh_loop,
)

_PHONE = {"width": 390, "height": 664}
_TABLET = {"width": 768, "height": 1024}
_DESKTOP = {"width": 1280, "height": 852}
_WIDE = {"width": 1800, "height": 900}
_WIDTHS = [_PHONE, _TABLET, _DESKTOP]

_TOL = 1.0


def _assert_symmetric_insets(card: FloatRect, container: FloatRect, tol: float = _TOL) -> None:
    """Fail unless the card's left and right insets inside its centering
    container match within ``tol`` px."""
    left = card["x"] - container["x"]
    right = container["x"] + container["width"] - (card["x"] + card["width"])
    assert abs(left - right) <= tol, (
        f"asymmetric card insets: left={left:.2f}px right={right:.2f}px "
        f"(card at x={card['x']:.2f} w={card['width']:.2f}, container at "
        f"x={container['x']:.2f} w={container['width']:.2f})"
    )


def _assert_same_vertical_center(a: FloatRect, b: FloatRect, tol: float = _TOL) -> None:
    """Fail unless two boxes share a vertical center within ``tol`` px."""
    a_center = a["y"] + a["height"] / 2
    b_center = b["y"] + b["height"] / 2
    assert abs(a_center - b_center) <= tol, (
        f"controls do not share a vertical center: {a_center:.2f} vs {b_center:.2f}"
    )


def _assert_no_overlap(a: FloatRect, b: FloatRect, tol: float = _TOL) -> None:
    """Fail when two boxes intersect by more than ``tol`` px on both axes."""
    x_overlap, y_overlap = _intersection(a, b)
    assert not (x_overlap > tol and y_overlap > tol), (
        f"elements overlap by {x_overlap:.2f}x{y_overlap:.2f}px"
    )


def _assert_within_viewport(box: FloatRect, viewport_width: int, tol: float = _TOL) -> None:
    """Fail unless the box is fully inside the viewport horizontally."""
    assert box["x"] >= -tol and box["x"] + box["width"] <= viewport_width + tol, (
        f"element overflows the {viewport_width}px viewport: x={box['x']:.2f} w={box['width']:.2f}"
    )


# Picker menus open with a 150ms zoom/fade animation; measuring row geometry
# mid-flight reads divergent scales on sibling rows. Awaiting every animation
# on the menu subtree pins the settled layout.
_MENU_SETTLE_JS = (
    "(el) => Promise.all("
    "el.getAnimations({ subtree: true }).map((a) => a.finished.catch(() => {}))"
    ")"
)

# The layout primitive every inset is pinned against (px-3 / mx-3 in
# ChatComposer's contract): 12px, plus whatever border separates the measured
# box from the reference's outer edge (the card's 1px border for content
# insets; the tray's own 1px border for its content).
_PRIMITIVE_INSET_PX = 12.0


def _assert_primitive_inset(measured: float, border_px: float, what: str) -> None:
    """Fail unless ``measured`` matches the 12px primitive plus ``border_px``."""
    expected = _PRIMITIVE_INSET_PX + border_px
    assert abs(measured - expected) <= _TOL, (
        f"{what} inset {measured:.2f}px off the shared primitive: expected "
        f"{expected:.2f}px (12px inset + {border_px:.0f}px border)"
    )


def _border_left_px(locator) -> float:
    """Computed left border width (px) of a sync locator's element."""
    value = locator.evaluate("(el) => getComputedStyle(el).borderLeftWidth")
    return float(value.removesuffix("px"))


async def _border_left_px_async(locator) -> float:
    """Computed left border width (px) of an async locator's element."""
    value = await locator.evaluate("(el) => getComputedStyle(el).borderLeftWidth")
    return float(value.removesuffix("px"))


def _right_inset(card: FloatRect, box: FloatRect) -> float:
    """The gap between a box's right edge and its card's right edge."""
    return card["x"] + card["width"] - (box["x"] + box["width"])


def _assert_row_grid(boxes: list[FloatRect], tol: float = _TOL) -> None:
    """Fail unless sibling list rows share x, width, and height within ``tol``
    and stack evenly (the ``| icon | label (flex) | metadata | trailing |``
    grid's shared columns and single-line row height)."""
    first = boxes[0]
    for box in boxes[1:]:
        assert abs(box["x"] - first["x"]) <= tol, (
            f"row x diverges: {first['x']:.2f} vs {box['x']:.2f}"
        )
        assert abs(box["width"] - first["width"]) <= tol, (
            f"row width diverges: {first['width']:.2f} vs {box['width']:.2f}"
        )
        assert abs(box["height"] - first["height"]) <= tol, (
            f"row height diverges: {first['height']:.2f} vs {box['height']:.2f}"
        )
    for a, b in pairwise(boxes):
        pitch = b["y"] - a["y"]
        assert abs(pitch - a["height"]) <= tol, (
            f"rows are not stacked evenly: pitch {pitch:.2f}px vs height {a['height']:.2f}px"
        )


def _attach_files(page: Page | AsyncPage, files: list[tuple[str, str, bytes]]) -> None:
    """Attach files through the composer's hidden file input.

    :param page: The page with a visible composer.
    :param files: ``(name, mime, content)`` triples for ``set_input_files``.
    :returns: None.
    """
    payload = [
        {"name": name, "mimeType": mime, "buffer": content} for name, mime, content in files
    ]
    page.locator('input[type="file"]').first.set_input_files(payload)


async def _attach_files_async(page: AsyncPage, files: list[tuple[str, str, bytes]]) -> None:
    """Async-page variant of :func:`_attach_files`."""
    payload = [
        {"name": name, "mimeType": mime, "buffer": content} for name, mime, content in files
    ]
    await page.locator('input[type="file"]').first.set_input_files(payload)


def _chip_boxes(page: Page, names: list[str]) -> list[FloatRect]:
    """Bounding boxes of the named attachment chips (their tiles, located via
    the remove button's parent — the button itself is corner-positioned)."""
    return [
        _box(page.get_by_role("button", name=f"Remove {name}").locator("..")) for name in names
    ]


async def _chip_boxes_async(page: AsyncPage, names: list[str]) -> list[FloatRect]:
    """Async-page variant of :func:`_chip_boxes`."""
    boxes = []
    for name in names:
        box = await page.get_by_role("button", name=f"Remove {name}").locator("..").bounding_box()
        assert box is not None, f"chip {name} has no bounding box"
        boxes.append(box)
    return boxes


async def _open_landing(
    page: AsyncPage,
    base_url: str,
    session_id: str,
    *,
    agents_body: str | None = None,
) -> None:
    """Route-stub and open the landing composer on a fresh async page."""
    await _register_common_routes(
        page, created_session_id=session_id, create_bodies=[], agents_body=agents_body
    )
    await page.route(
        re.compile(r"/v1/sessions\?"),
        lambda route: route.fulfill(json={"data": [], "has_more": False}),
    )
    await page.route(
        f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
        lambda route: route.fulfill(json={"models": _CLAUDE_HOST_ROWS}),
    )
    await page.goto(f"{base_url}/")
    await async_expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)


def _skilled_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: one Claude agent carrying skills, so
    the landing slash menu has real rows to measure."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_skilled_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": "claude-native",
                    "skills": [
                        {"name": "review-pr", "description": "Review a pull request"},
                        {"name": "cross-review", "description": "Cross-vendor review"},
                        {"name": "deslop", "description": "Remove AI slop"},
                    ],
                }
            ]
        }
    )


def _open_live(page: Page, base_url: str, session_id: str, viewport: dict[str, int]) -> None:
    """Open the live-chat composer at ``viewport`` and wait for its row."""
    page.set_viewport_size(viewport)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("composer-action-row")).to_be_visible(timeout=15_000)


# ---------------------------------------------------------------------------
# Passing characterizations of current behavior (must keep holding)
# ---------------------------------------------------------------------------


def test_landing_composer_never_overflows_the_viewport(
    seeded_session: tuple[str, str],
) -> None:
    """At phone, tablet, and desktop widths the landing card, its action row,
    and the submit button stay fully inside the viewport."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_overflow(base_url, session_id))


async def _landing_overflow(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_PHONE)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id)
            for viewport in _WIDTHS:
                await page.set_viewport_size(viewport)
                card = await page.locator("[data-composer-card]").bounding_box()
                assert card is not None
                for box in [
                    card,
                    await page.get_by_test_id("new-chat-landing-actions").bounding_box(),
                    await page.get_by_test_id("new-chat-landing-submit").bounding_box(),
                ]:
                    assert box is not None
                    assert box["x"] >= -_TOL
                    assert box["x"] + box["width"] <= viewport["width"] + _TOL
        finally:
            await context.close()
            await browser.close()


def test_live_composer_never_overflows_the_viewport(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """At phone, tablet, and desktop widths the live card, its action row, and
    the Send button stay fully inside the viewport."""
    base_url, session_id = seeded_session
    for viewport in _WIDTHS:
        _open_live(page, base_url, session_id, viewport)
        card = _box(page.locator("[data-composer-card]"))
        _assert_within_viewport(card, viewport["width"])
        _assert_within_viewport(
            _box(page.get_by_test_id("composer-action-row")), viewport["width"]
        )
        _assert_within_viewport(
            _box(page.get_by_role("button", name="Send", exact=True)), viewport["width"]
        )


# ---------------------------------------------------------------------------
# Target contract — the layout the composer primitives normalize to
# ---------------------------------------------------------------------------


def test_landing_card_centered_with_symmetric_insets(seeded_session: tuple[str, str]) -> None:
    """The landing card is horizontally centered with left/right insets
    symmetric within 1 CSS px at every width."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_symmetry(base_url, session_id))


async def _landing_symmetry(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_PHONE)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id)
            for viewport in _WIDTHS:
                await page.set_viewport_size(viewport)
                card_loc = page.locator("[data-composer-card]")
                card = await card_loc.bounding_box()
                container = await card_loc.locator("..").bounding_box()
                assert card is not None and container is not None
                _assert_symmetric_insets(card, container)
        finally:
            await context.close()
            await browser.close()


def test_live_card_centered_with_symmetric_insets(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The live card is horizontally centered with left/right insets symmetric
    within 1 CSS px at every width."""
    base_url, session_id = seeded_session
    for viewport in _WIDTHS:
        _open_live(page, base_url, session_id, viewport)
        card_loc = page.locator("[data-composer-card]")
        card = _box(card_loc)
        container = _box(card_loc.locator(".."))
        _assert_symmetric_insets(card, container)


def test_landing_content_aligns_to_shared_inset_lines(seeded_session: tuple[str, str]) -> None:
    """Landing input, attachment chips, error row, and action-row content sit
    on shared left/right inset lines within 1 px."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_inset_lines(base_url, session_id))


async def _landing_inset_lines(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_DESKTOP)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id)
            await _attach_files_async(
                page,
                [
                    ("notes.txt", "text/plain", b"hello"),
                    ("clip.mp4", "video/mp4", b"\x00" * 10),
                ],
            )
            await async_expect(
                page.get_by_test_id("new-chat-landing-attachment-error")
            ).to_be_visible()
            card = await page.locator("[data-composer-card]").bounding_box()
            assert card is not None
            input_box = await page.get_by_test_id("new-chat-landing-input").bounding_box()
            error_box = await page.get_by_test_id(
                "new-chat-landing-attachment-error"
            ).bounding_box()
            chip = (
                await page.get_by_role("button", name="Remove notes.txt")
                .locator("..")
                .bounding_box()
            )
            leading = await page.get_by_test_id("new-chat-landing-attach").bounding_box()
            submit = await page.get_by_test_id("new-chat-landing-submit").bounding_box()
            assert (
                input_box is not None
                and error_box is not None
                and chip is not None
                and leading is not None
                and submit is not None
            )
            lefts = {
                "input": input_box["x"] - card["x"],
                "error row": error_box["x"] - card["x"],
                "attachment chip": chip["x"] - card["x"],
                "action row leading": leading["x"] - card["x"],
            }
            reference = lefts["input"]
            border = await _border_left_px_async(page.locator("[data-composer-card]"))
            _assert_primitive_inset(reference, border, "landing content")
            for name, inset in lefts.items():
                assert abs(inset - reference) <= _TOL, (
                    f"{name} left inset {inset:.2f}px diverges from the input's {reference:.2f}px"
                )
            input_right = _right_inset(card, input_box)
            assert abs(_right_inset(card, submit) - input_right) <= _TOL, (
                f"submit right inset {_right_inset(card, submit):.2f}px diverges from "
                f"the input's {input_right:.2f}px"
            )
        finally:
            await context.close()
            await browser.close()


def test_live_content_aligns_to_shared_inset_lines(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Live input, attachment chips, error row, and action-row content sit on
    shared left/right inset lines within 1 px."""
    base_url, session_id = seeded_session
    _open_live(page, base_url, session_id, _DESKTOP)
    _attach_files(
        page,
        [("notes.txt", "text/plain", b"hello"), ("clip.mp4", "video/mp4", b"\x00" * 10)],
    )
    expect(page.get_by_text(re.compile("can't be attached"))).to_be_visible()
    card = _box(page.locator("[data-composer-card]"))
    input_box = _box(page.get_by_label("Message the agent"))
    error_box = _box(page.get_by_text(re.compile("can't be attached")))
    chip = _box(page.get_by_role("button", name="Remove notes.txt").locator(".."))
    leading = _box(page.get_by_test_id("composer-attach"))
    submit = _box(page.get_by_role("button", name="Send", exact=True))
    lefts = {
        "input": input_box["x"] - card["x"],
        "error row": error_box["x"] - card["x"],
        "attachment chip": chip["x"] - card["x"],
        "action row leading": leading["x"] - card["x"],
    }
    reference = lefts["input"]
    _assert_primitive_inset(
        reference, _border_left_px(page.locator("[data-composer-card]")), "live content"
    )
    for name, inset in lefts.items():
        assert abs(inset - reference) <= _TOL, (
            f"{name} left inset {inset:.2f}px diverges from the input's {reference:.2f}px"
        )
    input_right = _right_inset(card, input_box)
    assert abs(_right_inset(card, submit) - input_right) <= _TOL, (
        f"submit right inset {_right_inset(card, submit):.2f}px diverges from the "
        f"input's {input_right:.2f}px"
    )


def test_landing_action_row_controls_share_a_vertical_center(
    seeded_session: tuple[str, str],
) -> None:
    """Every landing action-row control shares one vertical center within
    1 px; nothing overlaps or overflows."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_row_centers(base_url, session_id))


async def _landing_row_centers(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_DESKTOP)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id)
            controls = [
                await page.get_by_test_id("new-chat-landing-attach").bounding_box(),
                await page.get_by_test_id("new-chat-landing-permission-chip").bounding_box(),
                await page.get_by_test_id("new-chat-landing-agent-select").bounding_box(),
                await page.get_by_test_id("new-chat-landing-submit").bounding_box(),
            ]
            assert all(box is not None for box in controls)
            boxes = [box for box in controls if box is not None]
            for a, b in pairwise(boxes):
                _assert_same_vertical_center(a, b)
                _assert_no_overlap(a, b)
                _assert_within_viewport(b, _DESKTOP["width"])
            row = await page.get_by_test_id("new-chat-landing-actions").bounding_box()
            assert row is not None
            _assert_within_viewport(row, _DESKTOP["width"])
        finally:
            await context.close()
            await browser.close()


def test_live_action_row_controls_share_a_vertical_center(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Every live action-row control shares one vertical center within 1 px;
    nothing overlaps or overflows."""
    base_url, session_id = seeded_session
    _open_live(page, base_url, session_id, _DESKTOP)
    boxes = [
        _box(page.get_by_test_id("composer-attach")),
        _box(page.get_by_test_id("composer-config-gear")),
        _box(page.get_by_role("button", name="Send", exact=True)),
    ]
    for a, b in pairwise(boxes):
        _assert_same_vertical_center(a, b)
        _assert_no_overlap(a, b)
        _assert_within_viewport(b, _DESKTOP["width"])
    _assert_within_viewport(_box(page.get_by_test_id("composer-action-row")), _DESKTOP["width"])


def test_landing_label_collapse_preserves_submit_geometry(seeded_session: tuple[str, str]) -> None:
    """Collapsing the landing row's labels to icons moves nothing: the submit
    button keeps its right inset and row centering across the collapse."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_collapse_geometry(base_url, session_id))


async def _landing_collapse_geometry(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_DESKTOP)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id)
            card_loc = page.locator("[data-composer-card]")
            submit_loc = page.get_by_test_id("new-chat-landing-submit")
            attach_loc = page.get_by_test_id("new-chat-landing-attach")

            expanded_card = await card_loc.bounding_box()
            expanded_submit = await submit_loc.bounding_box()
            assert expanded_card is not None and expanded_submit is not None
            expanded_inset = _right_inset(expanded_card, expanded_submit)

            await page.set_viewport_size(_PHONE)
            await async_expect(page.get_by_test_id("new-chat-landing-actions")).to_have_attribute(
                "data-labels", "collapsed"
            )
            collapsed_card = await card_loc.bounding_box()
            collapsed_submit = await submit_loc.bounding_box()
            collapsed_attach = await attach_loc.bounding_box()
            assert (
                collapsed_card is not None
                and collapsed_submit is not None
                and collapsed_attach is not None
            )
            collapsed_inset = _right_inset(collapsed_card, collapsed_submit)
            assert abs(collapsed_inset - expanded_inset) <= _TOL, (
                f"label collapse shifted the submit button: right inset "
                f"{expanded_inset:.2f}px expanded vs {collapsed_inset:.2f}px collapsed"
            )
            _assert_same_row(collapsed_attach, collapsed_submit)
        finally:
            await context.close()
            await browser.close()


def test_live_label_collapse_preserves_send_geometry(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Collapsing the live row's labels to icons moves nothing: the Send
    button keeps its right inset and row centering across the collapse."""
    base_url, session_id = seeded_session
    _patch_session_as_databricks_claude_native(page, session_id)
    try:
        _open_live(page, base_url, session_id, _DESKTOP)
        card_loc = page.locator("[data-composer-card]")
        send_loc = page.get_by_role("button", name="Send", exact=True)
        expanded_inset = _right_inset(_box(card_loc), _box(send_loc))

        page.set_viewport_size(_PHONE)
        expect(page.get_by_test_id("composer-action-row")).to_have_attribute(
            "data-labels", "collapsed"
        )
        collapsed_inset = _right_inset(_box(card_loc), _box(send_loc))
        assert abs(collapsed_inset - expanded_inset) <= _TOL, (
            f"label collapse shifted the Send button: right inset "
            f"{expanded_inset:.2f}px expanded vs {collapsed_inset:.2f}px collapsed"
        )
        _assert_same_row(_box(page.get_by_test_id("composer-attach")), _box(send_loc))
    finally:
        page.unroute_all(behavior="ignoreErrors")


def test_landing_attachment_chips_follow_the_row_grid(seeded_session: tuple[str, str]) -> None:
    """Landing attachment chips on one visual row share height, vertical
    center, and equal gaps within 1 px, and start on the shared inset line."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_chip_grid(base_url, session_id))


async def _landing_chip_grid(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_DESKTOP)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id)
            await _attach_files_async(
                page,
                [
                    ("alpha.txt", "text/plain", b"a"),
                    ("beta.txt", "text/plain", b"b"),
                    ("gamma.txt", "text/plain", b"g"),
                ],
            )
            chips = await _chip_boxes_async(page, ["alpha.txt", "beta.txt", "gamma.txt"])
            assert len(chips) == 3
            first, *rest = chips
            for chip in rest:
                assert abs(chip["height"] - first["height"]) <= _TOL, (
                    f"chip heights diverge: {first['height']:.2f} vs {chip['height']:.2f}"
                )
                _assert_same_vertical_center(first, chip)
            gaps = [
                chips[i + 1]["x"] - (chips[i]["x"] + chips[i]["width"])
                for i in range(len(chips) - 1)
            ]
            for gap in gaps[1:]:
                assert abs(gap - gaps[0]) <= _TOL, (
                    f"chip gaps diverge: {gaps[0]:.2f}px vs {gap:.2f}px"
                )
            input_box = await page.get_by_test_id("new-chat-landing-input").bounding_box()
            assert input_box is not None
            assert abs(chips[0]["x"] - input_box["x"]) <= _TOL, (
                f"first chip starts at x={chips[0]['x']:.2f}, off the input's inset "
                f"line x={input_box['x']:.2f}"
            )
        finally:
            await context.close()
            await browser.close()


def test_live_attachment_chips_follow_the_row_grid(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Live attachment chips on one visual row share height, vertical center,
    and equal gaps within 1 px, and start on the shared inset line."""
    base_url, session_id = seeded_session
    _open_live(page, base_url, session_id, _DESKTOP)
    # The Workspace rail defaults open on desktop and squeezes the chat
    # column below the three-chip row width; close it so the chips share
    # one visual row.
    rail = page.get_by_role("complementary", name="Workspace")
    if rail.is_visible():
        page.keyboard.press("Control+Alt+BracketRight")
        expect(rail).not_to_be_visible()
    _attach_files(
        page,
        [
            ("alpha.txt", "text/plain", b"a"),
            ("beta.txt", "text/plain", b"b"),
            ("gamma.txt", "text/plain", b"g"),
        ],
    )
    chips = _chip_boxes(page, ["alpha.txt", "beta.txt", "gamma.txt"])
    assert len(chips) == 3
    first, *rest = chips
    for chip in rest:
        assert abs(chip["height"] - first["height"]) <= _TOL
        _assert_same_vertical_center(first, chip)
    gaps = [chips[i + 1]["x"] - (chips[i]["x"] + chips[i]["width"]) for i in range(len(chips) - 1)]
    for gap in gaps[1:]:
        assert abs(gap - gaps[0]) <= _TOL
    input_box = _box(page.get_by_label("Message the agent"))
    assert abs(chips[0]["x"] - input_box["x"]) <= _TOL


def _patch_session_as_mention_capable_claude_native(page: Page, session_id: str) -> None:
    """Shape the session snapshot so the composer enables ``@``-mentions:
    ``nativeCodingAgentForHarness`` gates them on a native harness id, which
    the databricks-claude label patch (``harness="claude"``) does not set.
    Everything else goes to the real spawned server."""

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["harness"] = "claude-native"
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        route.fulfill(response=response, json=payload)

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), _handle)


def test_live_mention_chips_follow_the_row_grid(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Mention chips (custom spans in ``ComposerChipRow``, not file tiles)
    follow the chip-row grid: shared height, vertical center, equal gaps
    within 1 px, and the first chip on the shared inset line."""
    base_url, session_id = seeded_session
    _patch_session_as_mention_capable_claude_native(page, session_id)
    names = ["alpha.md", "beta.md", "gamma.md"]
    listing = {
        "available": True,
        "data": [
            {"path": name, "name": name, "type": "file", "bytes": 10, "modified_at": None}
            for name in names
        ],
        "has_more": False,
    }
    try:
        page.route(
            re.compile(r"/v1/sessions/[^/]+/resources/environments/[^/]+/filesystem[?].*"),
            lambda route: route.fulfill(json=listing),
        )
        _open_live(page, base_url, session_id, _DESKTOP)
        input_loc = page.get_by_label("Message the agent")
        input_loc.click()
        for name in names:
            input_loc.press_sequentially("@")
            option = page.get_by_role("option", name=re.compile(name))
            expect(option).to_be_visible(timeout=15_000)
            option.get_by_role("button", name=name).click()
            expect(page.locator(f"span[title='{name}']")).to_be_visible()
        chips = [_box(page.locator(f"span[title='{name}']").locator("..")) for name in names]
        first, *rest = chips
        for chip in rest:
            assert abs(chip["height"] - first["height"]) <= _TOL, (
                f"mention-chip heights diverge: {first['height']:.2f} vs {chip['height']:.2f}"
            )
            _assert_same_vertical_center(first, chip)
        gaps = [chips[i + 1]["x"] - (chips[i]["x"] + chips[i]["width"]) for i in range(2)]
        assert abs(gaps[1] - gaps[0]) <= _TOL, (
            f"mention-chip gaps diverge: {gaps[0]:.2f}px vs {gaps[1]:.2f}px"
        )
        input_box = _box(page.get_by_label("Message the agent"))
        assert abs(first["x"] - input_box["x"]) <= _TOL, (
            f"first mention chip starts at x={first['x']:.2f}, off the input's "
            f"inset line x={input_box['x']:.2f}"
        )
    finally:
        page.unroute_all(behavior="ignoreErrors")


def test_live_wrapped_chip_rows_keep_the_grid(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Attachment chips wrapped across visual lines keep the grid: every line
    starts on the shared inset line, chips on one line share a baseline, and
    lines do not overlap. The Workspace rail stays OPEN — its squeeze is what
    wraps the row."""
    base_url, session_id = seeded_session
    _open_live(page, base_url, session_id, _DESKTOP)
    names = [
        "quarterly-planning-notes.txt",
        "customer-feedback-export.csv",
        "incident-review-minutes.md",
        "onboarding-checklist-draft.txt",
        "competitive-analysis-sheet.csv",
        "roadmap-brainstorm-doc.md",
    ]
    _attach_files(page, [(name, "text/plain", b"x") for name in names])
    chips = sorted(
        (_box(page.get_by_role("button", name=f"Remove {name}").locator("..")) for name in names),
        key=lambda box: (box["y"], box["x"]),
    )
    lines: list[list[FloatRect]] = []
    for chip in chips:
        if lines and abs(chip["y"] - lines[-1][0]["y"]) <= _TOL:
            lines[-1].append(chip)
        else:
            lines.append([chip])
    assert len(lines) >= 2, f"chips did not wrap: one line of {len(chips)}"
    for line in lines:
        for chip in line[1:]:
            _assert_same_vertical_center(line[0], chip)
    first_line_first = lines[0][0]
    input_box = _box(page.get_by_label("Message the agent"))
    assert abs(first_line_first["x"] - input_box["x"]) <= _TOL, (
        f"first wrapped-row chip starts at x={first_line_first['x']:.2f}, off the "
        f"input's inset line x={input_box['x']:.2f}"
    )
    for line in lines[1:]:
        assert abs(line[0]["x"] - first_line_first["x"]) <= _TOL, (
            f"wrapped line starts at x={line[0]['x']:.2f}, off the first line's "
            f"x={first_line_first['x']:.2f}"
        )
    for above, below in pairwise(lines):
        overlap = (above[0]["y"] + above[0]["height"]) - below[0]["y"]
        assert overlap <= _TOL, f"wrapped lines overlap by {overlap:.2f}px"


def test_landing_slash_rows_follow_the_row_grid(seeded_session: tuple[str, str]) -> None:
    """Landing slash-suggestion rows share icon x, label x, width, and height
    within 1 px and stack without overlap."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_slash_grid(base_url, session_id))


async def _landing_slash_grid(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_DESKTOP)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id, agents_body=_skilled_agents_body())
            input_loc = page.get_by_test_id("new-chat-landing-input")
            await input_loc.click()
            await input_loc.press_sequentially("/")
            rows = page.locator("[data-testid^='slash-menu-item-']")
            await async_expect(rows.first).to_be_visible()
            count = await rows.count()
            assert count >= 2, f"expected several slash rows, found {count}"
            boxes = []
            for i in range(min(count, 3)):
                box = await rows.nth(i).bounding_box()
                assert box is not None
                boxes.append(box)
            first, *rest = boxes
            for row in rest:
                assert abs(row["x"] - first["x"]) <= _TOL
                assert abs(row["width"] - first["width"]) <= _TOL
                assert abs(row["height"] - first["height"]) <= _TOL
            for a, b in pairwise(boxes):
                pitch = b["y"] - a["y"]
                assert abs(pitch - a["height"]) <= _TOL, (
                    f"rows are not stacked evenly: pitch {pitch:.2f}px vs height "
                    f"{a['height']:.2f}px"
                )
        finally:
            await context.close()
            await browser.close()


def test_live_slash_rows_follow_the_row_grid(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Live slash-suggestion rows share icon x, label x, width, and height
    within 1 px and stack without overlap."""
    base_url, session_id = seeded_session
    _patch_session_as_databricks_claude_native(page, session_id)
    try:
        _open_live(page, base_url, session_id, _DESKTOP)
        input_loc = page.get_by_label("Message the agent")
        input_loc.click()
        input_loc.press_sequentially("/")
        rows = page.locator("[data-testid^='slash-menu-item-']")
        expect(rows.first).to_be_visible()
        count = rows.count()
        assert count >= 2, f"expected several slash rows, found {count}"
        boxes = [_box(rows.nth(i)) for i in range(min(count, 3))]
        first, *rest = boxes
        for row in rest:
            assert abs(row["x"] - first["x"]) <= _TOL
            assert abs(row["width"] - first["width"]) <= _TOL
            assert abs(row["height"] - first["height"]) <= _TOL
        for a, b in pairwise(boxes):
            pitch = b["y"] - a["y"]
            assert abs(pitch - a["height"]) <= _TOL
        # A hovered row paints its background with symmetric left/right
        # insets inside the menu's content box.
        hovered = rows.nth(1)
        hovered.hover()
        background = hovered.evaluate("(el) => getComputedStyle(el).backgroundColor")
        assert background not in {"rgba(0, 0, 0, 0)", "transparent"}, (
            f"hovered slash row has no painted background: {background}"
        )
        panel = _box(hovered.locator("xpath=../.."))
        row_box = _box(hovered)
        menu_chrome = _border_left_px(hovered.locator("xpath=../..")) + 8  # border + p-2
        left_inset = row_box["x"] - (panel["x"] + menu_chrome)
        right_inset = (panel["x"] + panel["width"] - menu_chrome) - (
            row_box["x"] + row_box["width"]
        )
        assert abs(left_inset - right_inset) <= _TOL, (
            f"hovered row background insets asymmetric: left={left_inset:.2f}px "
            f"right={right_inset:.2f}px"
        )
    finally:
        page.unroute_all(behavior="ignoreErrors")


def test_live_queued_rows_follow_the_row_grid(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Queued-message rows share status-icon x, label x, trailing-action x,
    width, and height within 1 px while a turn runs."""
    base_url, session_id = seeded_session
    sentinel = "geometry-contract queue hold"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "done", "block": True}] * 4,
        key="geometry-queue-gate",
        match=sentinel,
    )
    try:
        _open_live(page, base_url, session_id, _DESKTOP)
        input_loc = page.get_by_label("Message the agent")
        input_loc.fill(sentinel)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_role("button", name="Interrupt")).to_be_visible(timeout=60_000)
        for follow_up in ["queued one", "queued two"]:
            input_loc.fill(follow_up)
            page.get_by_role("button", name="Send", exact=True).click()
        strip = page.get_by_test_id("composer-queued-strip")
        expect(strip).to_be_visible()
        rows = strip.get_by_role("listitem")
        expect(rows).to_have_count(2)
        boxes = [_box(rows.nth(i)) for i in range(2)]
        first, second = boxes
        assert abs(second["x"] - first["x"]) <= _TOL
        assert abs(second["width"] - first["width"]) <= _TOL
        assert abs(second["height"] - first["height"]) <= _TOL
        # Inner columns: the leading icon/status (drag grip or clock), the
        # primary label, and the trailing action group share x across rows.
        columns = []
        for i in range(2):
            row = rows.nth(i)
            status = _box(row.locator(":scope > *:first-child"))
            label = _box(row.locator("span.truncate").first)
            trailing = _box(row.locator(":scope > span").last)
            columns.append({"status icon": status, "label": label, "trailing action": trailing})
        for name in ("status icon", "label", "trailing action"):
            a, b = columns[0][name], columns[1][name]
            assert abs(a["x"] - b["x"]) <= _TOL, (
                f"queued-row {name} columns diverge: {a['x']:.2f} vs {b['x']:.2f}"
            )
        # The strip's leading column sits on the tray's shared inset line.
        strip_box = _box(strip)
        _assert_primitive_inset(
            columns[0]["status icon"]["x"] - strip_box["x"],
            0.0,
            "queued-row leading column",
        )
        _screenshot(page, "live-queued-rows-grid")
    finally:
        page.unroute_all(behavior="ignoreErrors")
        _release_gates(mock_llm_server_url)
        reset_mock_llm(mock_llm_server_url)


def test_landing_and_live_cards_share_max_width_and_centering(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Cross-surface contract: at a wide desktop width both cards share the
    same max-width, and each is centered in its column with symmetric insets
    within 1 CSS px."""
    base_url, session_id = seeded_session
    _open_live(page, base_url, session_id, _WIDE)
    live_card = _box(page.locator("[data-composer-card]"))
    live_container = _box(page.locator("[data-composer-card]").locator(".."))
    _assert_symmetric_insets(live_card, live_container)
    _run_in_fresh_loop(_landing_shared_width(base_url, session_id, live_card["width"]))


async def _landing_shared_width(base_url: str, session_id: str, live_width: float) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_WIDE)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id)
            card_loc = page.locator("[data-composer-card]")
            card = await card_loc.bounding_box()
            container = await card_loc.locator("..").bounding_box()
            assert card is not None and container is not None
            assert abs(card["width"] - live_width) <= _TOL
            _assert_symmetric_insets(card, container)
        finally:
            await context.close()
            await browser.close()


def _three_agent_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: three agents so the landing picker
    has sibling rows to measure."""
    agents = [
        ("ag_claude_e2e", "claude-native-ui", "Claude Code", "claude-native"),
        ("ag_codex_e2e", "codex-native-ui", "Codex", "codex-native"),
        ("ag_gemini_e2e", "gemini-cli", "Gemini CLI", "gemini-cli"),
    ]
    return json.dumps(
        {
            "data": [
                {
                    "id": agent_id,
                    "name": name,
                    "display_name": display,
                    "description": f"{display} agent",
                    "harness": harness,
                    "skills": [],
                }
                for agent_id, name, display, harness in agents
            ]
        }
    )


def _row_boxes(page: Page | AsyncPage, locator) -> list[FloatRect]:
    """Bounding boxes of the first three visible rows in a menu locator."""
    count = locator.count()
    assert count >= 2, f"expected several rows, found {count}"
    return [_box(locator.nth(i)) for i in range(min(count, 3))]


def test_landing_picker_rows_follow_the_row_grid(seeded_session: tuple[str, str]) -> None:
    """Landing agent-picker rows share the row grid (icon x, label x, trailing
    x, height, pitch) at desktop and phone widths."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_picker_grid(base_url, session_id))


async def _landing_picker_grid(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_DESKTOP)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id, agents_body=_three_agent_agents_body())
            for viewport in [_DESKTOP, _PHONE]:
                # A fresh load at each width: resizing desktop-to-phone leaves
                # the sidebar drawer open over the composer and it eats the
                # click (the live picker test reloads for the same reason).
                await page.set_viewport_size(viewport)
                await page.reload()
                await async_expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(
                    timeout=30_000
                )
                trigger = page.get_by_test_id("new-chat-landing-agent-select")
                await trigger.click()
                rows = page.locator(".composer-agent-menu .composer-agent-row")
                await async_expect(rows.first).to_be_visible()
                await page.locator(".composer-agent-menu").first.evaluate(_MENU_SETTLE_JS)
                boxes = []
                count = await rows.count()
                assert count >= 2, f"expected several picker rows, found {count}"
                for i in range(min(count, 3)):
                    box = await rows.nth(i).bounding_box()
                    assert box is not None
                    boxes.append(box)
                _assert_row_grid(boxes)
                # Harness marks render as <img> glyphs (data-URI SVG), so the
                # icon column is the first vector-or-image element in the row.
                icons = [
                    await rows.nth(i).locator("img, svg").first.bounding_box() for i in range(2)
                ]
                assert icons[0] is not None and icons[1] is not None
                assert abs(icons[0]["x"] - icons[1]["x"]) <= _TOL, (
                    f"row icons do not share x: {icons[0]['x']:.2f} vs {icons[1]['x']:.2f}"
                )
                # Label column: the first truncate span in each row.
                labels = [
                    await rows.nth(i).locator("span.truncate").first.bounding_box()
                    for i in range(2)
                ]
                assert labels[0] is not None and labels[1] is not None
                assert abs(labels[0]["x"] - labels[1]["x"]) <= _TOL, (
                    f"picker-row label columns diverge ({viewport['width']}px): "
                    f"{labels[0]['x']:.2f} vs {labels[1]['x']:.2f}"
                )
                # Value column: the summary cell's right edge.
                values = [
                    await rows.nth(i).locator("[data-testid*='agent-summary-']").bounding_box()
                    for i in range(2)
                ]
                assert values[0] is not None and values[1] is not None
                rights = [v["x"] + v["width"] for v in values]
                assert abs(rights[0] - rights[1]) <= _TOL, (
                    f"picker-row value columns diverge ({viewport['width']}px): "
                    f"{rights[0]:.2f} vs {rights[1]:.2f}"
                )
                await page.keyboard.press("Escape")
                # The menu unmounts after its close animation; reopening before
                # then races the outgoing overlay for the pointer.
                await async_expect(page.locator(".composer-agent-menu")).to_have_count(0)
        finally:
            await context.close()
            await browser.close()


def test_live_picker_rows_follow_the_row_grid(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Live model/effort config rows share the row grid (label x, value
    trailing x, height, pitch) at desktop and phone widths."""
    base_url, session_id = seeded_session
    _patch_session_as_databricks_claude_native(page, session_id)
    try:
        _open_live(page, base_url, session_id, _DESKTOP)
        for viewport in [_DESKTOP, _PHONE]:
            # A fresh load at each width: resizing desktop→phone leaves the
            # sidebar drawer open over the composer and it eats the click.
            _open_live(page, base_url, session_id, viewport)
            gear = page.get_by_test_id("composer-config-gear")
            gear.click()
            rows = page.locator(".composer-agent-menu [role=menuitem]")
            expect(rows.first).to_be_visible()
            page.locator(".composer-agent-menu").first.evaluate(_MENU_SETTLE_JS)
            boxes = _row_boxes(page, rows)
            _assert_row_grid(boxes)
            # Inner columns on the config sub-trigger rows: the label span's x
            # and the value span's right edge.
            columns = []
            for i in range(2):
                label = _box(rows.nth(i).locator("span.flex-1").first)
                value = _box(rows.nth(i).locator("span.text-right").first)
                columns.append((label, value))
            (a_label, a_value), (b_label, b_value) = columns
            assert abs(a_label["x"] - b_label["x"]) <= _TOL, (
                f"config-row label columns diverge ({viewport['width']}px): "
                f"{a_label['x']:.2f} vs {b_label['x']:.2f}"
            )
            a_right = a_value["x"] + a_value["width"]
            b_right = b_value["x"] + b_value["width"]
            assert abs(a_right - b_right) <= _TOL, (
                f"config-row value columns diverge ({viewport['width']}px): "
                f"{a_right:.2f} vs {b_right:.2f}"
            )
            page.keyboard.press("Escape")
    finally:
        page.unroute_all(behavior="ignoreErrors")


def test_landing_workspace_bar_aligns_with_the_card(seeded_session: tuple[str, str]) -> None:
    """The landing workspace tray nests inside the card's edges by the shared
    inset, with its content on the card's inset line."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_landing_workspace_bar(base_url, session_id))


def _assert_tray_nesting(bar: FloatRect, card: FloatRect, content_inset: float) -> None:
    """Fail unless the docked tray nests inside the card's outer edges by the
    shared content inset, symmetric left/right within ``_TOL`` px."""
    tray_left = bar["x"] - card["x"]
    tray_right = _right_inset(card, bar)
    assert abs(tray_left - content_inset) <= _TOL, (
        f"tray nesting inset {tray_left:.2f}px diverges from the card's content "
        f"inset {content_inset:.2f}px"
    )
    assert abs(tray_left - _PRIMITIVE_INSET_PX) <= _TOL, (
        f"tray nesting inset {tray_left:.2f}px off the shared "
        f"{_PRIMITIVE_INSET_PX:.0f}px primitive"
    )
    assert abs(tray_right - tray_left) <= _TOL, (
        f"tray nests asymmetrically: left={tray_left:.2f}px right={tray_right:.2f}px"
    )


async def _landing_workspace_bar(base_url: str, session_id: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=_DESKTOP)
        page = await context.new_page()
        try:
            await _open_landing(page, base_url, session_id)
            bar = await page.get_by_test_id("new-chat-landing-workspace-controls").bounding_box()
            card = await page.locator("[data-composer-card]").bounding_box()
            chip = await page.get_by_test_id("new-chat-landing-workspace-chip").bounding_box()
            input_box = await page.get_by_test_id("new-chat-landing-input").bounding_box()
            assert (
                bar is not None and card is not None and chip is not None and input_box is not None
            )
            input_inset = input_box["x"] - card["x"]
            _assert_tray_nesting(bar, card, input_inset)
            chip_inset = chip["x"] - bar["x"]
            assert abs(chip_inset - input_inset) <= _TOL, (
                f"bar content inset {chip_inset:.2f}px diverges from the card's "
                f"{input_inset:.2f}px"
            )
            bar_border = await _border_left_px_async(
                page.get_by_test_id("new-chat-landing-workspace-controls")
            )
            _assert_primitive_inset(chip_inset, bar_border, "landing tray content")
        finally:
            await context.close()
            await browser.close()


def test_live_workspace_bar_aligns_with_the_card(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The live workspace tray nests inside the card's edges by the shared
    inset, with its content on the card's inset line."""
    base_url, session_id = seeded_session
    _open_live(page, base_url, session_id, _DESKTOP)
    bar = _box(page.get_by_test_id("composer-workspace-controls"))
    card = _box(page.locator("[data-composer-card]"))
    chip = _box(page.get_by_test_id("composer-workspace-dir"))
    input_box = _box(page.get_by_label("Message the agent"))
    input_inset = input_box["x"] - card["x"]
    _assert_tray_nesting(bar, card, input_inset)
    chip_inset = chip["x"] - bar["x"]
    assert abs(chip_inset - input_inset) <= _TOL, (
        f"bar content inset {chip_inset:.2f}px diverges from the card's {input_inset:.2f}px"
    )
    _assert_primitive_inset(
        chip_inset,
        _border_left_px(page.get_by_test_id("composer-workspace-controls")),
        "live tray content",
    )


def test_live_workspace_bar_nested_variant_keeps_the_inset_line(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """With queued messages docked above it, the live workspace bar drops its
    top border (the nesting variant) without shifting its content inset line
    or the card's edges."""
    base_url, session_id = seeded_session
    sentinel = "geometry-contract workspace bar hold"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "done", "block": True}] * 4,
        key="geometry-workspace-bar-gate",
        match=sentinel,
    )
    try:
        _open_live(page, base_url, session_id, _DESKTOP)
        input_loc = page.get_by_label("Message the agent")
        input_loc.fill(sentinel)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_role("button", name="Interrupt")).to_be_visible(timeout=60_000)
        input_loc.fill("queued while running")
        page.get_by_role("button", name="Send", exact=True).click()
        strip = page.get_by_test_id("composer-queued-strip")
        expect(strip).to_be_visible()

        bar = page.get_by_test_id("composer-workspace-controls")
        border_top = bar.evaluate("(el) => getComputedStyle(el).borderTopWidth")
        assert border_top == "0px", f"nesting variant keeps a top border: {border_top}"
        bar_box = _box(bar)
        card_box = _box(page.locator("[data-composer-card]"))
        input_box = _box(page.get_by_label("Message the agent"))
        _assert_tray_nesting(bar_box, card_box, input_box["x"] - card_box["x"])
        strip_box = _box(strip)
        assert abs(strip_box["x"] - bar_box["x"]) <= _TOL, (
            f"queued strip and bar left edges diverge: {strip_box['x']:.2f} vs {bar_box['x']:.2f}"
        )
        _screenshot(page, "live-workspace-bar-nested-variant")
    finally:
        page.unroute_all(behavior="ignoreErrors")
        _release_gates(mock_llm_server_url)
        reset_mock_llm(mock_llm_server_url)
