"""The optional recorder must capture sync Playwright calls and pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page


@pytest.fixture(scope="module", autouse=True)
def record_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Set an isolated recording directory before function-scoped browser fixtures run."""
    target = tmp_path_factory.mktemp("record")
    mp = pytest.MonkeyPatch()
    mp.setenv("OMNIGENT_E2E_RECORD_DIR", str(target))
    yield target
    mp.undo()


def test_context_args_carry_record_video_dir(
    record_dir: Path,
    browser_context_args: dict[str, Any],
    browser: Browser,
    pytestconfig: pytest.Config,
) -> None:
    """Record to the environment default or the explicit pytest video directory."""
    target = Path(browser_context_args["record_video_dir"])
    if pytestconfig.getoption("video") == "off":
        assert target == record_dir
    context = browser.new_context(**browser_context_args)
    try:
        page = context.new_page()
        page.goto("about:blank")
        video = page.video
        assert video is not None
    finally:
        context.close()
    path = Path(video.path())
    assert path.parent == target
    assert path.stat().st_size > 0


def test_sync_api_context_is_recorded(record_dir: Path, browser: Browser) -> None:
    """A context opened via the sync API directly must emit a ``.webm``."""
    context = browser.new_context()
    page = context.new_page()
    page.goto("about:blank")
    video = page.video
    assert video is not None, "page opened via sync Browser API is not recording"
    context.close()
    assert list(record_dir.rglob("*.webm")), "no .webm landed in the record dir"


def test_page_fixture_is_recorded(record_dir: Path, page: Page) -> None:
    """A test on pytest-playwright's sync ``page`` fixture must be recording."""
    page.goto("about:blank")
    assert page.video is not None, "sync `page` fixture journey is not recording"
