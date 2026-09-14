"""The ``OMNIGENT_E2E_RECORD_DIR`` recorder must film sync-API journeys.

Tests that take pytest-playwright's sync ``page``/``context`` fixtures, or open
a browser through ``playwright.sync_api`` directly, must be recorded when
``OMNIGENT_E2E_RECORD_DIR`` is set — exactly like tests driving the async API.
Historically only the async ``Browser`` methods were patched, so sync-fixture
journeys were silently never filmed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, Page


@pytest.fixture(scope="module", autouse=True)
def record_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point the recorder at a temp dir before any function-scoped fixture runs.

    Module scope guarantees the env var is set before ``browser_context_args``,
    ``_record_video``, and pytest-playwright's ``context`` fixture instantiate,
    and overrides any ambient ``OMNIGENT_E2E_RECORD_DIR`` so the assertions
    below stay hermetic.
    """
    target = tmp_path_factory.mktemp("record")
    mp = pytest.MonkeyPatch()
    mp.setenv("OMNIGENT_E2E_RECORD_DIR", str(target))
    yield target
    mp.undo()


def test_context_args_carry_record_video_dir(
    record_dir: Path,
    browser_context_args: dict[str, Any],
) -> None:
    """The sync-fixture context options must point Playwright at the record dir."""
    assert browser_context_args.get("record_video_dir") == str(record_dir)


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
