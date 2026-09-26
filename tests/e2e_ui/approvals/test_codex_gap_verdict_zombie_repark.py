r"""E2E (UI): a codex gap-landing approve must reach the re-park.

The codex-native harness drives an operator gate by long-polling
``POST /v1/sessions/{sid}/hooks/codex-elicitation-request`` in chunks, sending
the SAME JSON-RPC envelope every chunk, so the server-side elicitation id
(``codex_elicitation_id(session, method, rpc_id)``) is stable across re-parks.
A proxy can sever a chunk client-side while holding the backend connection
open, so the harness abandons the chunk but the server never detects the
disconnect: the previous chunk's waiter stays parked as a zombie.

If the operator approves the web card in that state, the verdict is set on the
zombie waiter's Future and written to the abandoned connection — nobody reads
it, and no pre-resolved tombstone is written (``_resolve_elicitation`` only
tombstones when NO future is registered for the id). The harness's next chunk
re-parks the same stable id, finds nothing, and re-publishes the same gate as
a fresh pending card: the operator's answer is lost and they must answer twice.

This test drives the real journey through the SPA: park chunk 1 (kept open —
the undetected-sever state), approve the card in the browser, then re-POST the
same envelope as chunk 2 and require the verdict to be delivered to it, with
no re-asked pending card. It FAILS on the unfixed build (chunk 2 parks and the
card re-appears) and passes once a gap-landing verdict survives to the re-park.

Sibling of ``test_ask_user_question.py`` in structure: a synthetic hook POST
against a seeded session plus the real SPA — no real Codex CLI, no LLM,
seconds not minutes.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

_APPROVAL_CARD = '[data-testid="approval-card"]'
_CARD_TIMEOUT_MS = 15_000

# One codex command-approval gate. The harness re-POSTs this SAME envelope for
# every long-poll chunk (stable JSON-RPC id 12), so every chunk addresses the
# same logical elicitation server-side.
_CODEX_GATE: dict[str, Any] = {
    "id": 12,
    "method": "item/commandExecution/requestApproval",
    "params": {
        "threadId": "thread_gapverdict",
        "turnId": "turn_gapverdict",
        "itemId": "item_cmd_gapverdict",
        "startedAtMs": 1,
        "approvalId": None,
        "reason": "zombie-poll gap reproduction",
        "command": "date",
        "cwd": "/tmp/workspace",
        "commandActions": [],
    },
}

# Chunk 1 models the proxy-held backend connection: the client keeps it open
# (the server never sees a disconnect) but the harness has stopped caring.
_CHUNK1_CLIENT_TIMEOUT_S = 120.0
# Chunk 2 is the live re-park that must receive the verdict. On the unfixed
# build it parks for the hook's full 86400s timeout, so a short client budget
# converts "verdict lost" into a deterministic failure.
_CHUNK2_CLIENT_TIMEOUT_S = 10.0


def _post_gate_chunk(
    base_url: str,
    session_id: str,
    holder: dict,
    *,
    timeout_s: float,
) -> threading.Thread:
    """POST one long-poll chunk of the codex gate on a background thread.

    The call blocks server-side until a verdict (or the client timeout)
    lands, so it runs on its own thread; the thread writes ``response`` or
    ``error`` into *holder*.

    :param base_url: Server base URL.
    :param session_id: Session to raise the gate on.
    :param holder: Dict the thread writes ``response`` / ``error`` into.
    :param timeout_s: Client-side read timeout for this chunk.
    :returns: The started thread.
    """

    def _post() -> None:
        try:
            holder["response"] = httpx.post(
                f"{base_url}/v1/sessions/{session_id}/hooks/codex-elicitation-request",
                json=_CODEX_GATE,
                timeout=timeout_s,
            )
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=_post, daemon=True)
    thread.start()
    return thread


@pytest.mark.timeout(180)
def test_gap_landing_approve_is_delivered_to_the_repark(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Approve during an undetected-sever gap → the re-park gets the verdict.

    Journey: codex raises a gate (chunk 1 parks) → the harness abandons the
    chunk while the proxy holds the backend connection (undetected sever) →
    the operator approves the web card → the harness re-parks the same stable
    envelope (chunk 2). The verdict must reach chunk 2; the same gate must NOT
    re-appear as a fresh pending card.
    """
    base_url, session_id = seeded_session

    # Chunk 1: the harness's long-poll. Client-side it will be abandoned (we
    # never act on its result); server-side its waiter stays parked because
    # the connection is still open — the undetected-sever state.
    chunk1: dict = {}
    _post_gate_chunk(base_url, session_id, chunk1, timeout_s=_CHUNK1_CLIENT_TIMEOUT_S)

    page.goto(f"{base_url}/c/{session_id}")
    pending = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(pending).to_be_visible(timeout=_CARD_TIMEOUT_MS)

    # The operator answers the card. Server-side the verdict lands on the
    # zombie waiter and is written to the abandoned connection; the harness
    # never reads it.
    pending.get_by_role("button", name="Approve").click()
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first).to_be_visible(
        timeout=_CARD_TIMEOUT_MS
    )

    # Chunk 2: the harness re-invokes with the same stable envelope. A correct
    # server hands it the gap-landing verdict; the unfixed one re-parks it and
    # re-publishes the same gate as pending.
    chunk2: dict = {}
    chunk2_thread = _post_gate_chunk(
        base_url, session_id, chunk2, timeout_s=_CHUNK2_CLIENT_TIMEOUT_S
    )
    chunk2_thread.join(timeout=_CHUNK2_CLIENT_TIMEOUT_S + 5)

    repark_pending = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
    reasked = repark_pending.count() > 0

    response = chunk2.get("response")
    assert response is not None and response.status_code == 200, (
        "gap-landing verdict LOST: the re-park never received the operator's "
        f"approve (chunk 2 outcome: error={chunk2.get('error')!r}); "
        f"gate re-asked as a fresh pending card: {reasked}"
    )
    assert response.json() == {"decision": "accept"}, (
        f"re-park returned {response.text!r} instead of the approve verdict"
    )
    # And the user-facing symptom: the answered gate must not come back.
    expect(repark_pending).to_have_count(0)
