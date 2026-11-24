"""Drive a branch dev server headlessly and save screenshots as e2e evidence.

Usage (with the repo venv's Python):
    python dev/branch_screenshot.py URL OUT.png [--click TEXT]... [--expect TEXT]...
        [--header NAME:VALUE]... [--video DIR]

Opens URL in headless Chromium with clipboard access, clicks each ``--click``
target (by visible text or role name, in order), waits for every ``--expect``
text, then writes a full-page screenshot to OUT.png. Exits non-zero when a
step fails, after saving a screenshot of the failure next to OUT.png.
``--header`` adds a request header to every request, e.g.
``X-Forwarded-Email:member@example.com`` on an ``--auth-header`` branch server;
``--video DIR`` also records the run as a WebM in DIR.
Pair with ``dev/branch-server.sh``; see ``dev/branch-server.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from playwright.sync_api import Error, Page, sync_playwright


def installed_chromium() -> str | None:
    """Return the newest Chromium in the Playwright cache, if any.

    The pinned Playwright release may expect a browser build that was never
    downloaded; any cached build works for screenshots.

    :returns: Executable path, or ``None`` to let Playwright use its default.
    """
    cache = Path.home() / ".cache" / "ms-playwright"
    candidates = sorted(
        cache.glob("chromium-*/chrome-linux*/chrome"),
        key=lambda path: int(path.parts[-3].rsplit("-", 1)[1]),
    )
    return str(candidates[-1]) if candidates else None


def settle(page: Page, seconds: float) -> None:
    """Give the app up to ``seconds`` to go network-idle after load.

    Chat pages keep a live stream open and never go idle, so this is best effort.
    """
    with contextlib.suppress(Error):
        page.wait_for_load_state("networkidle", timeout=seconds * 1000)


def click(page: Page, target: str) -> None:
    """Click a button/link by accessible name, falling back to visible text."""
    by_role = page.get_by_role("button", name=target).or_(page.get_by_role("link", name=target))
    locator = by_role if by_role.count() else page.get_by_text(target, exact=False)
    locator.first.click()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("out", type=Path)
    parser.add_argument(
        "--click", action="append", default=[], help="text or name to click, in order"
    )
    parser.add_argument(
        "--expect", action="append", default=[], help="text that must become visible"
    )
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument(
        "--settle", type=float, default=5.0, help="max seconds to wait for idle after load"
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="seconds per step")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="request header for every request, e.g. X-Forwarded-Email:member@example.com",
    )
    parser.add_argument("--video", type=Path, help="also record a WebM video into this dir")
    args = parser.parse_args()
    headers: dict[str, str] = {}
    for header in args.header:
        name, separator, value = header.partition(":")
        if not separator or not name.strip():
            parser.error(f"--header needs NAME:VALUE, got {header!r}")
        headers[name.strip()] = value.strip()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = None
        try:
            browser = playwright.chromium.launch(executable_path=installed_chromium())
            viewport = {"width": args.width, "height": args.height}
            context = browser.new_context(
                viewport=viewport,
                permissions=["clipboard-read", "clipboard-write"],
                extra_http_headers=headers or None,
                record_video_dir=str(args.video) if args.video else None,
                record_video_size=viewport if args.video else None,
            )
            page = context.new_page()
            page.set_default_timeout(args.timeout * 1000)
            page.goto(args.url, wait_until="load")
            settle(page, args.settle)
            for target in args.click:
                click(page, target)
            for text in args.expect:
                page.get_by_text(text, exact=False).first.wait_for(state="visible")
            page.screenshot(path=str(args.out), full_page=True)
        except Error as error:
            failure = args.out.with_name(f"{args.out.stem}-failure{args.out.suffix}")
            if browser and browser.contexts and browser.contexts[0].pages:
                browser.contexts[0].pages[0].screenshot(path=str(failure), full_page=True)
                print(f"failure screenshot: {failure}", file=sys.stderr)
            print(f"e2e step failed: {error}", file=sys.stderr)
            return 1
        finally:
            if browser:
                # Closing the context first finishes writing a --video recording.
                for open_context in browser.contexts:
                    open_context.close()
                browser.close()
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
