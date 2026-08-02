"""Escalate native-forwarder poll / supervisor failures to durable session status.

Native forwarders historically caught poll-loop exceptions, logged them, slept,
and continued. The UI only reacts to durable ``session.status`` writes, so a
hung TUI or repeatedly-crashing forwarder left the chat frozen with no failed
card. This module is the single escalation path every forwarder must use.

Policy:
- After ``POLL_FAILURE_THRESHOLD`` consecutive poll failures, POST
  ``external_session_status: failed``.
- Permanent error classes escalate on the first failure.
- After ``RESTART_FAILURE_THRESHOLD`` supervisor restarts within
  ``RESTART_FAILURE_WINDOW_S``, POST the same durable failure.
- A successful poll / healthy supervisor uptime resets the counters so
  transient blips do not accumulate into a false failure card.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import httpx

from omnigent._native_post_delivery import post_external_session_status

_logger = logging.getLogger(__name__)

# Consecutive poll-loop exceptions before a durable failed status.
POLL_FAILURE_THRESHOLD = 5
# Supervisor crashes within the window before escalating.
RESTART_FAILURE_THRESHOLD = 3
RESTART_FAILURE_WINDOW_S = 300.0

# Errors that will not recover by sleeping and retrying the same poll.
PERMANENT_POLL_ERROR_TYPES: tuple[type[BaseException], ...] = (
    PermissionError,
    NotADirectoryError,
)


PostStatusFn = Callable[..., Awaitable[None]]


@dataclass
class PollFailureTracker:
    """Consecutive poll-failure counter for one forwarder run.

    :param consecutive_failures: Failures since the last successful poll.
    :param failed_status_emitted: Whether a durable ``failed`` was already
        posted for this streak (avoids spamming the server every poll).
    """

    consecutive_failures: int = 0
    failed_status_emitted: bool = False


@dataclass
class RestartFailureTracker:
    """Sliding-window supervisor-restart tracker for one session.

    :param restart_at: Monotonic timestamps of recent crashes / unexpected
        returns, oldest first.
    :param failed_status_emitted: Whether a durable ``failed`` was already
        posted for the current outage window.
    """

    restart_at: list[float] = field(default_factory=list)
    failed_status_emitted: bool = False


def is_permanent_poll_error(
    error: BaseException,
    *,
    permanent_types: Sequence[type[BaseException]] = PERMANENT_POLL_ERROR_TYPES,
) -> bool:
    """
    Return whether ``error`` should fail the session on the first poll miss.

    :param error: Exception raised by a forwarder poll iteration.
    :param permanent_types: Exception types treated as non-recoverable.
    :returns: ``True`` when the error should escalate immediately.
    """
    return isinstance(error, tuple(permanent_types))


def note_poll_success(tracker: PollFailureTracker) -> None:
    """
    Clear the poll-failure streak after a successful iteration.

    :param tracker: Per-forwarder poll failure tracker.
    :returns: None.
    """
    tracker.consecutive_failures = 0
    tracker.failed_status_emitted = False


def note_supervisor_healthy(tracker: RestartFailureTracker) -> None:
    """
    Clear restart escalation after a healthy forwarder uptime window.

    :param tracker: Per-session supervisor restart tracker.
    :returns: None.
    """
    tracker.restart_at.clear()
    tracker.failed_status_emitted = False


async def handle_poll_failure(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    tracker: PollFailureTracker,
    error: BaseException,
    harness: str,
    threshold: int = POLL_FAILURE_THRESHOLD,
    permanent_types: Sequence[type[BaseException]] = PERMANENT_POLL_ERROR_TYPES,
    post_status: PostStatusFn | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """
    Record a poll-loop exception and POST ``failed`` when the policy trips.

    :param client: Omnigent HTTP client used for the status POST.
    :param session_id: Omnigent session/conversation id.
    :param tracker: Per-forwarder poll failure tracker.
    :param error: Exception raised by the poll iteration.
    :param harness: Short harness label for the failure message, e.g.
        ``"claude-native"``.
    :param threshold: Consecutive failures required for a transient error.
    :param permanent_types: Exception types that escalate immediately.
    :param post_status: Optional override for :func:`post_external_session_status`
        (tests inject a recorder).
    :param logger: Optional logger; defaults to this module's logger.
    :returns: ``True`` when a durable ``failed`` status was posted this call.
    """
    log = logger or _logger
    tracker.consecutive_failures += 1
    permanent = is_permanent_poll_error(error, permanent_types=permanent_types)
    should_fail = permanent or tracker.consecutive_failures >= threshold
    if not should_fail or tracker.failed_status_emitted:
        return False

    reason = (
        f"{harness} forwarder permanent poll failure: {error!r}"
        if permanent
        else (
            f"{harness} forwarder failed {tracker.consecutive_failures} "
            f"consecutive polls: {error!r}"
        )
    )
    posted = await _post_failed_status(
        client=client,
        session_id=session_id,
        reason=reason,
        post_status=post_status,
        logger=log,
    )
    if posted:
        tracker.failed_status_emitted = True
    return posted


async def handle_supervisor_restart(
    *,
    client: httpx.AsyncClient | None,
    session_id: str,
    tracker: RestartFailureTracker,
    error: BaseException | None,
    harness: str,
    threshold: int = RESTART_FAILURE_THRESHOLD,
    window_s: float = RESTART_FAILURE_WINDOW_S,
    now: float | None = None,
    post_status: PostStatusFn | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """
    Record a supervisor restart and POST ``failed`` when the window trips.

    :param client: Omnigent HTTP client, or ``None`` when the crash happened
        before a client existed (escalation is skipped until a client is
        available; the restart is still counted).
    :param session_id: Omnigent session/conversation id.
    :param tracker: Per-session supervisor restart tracker.
    :param error: Crash exception, or ``None`` for an unexpected normal return.
    :param harness: Short harness label for the failure message.
    :param threshold: Restarts within ``window_s`` that trigger escalation.
    :param window_s: Sliding window length in seconds.
    :param now: Optional monotonic timestamp (tests inject a clock).
    :param post_status: Optional override for the status POST.
    :param logger: Optional logger; defaults to this module's logger.
    :returns: ``True`` when a durable ``failed`` status was posted this call.
    """
    log = logger or _logger
    stamp = time.monotonic() if now is None else now
    tracker.restart_at.append(stamp)
    cutoff = stamp - window_s
    tracker.restart_at = [t for t in tracker.restart_at if t >= cutoff]
    if len(tracker.restart_at) < threshold or tracker.failed_status_emitted:
        return False
    if client is None:
        log.warning(
            "%s forwarder supervisor exceeded restart budget but has no HTTP "
            "client to publish failed status; session=%s restarts=%d",
            harness,
            session_id,
            len(tracker.restart_at),
        )
        return False

    detail = f"{error!r}" if error is not None else "unexpected return"
    reason = (
        f"{harness} forwarder restarted {len(tracker.restart_at)} times within "
        f"{window_s:.0f}s: {detail}"
    )
    posted = await _post_failed_status(
        client=client,
        session_id=session_id,
        reason=reason,
        post_status=post_status,
        logger=log,
    )
    if posted:
        tracker.failed_status_emitted = True
    return posted


async def _post_failed_status(
    *,
    client: httpx.AsyncClient,
    session_id: str,
    reason: str,
    post_status: PostStatusFn | None,
    logger: logging.Logger,
) -> bool:
    """
    Best-effort POST of ``external_session_status: failed``.

    :returns: ``True`` when the POST succeeded.
    """
    poster = post_status or post_external_session_status
    try:
        await poster(
            client,
            session_id=session_id,
            status="failed",
            output=reason,
        )
    except Exception as exc:  # noqa: BLE001 — status POST must not kill the loop
        logger.warning(
            "Failed to publish forwarder failure status; session=%s reason=%s error=%r",
            session_id,
            reason,
            exc,
            exc_info=True,
        )
        return False
    return True
