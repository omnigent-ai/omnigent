"""Real Chromium checks for benchmark validity; all network responses are local mocks."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from playwright.async_api import Browser, BrowserContext, Route, async_playwright

from dev.benchmarks.omnigent.environment import BenchEnvironment
from dev.benchmarks.ui.run import (
    _MIN_ELEMENTS,
    _TRANSCRIPT_TURNS,
    measure_scenario,
    measure_typing,
)
from tests._helpers.async_thread import run_in_fresh_loop

# Keep the synchronous suite's event loop alive even when this file runs alone.
pytestmark = pytest.mark.usefixtures("playwright")


@asynccontextmanager
async def _routed_browser(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[Route], Awaitable[None]]
) -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        new_context = browser.new_context

        async def context(**kwargs: Any) -> BrowserContext:
            result = await new_context(**kwargs)
            await result.route("**/*", handler)
            return result

        monkeypatch.setattr(browser, "new_context", context)
        try:
            yield browser
        finally:
            await browser.close()


_ENV = cast(BenchEnvironment, SimpleNamespace(base_url="http://ui-benchmark.invalid"))
_ARGS = argparse.Namespace(cpu_throttle=1, warmup=1, iterations=2)
_BODY = (
    '<textarea aria-label="Message the agent"></textarea>'
    f"<p>Review complete {_TRANSCRIPT_TURNS - 1}.</p>" + "<span></span>" * (_MIN_ELEMENTS + 100)
)


@pytest.mark.parametrize("failure", ["http-404", "network-error", "aborted"])
def test_measure_scenario_rejects_failed_static_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def handler(route: Route) -> None:
        if route.request.url.endswith("/missing.css"):
            if failure == "http-404":
                await route.fulfill(status=404, content_type="text/css", body="")
            else:
                await route.abort("aborted" if failure == "aborted" else "failed")
        else:
            assert route.request.url.endswith("/c/session")
            await route.fulfill(
                content_type="text/html",
                body='<html><head><link rel="stylesheet" href="/missing.css">'
                '<script type="module">window.booted = true;</script></head>'
                f"<body>{_BODY}</body></html>",
            )

    async def drive() -> None:
        async with _routed_browser(monkeypatch, handler) as browser:
            with pytest.raises(RuntimeError, match=r"Required asset \(stylesheet\)"):
                await measure_scenario(
                    browser, _ENV, "session", "browser", _ARGS, tmp_path / "asset"
                )

    run_in_fresh_loop(drive())
    assert (tmp_path / "asset.failed.png").is_file()


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("page-error", "Browser errors:.*benchmark test error"),
        ("small-dom", "Fixture has only .* elements; requires"),
        ("shrinking-dom", "Fixture shrank to .* elements while typing"),
    ],
)
def test_measure_scenario_rejects_invalid_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, message: str
) -> None:
    body = _BODY.replace("<span></span>", "") if failure == "small-dom" else _BODY
    script = "throw new Error('benchmark test error');" if failure == "page-error" else ""
    if failure == "shrinking-dom":
        script = """
            document.addEventListener('input', () => {
                document.querySelectorAll('span').forEach(node => node.remove());
            }, {once: true});
        """

    async def handler(route: Route) -> None:
        await route.fulfill(
            content_type="text/html",
            body=f"<html><head><script>{script}</script></head><body>{body}</body></html>",
        )

    async def drive() -> None:
        async with _routed_browser(monkeypatch, handler) as browser:
            with pytest.raises(RuntimeError, match=message):
                await measure_scenario(
                    browser, _ENV, "session", "browser", _ARGS, tmp_path / failure
                )

    run_in_fresh_loop(drive())
    assert (tmp_path / f"{failure}.failed.png").is_file()


def test_measure_typing_rejects_lost_keystroke(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(route: Route) -> None:
        await route.fulfill(
            content_type="text/html",
            body="""<textarea aria-label="Message the agent"></textarea><script>
                let insertions = 0;
                document.querySelector('textarea').addEventListener('beforeinput', event => {
                    if (event.inputType === 'insertText' && ++insertions === 2) {
                        event.preventDefault();
                    }
                });
                </script>""",
        )

    async def drive() -> None:
        async with _routed_browser(monkeypatch, handler) as browser:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(_ENV.base_url)
            cdp = await context.new_cdp_session(page)
            await cdp.send("Performance.enable")
            with pytest.raises(RuntimeError, match="Composer lost or changed keystroke 2"):
                await measure_typing(page, cdp, 2)

    run_in_fresh_loop(drive())


@pytest.mark.parametrize("mode", ["browser", "mac_css"])
def test_css_scope_is_active_before_spa_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    expected = "true" if mode == "mac_css" else "false"
    html = f"""<html><head><script type="module">
        if (document.documentElement.hasAttribute('data-electron-mac') !== {expected}) {{
            throw new Error('Wrong CSS scope during SPA bootstrap');
        }}
        </script></head><body>{_BODY}</body></html>"""

    async def handler(route: Route) -> None:
        assert route.request.url.endswith("/c/session")
        await route.fulfill(content_type="text/html", body=html)

    async def drive() -> None:
        async with _routed_browser(monkeypatch, handler) as browser:
            sample = await measure_scenario(browser, _ENV, "session", mode, _ARGS, tmp_path / mode)
        assert sample["page_errors"] == []
        assert len(sample["key_to_frame"]) == _ARGS.iterations

    run_in_fresh_loop(drive())
