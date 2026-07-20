"""E2E: a pending approval surfaces on the /inbox page and resolves there.

The Inbox page (``web/src/pages/InboxPage.tsx``) gathers every pending
``response.elicitation_request`` across the user's sessions and renders each
as the same ``ApprovalCard`` the chat uses, with a local submit handler that
posts the verdict to the owning session. This test raises a gated-push
approval in a session, navigates to ``/inbox``, asserts the prompt is listed,
approves it from the inbox, and asserts the item drains (the row's pending
count drops to zero, so it falls out of the inbox).

Driven by the same ``approval_session`` fixture as the in-chat card test;
real LLM → nightly + generous timeout.

The second test
(:func:`test_reparked_elicitation_reliably_resurfaces_in_inbox`) covers
omnigent#927: when a hook retry re-parks the *same* elicitation id after the
user already approved it, the inbox card must drop its stale optimistic
verdict and resurface as an actionable pending card — not stay frozen on
"Approved" with no buttons. It drives the real claude-native permission hook
(``POST /v1/sessions/{id}/hooks/permission-request``) so the re-park is a
genuine server round-trip, and repeats the approve→re-park cycle with
randomized timing to walk the 4s websocket rescan window the bug raced
against.
"""

from __future__ import annotations

import contextlib
import random
import secrets
import threading
import time
from functools import partial

import httpx
import pytest
from playwright.sync_api import Page, expect

_COMPOSER = "Ask the agent anything…"
_APPROVAL_CARD = '[data-testid="approval-card"]'
_INBOX_ITEM = '[data-testid="inbox-item"]'
_AGENT_TURN_TIMEOUT_MS = 120_000

# Re-park stress knobs (test_reparked_elicitation_reliably_resurfaces_in_inbox).
# Several cycles, each with a randomized re-park delay, so the regression is
# exercised across the rescan window rather than at one lucky interleaving.
_REPARK_CYCLES = 3
# The session-list WS (``/v1/sessions/updates``) re-reads + diffs each watched
# session every 4s (``_SESSION_UPDATES_RESCAN_INTERVAL_S``). The re-park must
# land AFTER a rescan has observed the drained count, otherwise the rescan
# coalesces the 0→…→1 bounce into "no change" and never patches the row — the
# realistic case is a hook retry seconds later (after the server's re-park
# grace), not inside one tick.
_WS_RESCAN_S = 4.0
# Inbox-surface and clear assertions ride the WS count patch (≤4s rescan) plus
# a snapshot refetch, so allow well past that without masking a real hang.
_REPARK_TIMEOUT_MS = 30_000


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _permission_hook_payload(elicitation_id: str) -> dict:
    """Build a Claude PermissionRequest hook body that pins a stable id.

    Mirrors the ``omnigent claude`` wrapper's hook subprocess: the
    ``_omnigent_elicitation_id`` is minted once per prompt and re-sent on
    every retry POST, so the server re-parks the SAME elicitation. A plain
    tool name (``Bash``) with no ``requestedSchema`` renders the binary
    Approve/Reject ``ApprovalCard`` (not a form / plan / question card).

    :param elicitation_id: ``elicit_claude_`` + 32 hex chars.
    :returns: JSON-serializable PermissionRequest payload.
    """
    return {
        "session_id": "claude_sess_e2e",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/cwd",
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"},
        "tool_use_id": "tool_use_e2e",
        "_omnigent_elicitation_id": elicitation_id,
    }


def _park_permission_hook(
    base_url: str,
    session_id: str,
    elicitation_id: str,
    sink: dict,
) -> None:
    """Long-poll the permission hook in a worker thread; stash the verdict.

    The endpoint parks the elicitation (publishing the SSE the inbox renders)
    and blocks until the web UI delivers a verdict — exactly what the
    real hook subprocess does. Run from a thread so the test thread can drive
    Playwright and click Approve, which is what releases this POST. The
    verdict response (or any error) lands in *sink* for the test thread to
    assert on after the click.

    :param base_url: Live server base URL.
    :param session_id: Owning session id.
    :param elicitation_id: Stable id to (re-)park.
    :param sink: Mutable dict; gets ``"resp"`` (httpx.Response) or
        ``"error"`` (Exception).
    """
    try:
        sink["resp"] = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
            json=_permission_hook_payload(elicitation_id),
            timeout=120.0,
        )
    except Exception as exc:
        # Surfaced to the test thread via the sink rather than crashing a
        # daemon worker whose traceback the test would never see.
        sink["error"] = exc


def _is_parked(base_url: str, session_id: str, elicitation_id: str) -> bool:
    """True once the server snapshot lists *elicitation_id* as pending."""
    return any(
        e.get("elicitation_id") == elicitation_id
        for e in _pending_elicitations(base_url, session_id)
    )


