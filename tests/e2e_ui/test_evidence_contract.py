"""Small browser probes for every Playwright context style used by this suite."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import zipfile
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import async_playwright
from playwright.sync_api import Browser, Page

from tests.e2e_ui.conftest import _validate_vite_output, _vite_build_lock_path
from tests.e2e_ui.playwright_evidence import VERIFY_RUN_DIR_ENV, _sanitize_trace_archive


def test_managed_page_fixture_is_evidence_bound(page: Page) -> None:
    page.goto("data:text/html,<title>managed</title>")
    page.evaluate("console.log('token=managed-secret-value')")
    page.evaluate("setTimeout(() => { throw new Error('managed page error'); }, 0)")
    page.wait_for_timeout(50)
    assert page.title() == "managed"


def test_vite_output_symlink_cannot_delete_external_data(tmp_path: Path) -> None:
    trusted = tmp_path / "run"
    external = tmp_path / "external"
    trusted.mkdir()
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")
    output = trusted / "build"
    output.symlink_to(external, target_is_directory=True)

    with pytest.raises(pytest.fail.Exception, match="symlink"):
        _validate_vite_output(output, trusted)

    assert sentinel.read_text(encoding="utf-8") == "untouched\n"


def test_vite_build_lock_is_outside_checkout() -> None:
    lock = _vite_build_lock_path().resolve()

    assert not lock.is_relative_to(Path(__file__).resolve().parents[2])


def test_direct_sync_context_is_evidence_bound(browser: Browser) -> None:
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto("data:text/html,<title>direct-sync</title>")
        page.evaluate("console.warn('api_key=direct-sync-secret')")
        assert page.title() == "direct-sync"
    finally:
        context.close()


def test_direct_async_context_is_evidence_bound() -> None:
    _run_in_fresh_loop(_drive_direct_async_context())


async def _drive_direct_async_context() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto("data:text/html,<title>direct-async</title>")
            await page.evaluate("console.error('Bearer direct-async-secret')")
            assert await page.title() == "direct-async"
        finally:
            await browser.close()


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    error: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "async Playwright evidence probe timed out"
    if error:
        raise error[0]


def test_evidence_plugin_does_not_require_a_browser() -> None:
    raw_run_dir = os.environ.get(VERIFY_RUN_DIR_ENV)
    if raw_run_dir:
        run_dir = Path(raw_run_dir)
        metadata_files = list((run_dir / "playwright").glob("**/metadata.json"))
        metadata = [json.loads(path.read_text(encoding="utf-8")) for path in metadata_files]
        assert {item["context_style"] for item in metadata} >= {
            "managed-sync",
            "direct-sync",
            "direct-async",
        }
        assert len(list((run_dir / "playwright").glob("**/*.png"))) >= 3
        assert len(list((run_dir / "playwright").glob("**/*.zip"))) >= 3
        assert list((run_dir / "playwright").glob("**/*.webm"))
        serialized = json.dumps(metadata)
        assert "managed-secret-value" not in serialized
        assert "direct-sync-secret" not in serialized
        assert "direct-async-secret" not in serialized
        markers = (
            b"managed-secret-value",
            b"direct-sync-secret",
            b"direct-async-secret",
        )
        for trace in run_dir.rglob("*.zip"):
            _sanitize_trace_archive(trace)
        for path in run_dir.rglob("*"):
            if not path.is_file():
                continue
            payload = path.read_bytes()
            assert not any(marker in payload for marker in markers)
            if path.suffix == ".zip":
                with zipfile.ZipFile(path) as archive:
                    assert archive.testzip() is None
                    for member in archive.infolist():
                        payload = archive.read(member)
                        assert not any(marker in payload for marker in markers)
    assert True
