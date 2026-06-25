"""Process-local record of the most recent native-forwarder event-POST failure.

A native-harness subprocess serves exactly one conversation (see
``app.state.conversation_id`` in ``omnigent/runtime/harnesses/_scaffold.py``),
and its transcript forwarder runs as an ``asyncio`` task in the SAME event loop
as the harness idle-turn watchdog. When the watchdog fires after a stall, the
real cause is often that the forwarder could not POST session events to the
server (e.g. ``ConnectError: No route to host``) — which is logged separately
and otherwise lost, so the user only sees a generic "wedged LLM" reason.

The forwarder records its last exhausted POST failure here so the watchdog can
attach the connectivity cause to the turn-failure reason (issue #1119). The
record is process-global rather than session-keyed because one subprocess
serves exactly one conversation; it carries a monotonic timestamp so the
watchdog only blames connectivity for a failure recent enough to plausibly be
the cause of the stall.
"""

from __future__ import annotations

import time

# Single-slot holder rather than a module global + ``global`` statement: one
# conversation per subprocess means one "last failure" is unambiguous. The
# tuple is ``(monotonic_timestamp, human_detail)``; ``None`` until a forwarder
# POST exhausts its retries.
_state: dict[str, tuple[float, str] | None] = {"last_post_failure": None}


def record_post_failure(event_type: str, error: BaseException) -> None:
    """
    Record a native-forwarder event-POST failure that exhausted its retries.

    Called from the forwarder's post path after all retries fail (a persistent
    failure such as a connectivity outage — transient single failures that
    recover are not recorded). Overwrites any prior record; only the most
    recent failure is kept.

    :param event_type: Session event type that failed to post, e.g.
        ``"external_conversation_item"`` or ``"external_session_status"``.
    :param error: The final transport error, e.g. an
        ``httpx.ConnectError``. Rendered with ``repr`` into the detail string.
    :returns: None.
    """
    _state["last_post_failure"] = (time.monotonic(), f"{event_type}: {error!r}")


def recent_post_failure(within_s: float) -> str | None:
    """
    Return the most recent forwarder POST failure detail, if recent enough.

    :param within_s: Recency window in seconds. A recorded failure older than
        this is ignored so a long-resolved blip is not blamed for a fresh
        stall. Pass the watchdog's idle window so only a failure that occurred
        during the stall is surfaced.
    :returns: A human-readable detail string (``"<event_type>: <repr>"``) when
        a failure was recorded within *within_s* seconds, else ``None``.
    """
    record = _state["last_post_failure"]
    if record is None:
        return None
    recorded_at, detail = record
    if time.monotonic() - recorded_at > within_s:
        return None
    return detail


def clear() -> None:
    """
    Forget any recorded forwarder POST failure.

    Lets a test isolate from earlier records; harmless in production (the next
    failure overwrites the slot regardless).

    :returns: None.
    """
    _state["last_post_failure"] = None
