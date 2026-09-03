"""E2E UI: an expired gateway token failure must surface the re-auth hint.

Drives the journey at the same boundary as the forwarder-pipeline e2e test:
the codex app-server reports a failed turn carrying the Databricks AI
Gateway's verbatim rejection ("access_token is expired"), and the REAL
forwarder pipeline (classification → edge derivation → status publication)
posts the edge to a live server. The chat page must render the failure pill
whose expanded message carries the re-auth hint — the recovery affordance a
generic classification drops, leaving the session dead with no instructions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent import codex_native_forwarder as fwd
from omnigent.codex_native_bridge import CodexNativeBridgeState, write_bridge_state
from tests.e2e_ui.conftest import seed_committed_turn

DATABRICKS_EXPIRED_TOKEN_MESSAGE = "access_token is expired"


async def _drive_forwarder_failure(base_url: str, session_id: str, bridge_dir: Path) -> None:
    """Publish the failed-turn edge through the real forwarder pipeline."""
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id=session_id,
            socket_path=str(bridge_dir / "app-server.sock"),
            thread_id="thread_demo",
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id="turn_demo",
        ),
    )
    params = {
        "turn": {
            "id": "turn_demo",
            "status": "failed",
            "error": {"message": DATABRICKS_EXPIRED_TOKEN_MESSAGE},
        }
    }
    # No classification assert here: whether the error was recognized as auth
    # must surface at the UI layer below (the pill's re-auth hint), so a
    # misclassification fails on the user-visible recovery affordance.
    edge = fwd._terminal_turn_status_edge(bridge_dir, "turn/completed", params)
    assert edge is not None and edge.error is not None
    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        await fwd._post_status(client, session_id, "running", response_id="codex_turn_demo")
        # Let the running edge land before the terminal one so the page walks
        # the same running -> failed lifecycle a live turn does.
        await asyncio.sleep(1.0)
        await fwd._post_turn_status_edge(client, session_id, edge)


@pytest.mark.timeout(120)
def test_expired_token_failure_surfaces_reauth_hint(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    base_url, session_id = seeded_session
    seed_committed_turn(
        session_id,
        prompt="Keep refactoring the ingestion job like we discussed.",
        reply="On it — I'll continue with the ingestion refactor now.",
    )

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=15_000)

    # pytest-asyncio auto mode owns the main-thread loop; drive the async
    # forwarder pipeline on its own loop in a worker thread.
    import threading

    error: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(_drive_forwarder_failure(base_url, session_id, tmp_path))
        except BaseException as exc:
            error.append(exc)

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(timeout=60)
    assert not error, f"forwarder drive failed: {error[0]!r}"

    pill = page.get_by_test_id("error-pill")
    expect(pill).to_have_count(1, timeout=15_000)
    pill.locator('button[aria-expanded="false"]').click()
    expect(pill.get_by_test_id("error-message-content")).to_contain_text(
        fwd._CODEX_REAUTH_HINT, timeout=10_000
    )
