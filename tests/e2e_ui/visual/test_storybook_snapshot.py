"""Visual regression tests for opted-in Storybook component states.

The static Storybook index is built before this suite runs. Every story tagged
``visual-snapshot`` becomes an independently named pytest case and therefore an
independent committed PNG baseline. Full-page snapshots remain in this suite for
integration coverage; these cases isolate smaller component state changes.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, Route, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STORYBOOK_ROOT = _REPO_ROOT / "web" / "storybook-static"
_STORY_INDEX = _STORYBOOK_ROOT / "index.json"
_STORYBOOK_SNAPSHOT_ROOT = (
    Path(__file__).with_name("snapshots")
    / "test_storybook_snapshot"
    / "test_story_matches_baseline"
)
_VISUAL_TAG = "visual-snapshot"


@dataclass(frozen=True)
class StoryCase:
    id: str
    title: str
    name: str


def _load_story_cases() -> list[StoryCase]:
    if not _STORY_INDEX.is_file():
        pytest.skip(
            "Storybook static build is missing; run `pnpm --filter web run build:storybook`.",
            allow_module_level=True,
        )

    payload = json.loads(_STORY_INDEX.read_text())
    entries = payload.get("entries", {})
    cases = [
        StoryCase(id=entry["id"], title=entry["title"], name=entry["name"])
        for entry in entries.values()
        if entry.get("type") == "story" and _VISUAL_TAG in entry.get("tags", [])
    ]
    if not cases:
        raise RuntimeError(f"Storybook index contains no stories tagged {_VISUAL_TAG!r}.")
    return sorted(cases, key=lambda case: case.id)


_STORY_CASES = _load_story_cases()


class _SilentStorybookHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def storybook_server() -> Iterator[str]:
    """Serve the prebuilt Storybook from a local ephemeral port."""
    handler = partial(_SilentStorybookHandler, directory=str(_STORYBOOK_ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.visual
def test_storybook_baselines_match_index() -> None:
    """Reject baselines left behind by removed, renamed, or untagged stories."""
    current_ids = {story.id for story in _STORY_CASES}
    stale: list[Path] = []
    for snapshot in _STORYBOOK_SNAPSHOT_ROOT.glob("*.png"):
        parameter = snapshot.name.partition("[")[2].partition("][")[0]
        _, separator, story_id = parameter.partition("-")
        if not separator or story_id not in current_ids:
            stale.append(snapshot)

    if not stale:
        return
    if os.environ.get("GITHUB_ACTIONS"):
        for snapshot in stale:
            snapshot.unlink()
    paths = "\n".join(f"- {path.relative_to(_REPO_ROOT)}" for path in stale)
    pytest.fail(f"Remove stale Storybook snapshot baselines:\n{paths}")


@pytest.mark.visual
@pytest.mark.parametrize("story", _STORY_CASES, ids=lambda story: story.id)
def test_story_matches_baseline(
    story: StoryCase,
    snapshot_page: Page,
    storybook_server: str,
    settle_for_snapshot,
    assert_snapshot,
) -> None:
    """Render one tagged story in isolation and compare its pixels."""
    page = snapshot_page
    unexpected_requests: list[str] = []

    def reject_external_request(route: Route) -> None:
        if route.request.url.startswith(f"{storybook_server}/"):
            route.continue_()
            return
        unexpected_requests.append(route.request.url)
        route.abort()

    page.route("**/*", reject_external_request)
    story_id = quote(story.id, safe="")
    page.goto(f"{storybook_server}/iframe.html?id={story_id}&viewMode=story")

    root = page.locator("#storybook-root")
    surface = root.locator(":scope > :not(script)").first
    expect(surface).to_be_visible(timeout=30_000)
    page.wait_for_function(
        "(id) => document.documentElement.dataset.storybookStoryId === id",
        arg=story.id,
        timeout=30_000,
    )
    html = page.locator("html")
    story_status = html.get_attribute("data-storybook-story-status")
    play_error = html.get_attribute("data-storybook-play-error")
    assert story_status == "success", f"Storybook story finished with status {story_status!r}"
    assert play_error is None, "Storybook play function threw an exception"

    # Freeze motion so spinners, transitions, and delayed mascot animation do
    # not encode capture timing into the baseline.
    page.add_style_tag(
        content=(
            "*, *::before, *::after { animation: none !important; transition: none !important; }"
        )
    )
    shared_code_blocks = page.locator('[data-code-highlighted="false"]')
    if shared_code_blocks.count() > 0:
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('[data-code-highlighted]'))
                .every((block) => block.getAttribute('data-code-highlighted') === 'true')""",
            timeout=30_000,
        )

    code_blocks = page.locator('[data-streamdown="code-block-body"]')
    if code_blocks.count() > 0:
        page.wait_for_function(
            """() => {
                const spans = Array.from(document.querySelectorAll(
                    '[data-streamdown="code-block-body"] span[style*="--sdm-c"]'
                ));
                return spans.length > 1 &&
                    new Set(spans.map((span) => getComputedStyle(span).color)).size > 1;
            }""",
            timeout=30_000,
        )

    images = page.locator("img[src]")
    if images.count() > 0:
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('img[src]'))
                .every((image) => image.complete && image.naturalWidth > 0)""",
            timeout=30_000,
        )

    settle_for_snapshot(page)
    page.evaluate(
        "() => new Promise((resolve) => "
        "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )

    assert not unexpected_requests, f"Story made external requests: {unexpected_requests}"

    portals = page.locator(
        '[data-radix-popper-content-wrapper]:visible, [role="dialog"][data-state="open"]:visible'
    )
    if portals.count() == 0:
        assert_snapshot(surface)
        return

    boxes = [surface.bounding_box()]
    boxes.extend(portals.nth(index).bounding_box() for index in range(portals.count()))
    visible_boxes = [box for box in boxes if box is not None]
    assert visible_boxes, "Story snapshot has no visible surface"
    viewport = page.viewport_size
    assert viewport is not None
    padding = 8
    left = max(0, min(box["x"] for box in visible_boxes) - padding)
    top = max(0, min(box["y"] for box in visible_boxes) - padding)
    right = min(viewport["width"], max(box["x"] + box["width"] for box in visible_boxes) + padding)
    bottom = min(
        viewport["height"], max(box["y"] + box["height"] for box in visible_boxes) + padding
    )
    screenshot = page.screenshot(
        animations="disabled",
        clip={"x": left, "y": top, "width": right - left, "height": bottom - top},
    )
    assert_snapshot(screenshot)