def _settle_after_drain(
    page: Page,
    base_url: str,
    session_id: str,
    rng: random.Random,
) -> None:
    """Wait out an approval drain so the next park is a clean 0→1 diff.

    After an Approve the prompt drains and the card leaves the inbox. The
    session-list socket (``/v1/sessions/updates``) only patches a row when a
    rescan sees its dump change, and it rescans every ``_WS_RESCAN_S``. If the
    next park (count 0→1) lands before a rescan has registered the drain
    (count →0), the socket sees the same count it last sent and coalesces the
    bounce — the new prompt never surfaces. Block until the server reports the
    drain, then sleep one randomized rescan window so the socket records it
    before the next bump. (A re-park's ``updated_at`` does not move, so the
    socket's count diff is the only signal that brings the row back.)

    :param page: Playwright page (for the card-gone wait).
    :param base_url: Live server base URL.
    :param session_id: Owning session id.
    :param rng: Test RNG for the randomized settle delay.
    """
    expect(page.locator(_APPROVAL_CARD)).to_have_count(0, timeout=_REPARK_TIMEOUT_MS)
    _wait_for(lambda: not _pending_elicitations(base_url, session_id), timeout_s=30.0)
    # > one full rescan window past the drain, with margin for a loaded CI box
    # lagging the rescan loop, so the drain is always registered before the bump.
    time.sleep(rng.uniform(_WS_RESCAN_S + 2.0, _WS_RESCAN_S + 5.0))


def _park_in_thread(
    base_url: str,
    session_id: str,
    elicitation_id: str,
    workers: list[threading.Thread],
) -> dict:
    """Start a hook long-poll for *elicitation_id* and wait until it parks.

    :param base_url: Live server base URL.
    :param session_id: Owning session id.
    :param elicitation_id: Stable id to (re-)park.
    :param workers: Thread registry the test drains on teardown.
    :returns: The sink dict the worker writes its verdict/error into; it also
        carries the worker thread under ``"thread"`` for the later join.
    """
    sink: dict = {}
    worker = threading.Thread(
        target=_park_permission_hook,
        args=(base_url, session_id, elicitation_id, sink),
        daemon=True,
    )
    sink["thread"] = worker
    workers.append(worker)
    worker.start()
    _wait_for(partial(_is_parked, base_url, session_id, elicitation_id), timeout_s=30.0)
    return sink


