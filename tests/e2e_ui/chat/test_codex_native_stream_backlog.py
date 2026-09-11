"""Codex web UI falls behind during high-volume native streaming.

During a high-volume codex-native turn the TUI keeps producing output while
the web transcript trickles in far behind, because the forwarder posts
(nearly) one transcript-relay POST per streamed delta:

- ``item/reasoning/textDelta`` bypasses the text coalescer entirely — one
  awaited relay POST per delta.
- ``item/commandExecution/outputDelta`` rides the coalescer but is flushed
  on every newline — one POST per output line.
- ``item/agentMessage/delta`` coalesces only up to a 64-character
  threshold — one POST per ~64 characters of answer text.

The relay POSTs are serialized (per-delta inline awaits and a single FIFO
worker), so when the relay is transiently slow the backlog drains far more
slowly than Codex emits, and the web UI looks stuck until it catches up.

Journey driven here (mirroring ``test_codex_native_subagent_activity.py``):
open the session page in the SPA, then feed the real forwarder
(``_handle_event``) the exact notification stream shape a Codex TUI
app-server emits for one high-volume turn — reasoning deltas, command
output lines, and assistant text deltas — against the live spawned server.
A small per-request latency on the forwarder's HTTP transport stands in for
the reported transient relay slowness; the POST-count assertions are
latency-independent, so the regression guard is deterministic.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
)

_THREAD_ID = "thread_hv_stream"
_TURN_ID = "turn_hv_stream"

# High-volume turn shape: tiny deltas, the way Codex streams them.
_N_REASONING_DELTAS = 150
_N_COMMAND_LINES = 150
_N_TEXT_DELTAS = 240
_MARKER = "HIGH-VOLUME-STREAM-END"

# Simulated transient relay slowness per request. Only stretches the
# user-visible drain time; every count asserted below is unaffected by it.
_RELAY_LATENCY_SECONDS = 0.02

# Aggregate relay budget for the whole scripted turn. Today's per-delta
# behavior spends ~330 POSTs on it; coalesced streaming needs a few dozen.
_TOTAL_RELAY_POST_BUDGET = 120


class _CountingLatencyTransport(httpx.AsyncBaseTransport):
    """Delegate to a real transport, counting and slowing relay event POSTs.

    :param latency_seconds: Added delay per ``POST .../events`` request,
        standing in for a transiently slow transcript relay.
    :param counts: Counter keyed by posted event ``type``.
    """

    def __init__(self, latency_seconds: float, counts: Counter[str]) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self._latency_seconds = latency_seconds
        self._counts = counts

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            try:
                payload = json.loads(request.content.decode("utf-8"))
                event_type = payload.get("type", "<untyped>")
            except (ValueError, UnicodeDecodeError):
                event_type = "<unparsed>"
            self._counts[event_type] += 1
            await asyncio.sleep(self._latency_seconds)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _scripted_high_volume_turn() -> tuple[list[dict[str, object]], str]:
    """Build the Codex app-server notification stream for one big turn.

    :returns: ``(events, full_answer_text)`` where ``events`` is the ordered
        notification list and ``full_answer_text`` the completed assistant
        message the deltas add up to.
    """
    events: list[dict[str, object]] = [
        {
            "method": "turn/started",
            "params": {
                "threadId": _THREAD_ID,
                "turn": {"id": _TURN_ID, "status": "inProgress"},
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": _THREAD_ID,
                "turnId": _TURN_ID,
                "item": {
                    "type": "userMessage",
                    "id": "item_user_hv_stream",
                    "content": [
                        {
                            "type": "text",
                            "text": "Run the full release build and summarize the output.",
                        }
                    ],
                },
            },
        },
    ]
    for i in range(_N_REASONING_DELTAS):
        events.append(
            {
                "method": "item/reasoning/textDelta",
                "params": {
                    "threadId": _THREAD_ID,
                    "turnId": _TURN_ID,
                    "itemId": "item_reasoning_hv_stream",
                    "delta": f"considering step {i:03d} ",
                },
            }
        )
    for i in range(_N_COMMAND_LINES):
        events.append(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "threadId": _THREAD_ID,
                    "turnId": _TURN_ID,
                    "itemId": "call_build_hv_stream",
                    "delta": f"compiling module {i:03d} ... ok\n",
                },
            }
        )
    words = [f"wd{i:04d} " for i in range(_N_TEXT_DELTAS)]
    words.append(_MARKER)
    for word in words:
        events.append(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": _THREAD_ID,
                    "turnId": _TURN_ID,
                    "itemId": "item_agent_hv_stream",
                    "delta": word,
                },
            }
        )
    full_text = "".join(words)
    events.append(
        {
            "method": "item/completed",
            "params": {
                "threadId": _THREAD_ID,
                "turnId": _TURN_ID,
                "item": {
                    "type": "agentMessage",
                    "id": "item_agent_hv_stream",
                    "text": full_text,
                },
            },
        }
    )
    events.append(
        {
            "method": "turn/completed",
            "params": {
                "threadId": _THREAD_ID,
                "turn": {"id": _TURN_ID, "status": "completed", "items": []},
            },
        }
    )
    return events, full_text


async def _drive_high_volume_turn(
    base_url: str, session_id: str, bridge_dir: Path
) -> dict[str, object]:
    """Feed the scripted turn through the real forwarder against the server.

    Seeds the bridge state exactly like a native Codex launch does, then
    hands every notification to ``_handle_event`` with the production
    coalescer configuration, over a latency-injected counting transport.

    :param base_url: Live spawned server base URL.
    :param session_id: Seeded session (conversation) id.
    :param bridge_dir: Temp dir standing in for the native bridge dir.
    :returns: Metrics: relay POST ``counts`` and the drain wall-clock.
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    codex_home = bridge_dir / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
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

    counts: Counter[str] = Counter()
    transport = _CountingLatencyTransport(_RELAY_LATENCY_SECONDS, counts)
    state = codex_native_forwarder._CodexForwarderState(parent_session_id=session_id)
    tracker = codex_native_forwarder._CodexElicitationTaskTracker()
    events, _ = _scripted_high_volume_turn()

    started = time.monotonic()
    async with httpx.AsyncClient(base_url=base_url, transport=transport, timeout=30.0) as client:
        usage_coalescer = codex_native_forwarder._SessionUsageCoalescer(client, session_id)
        delta_coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(client, session_id)
        try:
            for event in events:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id=session_id,
                    bridge_dir=bridge_dir,
                    event=event,
                    usage_coalescer=usage_coalescer,
                    elicitation_tracker=tracker,
                    delta_coalescer=delta_coalescer,
                    expected_thread_id=_THREAD_ID,
                    forwarder_state=state,
                )
        finally:
            await delta_coalescer.close()
            await usage_coalescer.flush()
            await tracker.close()
    return {
        "counts": counts,
        "drain_seconds": time.monotonic() - started,
        "n_events": len(events),
    }


