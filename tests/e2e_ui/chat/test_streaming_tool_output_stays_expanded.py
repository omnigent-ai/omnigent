"""E2E: an expanded command output stays expanded while more output streams in.

Codex streams a running command's output as ``item/commandExecution/outputDelta``
notifications. Once the output passes the preview limit the card offers
Expand; each new chunk used to collapse it back to the preview, so a long
running command's full output could not be followed.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState, write_bridge_state

_THREAD_ID = "thread_output_expand"
_TURN_ID = "turn_output_expand"
_CALL_ID = "call_output_expand"
_COMMAND = "make release-build"
_FINAL_LINE = "release build complete\n"
# Past the 80-line preview, so the card offers Expand.
_FIRST_LINES = 100


def _event(method: str, **params: Any) -> dict[str, Any]:
    return {"method": method, "params": {"threadId": _THREAD_ID, "turnId": _TURN_ID, **params}}


def _line(n: int) -> str:
    return f"build step {n:03d} ok\n"


def _output(n: int) -> dict[str, Any]:
    return _event("item/commandExecution/outputDelta", itemId=_CALL_ID, delta=_line(n))


def _command(**fields: Any) -> dict[str, Any]:
    return {
        "type": "commandExecution",
        "id": _CALL_ID,
        "command": _COMMAND,
        "cwd": "/repo",
        **fields,
    }


class _CodexForwarder:
    """Feeds Codex app-server notifications through the real forwarder."""

    def __init__(self, base_url: str, session_id: str, bridge_dir: Path) -> None:
        bridge_dir.mkdir(parents=True)
        codex_home = bridge_dir / "codex-home"
        codex_home.mkdir()
        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id=session_id,
                socket_path=str(bridge_dir / "codex.sock"),
                thread_id=_THREAD_ID,
                codex_home=str(codex_home),
                active_turn_id=None,
                cwd=None,
            ),
        )
        self._session_id = session_id
        self._bridge_dir = bridge_dir
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._run(self._open(base_url))

    def _run(self, coro: Coroutine[Any, Any, None]) -> None:
        asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=30)

    async def _open(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
        self._state = codex_native_forwarder._CodexForwarderState(
            parent_session_id=self._session_id
        )
        self._tracker = codex_native_forwarder._CodexElicitationTaskTracker()
        self._usage = codex_native_forwarder._SessionUsageCoalescer(self._client, self._session_id)
        self._deltas = codex_native_forwarder._OutputTextDeltaCoalescer(
            self._client, self._session_id
        )

    def send(self, *events: dict[str, Any]) -> None:
        self._run(self._send(events))

    async def _send(self, events: tuple[dict[str, Any], ...]) -> None:
        for event in events:
            await codex_native_forwarder._handle_event(
                self._client,
                session_id=self._session_id,
                bridge_dir=self._bridge_dir,
                event=event,
                usage_coalescer=self._usage,
                elicitation_tracker=self._tracker,
                delta_coalescer=self._deltas,
                expected_thread_id=_THREAD_ID,
                forwarder_state=self._state,
            )
        await self._deltas.flush()

    def close(self) -> None:
        self._run(self._close())
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _close(self) -> None:
        await self._deltas.close()
        await self._usage.flush()
        await self._tracker.close()
        await self._client.aclose()


def test_expanded_command_output_stays_expanded_while_streaming(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)

    codex = _CodexForwarder(base_url, session_id, tmp_path / "bridge")
    try:
        codex.send(
            {
                "method": "turn/started",
                "params": {
                    "threadId": _THREAD_ID,
                    "turn": {"id": _TURN_ID, "status": "inProgress"},
                },
            },
            _event(
                "item/completed",
                item={
                    "type": "userMessage",
                    "id": "item_user_output_expand",
                    "content": [{"type": "text", "text": "Run the release build."}],
                },
            ),
            _event("item/started", item=_command(status="inProgress")),
            *(_output(n) for n in range(_FIRST_LINES)),
        )

        terminal = page.get_by_test_id("shell-tool-terminal")
        if not terminal.is_visible():
            page.locator('[data-slot="collapsible-trigger"]', has_text=_COMMAND).first.click()
        expect(terminal.get_by_text("Previewing output")).to_be_visible(timeout=30_000)
        page.get_by_role("button", name="Expand", exact=True).click()
        expect(page.get_by_text("Showing full output")).to_be_visible()

        codex.send(_output(_FIRST_LINES))
        expect(terminal.get_by_text(_line(_FIRST_LINES).strip())).to_be_visible(timeout=15_000)
        expect(page.get_by_text("Showing full output")).to_be_visible()

        # Only the completed item's aggregated output carries the last line.
        codex.send(
            _event(
                "item/completed",
                item=_command(
                    status="completed",
                    aggregatedOutput="".join(_line(n) for n in range(_FIRST_LINES + 1))
                    + _FINAL_LINE,
                    exitCode=0,
                ),
            )
        )
        expect(terminal.get_by_text(_FINAL_LINE.strip())).to_be_visible(timeout=15_000)
        expect(page.get_by_text("Showing full output")).to_be_visible()
    finally:
        codex.close()
