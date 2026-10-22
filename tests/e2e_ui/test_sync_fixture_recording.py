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


def test_page_fixture_is_recorded(record_dir: Path, page: Page) -> None:
    """A test on pytest-playwright's sync ``page`` fixture must be recording."""
    page.goto("about:blank")
    assert page.video is not None, "sync `page` fixture journey is not recording"


@pytest.mark.parametrize("factory", ["new_context", "new_page"])
@pytest.mark.parametrize("explicit", [False, True])
def test_sync_api_recording_directory(
    record_dir: Path, browser: Browser, tmp_path: Path, factory: str, explicit: bool
) -> None:
    target = tmp_path / "explicit" if explicit else record_dir
    created = getattr(browser, factory)(**({"record_video_dir": str(target)} if explicit else {}))
    page = created.new_page() if factory == "new_context" else created
    try:
        page.set_content("<h1>sync recording</h1>")
        video = page.video
        assert video is not None
    finally:
        page.context.close()
    path = Path(video.path())
    assert path.parent == target
    assert path.stat().st_size > 0


@pytest.mark.parametrize("factory", ["new_context", "new_page"])
@pytest.mark.parametrize("explicit", [False, True])
def test_async_api_recording_directory(
    record_dir: Path, tmp_path: Path, factory: str, explicit: bool
) -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from playwright.async_api import async_playwright

    target = tmp_path / "explicit" if explicit else record_dir

    async def drive() -> Path:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                created = await getattr(browser, factory)(
                    **({"record_video_dir": str(target)} if explicit else {})
                )
                page = await created.new_page() if factory == "new_context" else created
                await page.set_content("<h1>async recording</h1>")
                # Let the screencast deliver a frame before finalizing the artifact.
                await page.wait_for_timeout(250)
                video = page.video
                assert video is not None
                await page.context.close()
                return Path(await video.path())
            finally:
                await browser.close()

    # pytest-playwright's sync loop may already occupy the main thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        path = executor.submit(lambda: asyncio.run(drive())).result(timeout=120)
    assert path.parent == target
    assert path.stat().st_size > 0