def test_high_volume_codex_native_stream_is_coalesced(
    page: Page,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """One high-volume Codex turn must not spend a relay POST per delta.

    Fails on the per-delta relay posting behavior: ~1 POST per reasoning delta,
    ~1 POST per command output line, and 64-char text batches make the
    serialized relay queue (and therefore the web transcript) fall far
    behind the TUI whenever the relay slows down.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)

    with ThreadPoolExecutor(max_workers=1) as executor:
        metrics = executor.submit(
            asyncio.run,
            _drive_high_volume_turn(base_url, session_id, tmp_path / "bridge"),
        ).result()

    # Journey end state: the web transcript did (eventually) catch up and
    # shows the completed answer — the report's symptom is lag, not loss.
    expect(page.get_by_text(_MARKER).first).to_be_visible(timeout=30_000)

    counts: Counter[str] = metrics["counts"]  # type: ignore[assignment]
    reasoning_posts = counts.get("external_output_reasoning_delta", 0)
    tool_output_posts = counts.get("external_tool_output_delta", 0)
    text_posts = counts.get("external_output_text_delta", 0)
    total_posts = sum(counts.values())
    print(
        "codex-native stream relay metrics: "
        f"total_posts={total_posts} reasoning_posts={reasoning_posts} "
        f"tool_output_posts={tool_output_posts} text_posts={text_posts} "
        f"drain_seconds={metrics['drain_seconds']:.2f} "
        f"n_events={metrics['n_events']} counts={dict(counts)}"
    )

    assert reasoning_posts <= _N_REASONING_DELTAS // 4, (
        f"{reasoning_posts} relay POSTs for {_N_REASONING_DELTAS} reasoning deltas: "
        "reasoning deltas are posted one-by-one instead of coalesced, so a slow "
        f"relay backlogs the web UI (drain took {metrics['drain_seconds']:.2f}s "
        f"at {_RELAY_LATENCY_SECONDS * 1000:.0f}ms simulated relay latency)"
    )
    assert tool_output_posts <= _N_COMMAND_LINES // 4, (
        f"{tool_output_posts} relay POSTs for {_N_COMMAND_LINES} command output "
        "lines: every newline forces a command-output flush, so line-oriented "
        "output posts once per line and backlogs the web UI"
    )
    assert text_posts <= 12, (
        f"{text_posts} relay POSTs for {_N_TEXT_DELTAS + 1} assistant text deltas: "
        "the 64-character batch threshold posts once per ~64 chars of answer text"
    )
    assert total_posts <= _TOTAL_RELAY_POST_BUDGET, (
        f"{total_posts} relay POSTs for one scripted high-volume turn "
        f"(budget {_TOTAL_RELAY_POST_BUDGET}): per-delta posting amplifies "
        "transient relay slowness into a client-visible transcript backlog"
    )
