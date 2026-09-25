"""Task labels survive the hook, routing relay, live transcript, and reload.

The Claude binary is omitted: realistic Agent inputs go through the real hook
payload builder, runner loopback endpoint, and server relay. Similar tasks must
remain attributable even when their routing rationales overlap.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from omnigent.inner.hook_scripts.subagent_router import build_route_request
from omnigent.runner.subagent_routing import make_server_relay_resolver, start_subagent_router
from tests.e2e_ui.conftest import seed_committed_turn

_PARENT_MODEL = "databricks-claude-sonnet-4-6"
_TASKS = ("Review auth.py", "Review sessions.py", "Review tokens.py")


async def _route_spawns(base_url: str, session_id: str, bridge_dir: Path) -> None:
    """Send hook payloads through the runner's real loopback and server hops."""
    async with httpx.AsyncClient(base_url=base_url) as server_client:
        router = start_subagent_router(
            bridge_dir=bridge_dir,
            session_id=session_id,
            resolver=make_server_relay_resolver(server_client),
            loop=asyncio.get_running_loop(),
        )
        try:
            async with httpx.AsyncClient() as hook_client:
                for description in _TASKS:
                    body = build_route_request(
                        {
                            "subagent_type": "general-purpose",
                            "description": description,
                            "prompt": (
                                f"{description} for correctness. Report findings without editing."
                            ),
                        },
                        harness="claude-native",
                        parent_model=_PARENT_MODEL,
                    )
                    hook = await hook_client.post(
                        f"{router.url}/v1/sessions/{session_id}/route-subagent",
                        headers={"Authorization": f"Bearer {router.token}"},
                        json=body,
                        timeout=15.0,
                    )
                    hook.raise_for_status()
        finally:
            router.close()


def _expect_task_attribution(page: Page) -> None:
    """Each card names its task and exposes the matching raw verdict."""
    cards = page.get_by_test_id("routing-decision-card")
    expect(cards).to_have_count(len(_TASKS), timeout=15_000)
    for description in _TASKS:
        card = cards.filter(
            has=page.get_by_test_id("routing-decision-task").filter(has_text=description)
        )
        expect(card).to_have_count(1)
        expect(card.get_by_test_id("routing-decision-task")).to_have_text(description)
        expect(card.get_by_test_id("routing-decision-scope")).to_contain_text(
            "subagent: general-purpose"
        )
        card.get_by_role("button", name="Show raw routing verdict").click()
        expect(card.locator("pre")).to_contain_text(f'"task_description": "{description}"')


def test_fanout_routing_chips_are_individually_attributable(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Identify three similar tasks in live chips and persisted raw verdicts."""
    base_url, session_id = seeded_session
    session_url = f"{base_url}/v1/sessions/{session_id}"
    resp = httpx.patch(
        session_url,
        json={"subagent_routing_override": "on"},
        timeout=10.0,
    )
    resp.raise_for_status()
    seed_committed_turn(
        session_id,
        prompt="Fan out three reviewers for auth.py, sessions.py, and tokens.py.",
        reply="Spawning three reviewers now.",
    )

    # Subscribe before routing so the first assertions exercise live delivery.
    with page.expect_response(lambda response: f"{session_url}/stream" in response.url):
        page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("composer-workspace-controls")).to_be_visible()

    # Playwright owns the main thread's event loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, _route_spawns(base_url, session_id, tmp_path)).result(
            timeout=60
        )

    _expect_task_attribution(page)

    items = httpx.get(f"{session_url}/items", timeout=10.0)
    items.raise_for_status()
    decisions = [i for i in items.json()["data"] if i["type"] == "routing_decision"]
    assert sorted(i["task_description"] for i in decisions) == sorted(_TASKS)
    assert all(i["scope"] == "native_subagent" for i in decisions)

    page.reload()
    _expect_task_attribution(page)
