"""Starting a session does not wait for the host's git worktree listing.

The composer lists the picked directory's worktrees for its worktree control;
a slow host must not hold an ordinary start in that directory.
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _WORKTREES_RE,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)


def test_start_session_while_worktree_discovery_is_slow(
    seeded_session: tuple[str, str],
) -> None:
    """Send enables and the session starts while ``/worktrees`` is still pending."""
    _run_in_fresh_loop(_drive_slow_worktree_discovery(*seeded_session))


async def _drive_slow_worktree_discovery(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        release_worktrees = asyncio.Event()
        try:
            create_bodies: list[dict[str, Any]] = []
            worktree_requests: list[str] = []
            await _register_common_routes(
                page, created_session_id=session_id, create_bodies=create_bodies
            )

            async def slow_worktrees(route: Route) -> None:
                # Stands in for a host whose git worktree listing runs into its
                # own timeout; the request stays open until the test ends.
                worktree_requests.append(route.request.url)
                await release_worktrees.wait()
                await route.fulfill(
                    status=400,
                    json={"detail": "worktree listing failed: git command timed out after 120s"},
                )

            await page.route(_WORKTREES_RE, slow_worktrees)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            composer = page.get_by_test_id("new-chat-landing-input")
            await composer.wait_for(state="visible", timeout=30_000)
            await _wait_until(lambda: len(worktree_requests) == 1)
            await composer.fill("set up the project")

            submit = page.get_by_test_id("new-chat-landing-submit")
            await expect(submit).to_be_enabled(timeout=15_000)
            assert not release_worktrees.is_set()
            await submit.click()

            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            assert body["host_id"] == _HOST_ID
            assert body["workspace"] == "/work/repo"
            assert "git" not in body
            await expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)
            assert len(worktree_requests) == 1
        finally:
            # Close the page before releasing the held route, so the handler's
            # fulfill cannot race a closing context and raise TargetClosedError.
            try:
                await page.context.close()
            finally:
                release_worktrees.set()
                await browser.close()
