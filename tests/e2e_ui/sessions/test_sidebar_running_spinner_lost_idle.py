"""A session whose runner is alive but holds no turn must not stay ``running``
in ``GET /v1/sessions`` after its terminal-idle edge is lost.

The sidebar badge renders its spinner purely from
``conversation.status == "running"`` in the list, so a stuck spinner is a
stuck list status; this asserts it at that layer. The invariant needs a *live*
runner: the orphan-reconcile backstop only settles a stuck row once the runner
is confirmed gone, so a DB seed with a fake runner (see
``tests/server/routes/test_orphaned_running_session_reconcile.py``) cannot
express it. This binds to the e2e lane's real runner and exercises the real
list -> tunnel -> runner -> list reconcile.
"""

from __future__ import annotations

import time

import httpx

# The settle is a background probe scheduled by the list read, so a healthy run
# clears within a poll or two. The deadline spans more than one 30s probe
# cooldown so a single transient probe failure retries instead of failing.
SETTLE_DEADLINE_SECONDS = 75.0
POLL_INTERVAL_SECONDS = 1.0


def _list_status(base_url: str, session_id: str) -> str | None:
    resp = httpx.get(f"{base_url}/v1/sessions", params={"visibility": "all"}, timeout=10.0)
    resp.raise_for_status()
    item = next((s for s in resp.json()["data"] if s["id"] == session_id), None)
    return item["status"] if item else None


def _post_lost_idle_running(base_url: str, session_id: str) -> None:
    """Post a lone ``running`` status with no following ``idle`` over the real
    external_session_status wire: a lost terminal-idle edge."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": {"status": "running"}},
        timeout=15.0,
    )
    resp.raise_for_status()


def test_list_settles_lost_idle_running_session_with_live_runner(
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session

    assert _list_status(base_url, session_id) in (None, "idle"), (
        "a freshly bound session with no active work should not start running"
    )

    _post_lost_idle_running(base_url, session_id)
    assert _list_status(base_url, session_id) == "running", (
        "the lost terminal-idle edge should land as a running status"
    )

    # The runner is alive and has no in-flight turn, so the list must stop
    # reporting running (the spinner must stop) rather than spin forever.
    start = time.monotonic()
    polls: list[str] = []
    status = "running"
    while time.monotonic() - start < SETTLE_DEADLINE_SECONDS:
        status = _list_status(base_url, session_id) or "idle"
        polls.append(f"t={time.monotonic() - start:.1f}s {status}")
        if status != "running":
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    assert status != "running", (
        f"GET /v1/sessions still reports running after {SETTLE_DEADLINE_SECONDS:.0f}s "
        "for a live-runner session with no active work; the sidebar spinner never "
        f"stops. Polls: {polls}"
    )