def _assert_allow(sink: dict, label: str) -> None:
    """Join the parked long-poll and assert it returned an ``allow`` verdict.

    :param sink: Sink from :func:`_park_in_thread` (carries ``"thread"``).
    :param label: Phase label for assertion messages, e.g. ``"re-park 2"``.
    """
    sink["thread"].join(timeout=30)
    assert not sink["thread"].is_alive(), f"{label}: hook long-poll never returned after Approve"
    assert "error" not in sink, f"{label}: hook errored: {sink.get('error')!r}"
    assert sink["resp"].status_code == 200, f"{label}: {sink['resp'].text}"
    behavior = sink["resp"].json()["hookSpecificOutput"]["decision"]["behavior"]
    assert behavior == "allow", f"{label}: expected allow, got {behavior!r}"


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_pending_approval_surfaces_and_resolves_in_inbox(
    page: Page,
    approval_session: tuple[str, str],
) -> None:
    """Gated tool call → /inbox lists the prompt → Approve there → it drains."""
    base_url, session_id = approval_session

    # Raise the approval from the chat surface, then leave it pending.
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the command now.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first).to_be_visible(
        timeout=_AGENT_TURN_TIMEOUT_MS
    )
    # Confirm the server is parked before we navigate away.
    _wait_for(lambda: bool(_pending_elicitations(base_url, session_id)))

    # The inbox gathers the prompt from the session's snapshot.
    page.goto(f"{base_url}/inbox")
    item = page.locator(_INBOX_ITEM).first
    expect(item).to_be_visible(timeout=30_000)
    card = item.locator(_APPROVAL_CARD)
    expect(card).to_be_visible()
    expect(card.get_by_text("Approval required")).to_be_visible()

    # Approve from the inbox: the verdict routes to the owning session, the
    # server drains the prompt, and the row's pending count drops to zero so
    # the item falls out of the inbox.
    card.get_by_role("button", name="Approve").click()
    _wait_for(lambda: not _pending_elicitations(base_url, session_id))
    expect(page.locator(_INBOX_ITEM)).to_have_count(0, timeout=30_000)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_reparked_elicitation_reliably_resurfaces_in_inbox(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A re-parked, already-approved elicitation resurfaces as an actionable card.

    Regression for omnigent#927. When the user approves an inbox approval, the
    page flips it to "Approved" optimistically (``responded`` keyed by
    elicitation id). When a hook retry later re-parks the SAME id (the server
    re-parks after its disconnect/grace window), the session snapshot lists it
    as pending again — but the stale ``responded`` entry pinned the card to
    "Approved" with no buttons, leaving a headless sub-agent invisibly blocked.
    The fix sweeps verdicts whose id is pending again whenever a snapshot
    refresh lands (``dataUpdatedAt`` advances).

    Recreated end to end against the live server: one elicitation id is parked,
    approved in a real browser, then re-parked again and again through the real
    claude-native permission hook (``POST .../hooks/permission-request``) — the
    omnigent#927 shape of a single prompt whose hook keeps re-parking the same
    id. Each retry must bring the card back as a *pending* card
    (``data-state="pending"`` with an Approve button), never a frozen
    "Approved" one. Several retries with randomized delays prove the resurface
    is robust, not a one-off interleaving.

    Timing note: each retry is issued only after the inbox has observed the
    approval drain the count to zero (card gone) plus one ``_WS_RESCAN_S``
    window, so the session-list socket registers the drop before the bump —
    exactly the real flow where the retry arrives seconds later. Issuing it
    inside a single rescan tick is a *separate* coalescing gap (the socket
    never reports the bounce, and the row's ``updated_at`` does not move on a
    re-park) that this fix does not address; this test stays on the path the
    fix governs.

    No real LLM: the hook endpoint parks elicitations directly, which is also
    the only way to deterministically re-park the *same* id (a model-driven
    gate mints a fresh id per call). Nightly + generous timeout to match the
    other live-server UI suites.
    """
    base_url, session_id = seeded_session

    # One mount, kept for the whole test: the optimistic ``responded`` map is
    # in-memory page state, so a reload between retries would wash out the very
    # stale verdict the regression is about. Navigate once, never reload.
    page.goto(f"{base_url}/inbox")
    expect(page.get_by_text("Nothing waiting on you")).to_be_visible(timeout=_REPARK_TIMEOUT_MS)

    # A single elicitation id, re-parked repeatedly — the omnigent#927 scenario
    # is one prompt whose hook keeps re-parking the SAME id after each approval.
    eid = f"elicit_claude_{secrets.token_hex(16)}"
    rng = random.Random()
    workers: list[threading.Thread] = []
    try:
        # Initial park + approve: surfaces the card and sets the optimistic
        # "Approved" verdict the later retries must not get stuck behind.
        sink = _park_in_thread(base_url, session_id, eid, workers)
        first = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        expect(first).to_be_visible(timeout=_REPARK_TIMEOUT_MS)
        first.get_by_role("button", name="Approve", exact=True).click()
        _assert_allow(sink, "initial park")
        _settle_after_drain(page, base_url, session_id, rng)

        for cycle in range(_REPARK_CYCLES):
            # A hook retry re-parks the SAME id.
            sink = _park_in_thread(base_url, session_id, eid, workers)

            # ── REGRESSION: the re-parked prompt must be actionable again. ──
            # The card returns either way; the bug is its STATE. Pre-fix the
            # stale verdict freezes it at data-state="responded" with no
            # buttons (the data-state assertion below times out); post-fix the
            # snapshot refresh sweeps the verdict and it reverts to pending with
            # Approve restored.
            resurfaced = page.locator(_APPROVAL_CARD)
            expect(resurfaced).to_have_count(1, timeout=_REPARK_TIMEOUT_MS)
            expect(resurfaced).to_have_attribute(
                "data-state", "pending", timeout=_REPARK_TIMEOUT_MS
            )
            expect(resurfaced.get_by_role("button", name="Approve", exact=True)).to_be_visible()

            # Approve again (re-arming the stale verdict), then settle so the
            # next retry is a clean 0→1 diff the socket won't coalesce.
            resurfaced.get_by_role("button", name="Approve", exact=True).click()
            _assert_allow(sink, f"re-park {cycle}")
            _settle_after_drain(page, base_url, session_id, rng)
    finally:
        # Release any still-parked long-poll so its worker thread exits even if
        # an assertion above failed mid-cycle (e.g. the regression timed out
        # with the prompt still parked). Daemon threads + the fixture's session
        # delete are the final backstop.
        for pending in _pending_elicitations(base_url, session_id):
            eid = pending.get("elicitation_id")
            if not eid:
                continue
            with contextlib.suppress(Exception):
                httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/elicitations/{eid}/resolve",
                    json={"action": "decline"},
                    timeout=10.0,
                )
        for worker in workers:
            worker.join(timeout=5)
