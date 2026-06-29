"""Delivery-ambiguity classification and shared retry loop for native-forwarder
event POSTs.

The claude-native, codex-native, and antigravity-native forwarders mirror
transcript items into AP as ``external_conversation_item`` POSTs. The server
persists those with a random primary key and does NOT dedupe them — producers
are responsible for not re-posting items they have already sent. That makes a
blind retry after a failed POST unsafe: if the server committed the item and
published ``session.input.consumed`` but the response was lost, a retry appends
a second copy and the web UI renders a duplicate bubble. The native tmux pane
is unaffected, which is why the duplicate is web-only.

:func:`post_may_have_been_delivered` is the shared classifier all forwarders
use to decide whether a failed POST is safe to retry.

:func:`post_session_event_with_retry` is the shared retry loop extracted from
the codex/antigravity forwarders so a single implementation is maintained.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Coroutine
from pathlib import Path

import httpx

_logger = logging.getLogger(__name__)

# Dead-letter sink for permanently-undeliverable forward payloads (#1120).
_DEAD_LETTER_FILE = "dead_letter.jsonl"
# When the active file reaches this size it is rotated to a single ``.1``
# backup and a fresh file is started, so the most recent drops are always
# retained (keep-newest). Disk stays bounded at ~2x this size (current + .1).
_DEAD_LETTER_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per session
_DEAD_LETTER_BACKUP_FILE = _DEAD_LETTER_FILE + ".1"


def append_dead_letter(
    bridge_dir: Path,
    *,
    session_id: str,
    event_type: str,
    payload: dict[str, object],
    reason: str,
) -> None:
    """
    Append one undeliverable forward payload to ``{bridge_dir}/dead_letter.jsonl`` (#1120).

    Write-only recovery artifact so a permanently-failed transcript/usage POST is
    recoverable on disk instead of silently lost. Replay is tracked separately (#1579).
    Best-effort: never raises (a dead-letter failure must not disrupt forwarding). When
    the file reaches :data:`_DEAD_LETTER_MAX_BYTES` it is rotated to a single ``.1``
    backup and a fresh file is started, so the most recent drops are kept (the oldest
    rotate out); disk stays bounded at ~2x the cap.

    :param bridge_dir: Native forwarder bridge directory the dead-letter file lives in.
    :param session_id: Omnigent conversation id the dropped event targeted,
        e.g. ``"conv_abc123"``.
    :param event_type: Session event type that was dropped, e.g.
        ``"external_conversation_item"``.
    :param payload: The event ``data`` payload that failed to deliver.
    :param reason: Short human-readable cause, e.g.
        ``"permanent HTTP failure after retries"``.
    :returns: None.
    """
    try:
        path = bridge_dir / _DEAD_LETTER_FILE
        bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Keep-newest: at the cap, rotate the full file to a single .1 backup
        # (overwriting any prior backup) and start fresh. A sustained outage
        # then retains the most recent items rather than stopping at the oldest.
        if path.exists() and path.stat().st_size >= _DEAD_LETTER_MAX_BYTES:
            path.replace(bridge_dir / _DEAD_LETTER_BACKUP_FILE)
            # Log the session id rather than the bridge path: the path is
            # deterministic per session and logging it trips CodeQL's
            # clear-text-sensitive-data heuristic (a bridge dir is not a secret).
            _logger.warning(
                "dead-letter file reached cap (%d bytes); rotated to %s and "
                "started fresh (oldest dead-lettered forwards dropped): session=%s",
                _DEAD_LETTER_MAX_BYTES,
                _DEAD_LETTER_BACKUP_FILE,
                session_id,
            )
        line = json.dumps(
            {
                "ts": time.time(),
                "session_id": session_id,
                "event_type": event_type,
                "reason": reason,
                "payload": payload,
            }
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - dead-lettering must never disrupt forwarding.
        _logger.warning(
            "failed to dead-letter undeliverable forward: type=%s session=%s error=%r",
            event_type,
            session_id,
            exc,
        )


# Transport failures proving a POST never reached the server (no bytes
# sent) — safe to retry. See :func:`post_may_have_been_delivered`.
_DELIVERY_SAFE_RETRY_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


def post_may_have_been_delivered(exc: httpx.HTTPError) -> bool:
    """
    Return whether a failed AP POST may have been delivered AND
    committed by the server despite the error — making a blind retry
    unsafe for non-idempotent events.

    - ``HTTPStatusError``: the server responded with a status. The
      events route returns 2xx only after the item is appended and the
      consume event is published, so any non-2xx means the item was not
      committed (4xx rejects at parse time; a 5xx fails before/at the
      append). No duplicate risk → safe to retry, so ``False``.
    - Connection-establishment / pool-acquire failures
      (:data:`_DELIVERY_SAFE_RETRY_ERRORS`): no bytes were sent → not
      delivered → safe to retry, so ``False``.
    - Any other transport error (read/write timeout, read/write error,
      remote protocol error): the request was sent and we never saw a
      response, so the server may have processed it → ambiguous →
      ``True``.

    :param exc: HTTP exception raised while posting an AP event.
    :returns: ``True`` when a retry could duplicate a server-committed
        item; ``False`` when retrying is safe.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return False
    if isinstance(exc, _DELIVERY_SAFE_RETRY_ERRORS):
        return False
    return True


async def post_session_event_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, object],
    event_type: str,
    max_attempts: int,
    retry_status_codes: frozenset[int],
    sleep: Callable[[float], Coroutine[None, None, None]],
    retry_delay: Callable[[int], float],
    logger_name: str,
) -> httpx.Response | None:
    """
    POST a session event payload with bounded transient retries.

    Shared retry loop used by the antigravity (and optionally other)
    native forwarders. Conversation items persist with a random primary
    key and no server-side dedup, so an ambiguous transport failure
    (request sent, response lost) is NOT retried — a re-post would
    duplicate the item. Other event types are idempotent/transient and
    are retried.

    :param client: HTTP client for Omnigent event posts.
    :param url: Full request URL, e.g. ``"/v1/sessions/conv_x/events"``.
    :param payload: JSON payload to POST, e.g. ``{"type": ..., "data": ...}``.
    :param event_type: Session event type, e.g.
        ``"external_conversation_item"``. Used in log messages and to decide
        whether an ambiguous failure is safe to retry.
    :param max_attempts: Maximum POST attempts, e.g. ``3``.
    :param retry_status_codes: HTTP status codes to retry, e.g.
        ``frozenset({429, 500, 503})``.
    :param sleep: Async sleep coroutine (stubbable in tests).
    :param retry_delay: Callable ``attempt -> float`` returning the delay
        before the next attempt (one-based failed attempt number).
    :param logger_name: Logger name used for warning messages, e.g.
        ``"omnigent.antigravity_native_reader"``.
    :returns: Final HTTP response, or ``None`` when all attempts raised
        transport errors (or after an ambiguous conversation-item failure).
    """
    log = logging.getLogger(logger_name)
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # Conversation items persist with a random primary key and no
            # server-side dedup, so an ambiguous failure (request sent,
            # response lost — the server may have committed it) must not
            # be retried: a re-post would duplicate the item.
            # Other event types are idempotent / transient, so retrying
            # them on the same errors is safe and preserves delivery.
            if event_type == "external_conversation_item" and post_may_have_been_delivered(exc):
                log.warning(
                    "skipping session event after an ambiguous transport "
                    "failure (may already be committed); not retrying to avoid "
                    "a duplicate: type=%s error=%r",
                    event_type,
                    exc,
                )
                return None
            if attempt >= max_attempts:
                log.warning(
                    "failed to post session event after retries: type=%s attempts=%s error=%r",
                    event_type,
                    max_attempts,
                    exc,
                )
                return None
            await sleep(retry_delay(attempt))
            continue
        if response.status_code < 400:
            return response
        if response.status_code not in retry_status_codes:
            return response
        if attempt >= max_attempts:
            return response
        await sleep(retry_delay(attempt))
    return None
