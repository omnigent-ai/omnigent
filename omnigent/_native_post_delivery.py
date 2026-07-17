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

import contextlib
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent._native_forwarder_health import (
    note_post_success as note_native_post_success,
)
from omnigent._native_forwarder_health import (
    record_post_failure as record_native_post_failure,
)

_logger = logging.getLogger(__name__)

# Dead-letter sink for permanently-undeliverable forward payloads (#1120).
_DEAD_LETTER_FILE = "dead_letter.jsonl"
# At this size the file rotates to a single .1 backup (keep-newest); disk ~2x this.
_DEAD_LETTER_MAX_BYTES = 50 * 1024 * 1024  # 50 MB per session
_DEAD_LETTER_BACKUP_FILE = _DEAD_LETTER_FILE + ".1"


def append_dead_letter(
    bridge_dir: Path,
    *,
    session_id: str,
    event_type: str,
    payload: dict[str, object],
    reason: str,
    delivered_ambiguous: bool = False,
    http_status: int | None = None,
    transport_error: str | None = None,
) -> None:
    """
    Append one undeliverable forward payload to ``{bridge_dir}/dead_letter.jsonl`` (#1120).

    Write-only recovery artifact so a permanently-failed transcript/usage POST is
    recoverable on disk instead of silently lost. Conservative startup replay of the
    *proven-undelivered* records is layered on top (#1579) and reads the structured
    classification fields below to decide what is safe to re-POST.
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
    :param delivered_ambiguous: Whether the failure was ambiguous (request sent,
        response lost), so the server may have committed the item. Such records are
        NEVER replayed — a re-POST risks a duplicate (no server-side dedup).
    :param http_status: Final HTTP status code when the server responded, e.g.
        ``503`` or ``400``; ``None`` for a transport failure that saw no response.
    :param transport_error: Transport-error class name when the POST raised without a
        response, e.g. ``"ConnectError"``; ``None`` when the server responded.
    :returns: None.
    """
    try:
        path = bridge_dir / _DEAD_LETTER_FILE
        bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Keep-newest: at the cap, rotate to a single .1 backup and start fresh.
        if path.exists() and path.stat().st_size >= _DEAD_LETTER_MAX_BYTES:
            path.replace(bridge_dir / _DEAD_LETTER_BACKUP_FILE)
            # Log session_id, not path (logging a bridge path trips CodeQL's
            # clear-text-sensitive-data heuristic; a bridge dir is not a secret).
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
                "delivered_ambiguous": delivered_ambiguous,
                "http_status": http_status,
                "transport_error": transport_error,
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


async def post_external_session_status(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    status: str,
    output: str | None = None,
    background_task_count: int | None = None,
    response_id: str | None = None,
) -> None:
    """Post one ``external_session_status`` event to the Sessions API.

    The turn-end edge native forwarders use to drive session status and, for
    sub-agents, the parent-inbox wake. Shared by the claude-native and
    cursor-native forwarders so the event shape stays in one place.

    :param client: Omnigent HTTP client.
    :param session_id: Omnigent session/conversation id.
    :param status: Session status value, e.g. ``"idle"`` or ``"failed"``.
    :param output: Optional text attached to ``data``. On a ``"failed"`` edge
        the server surfaces it as ``last_task_error`` so the UI shows a detail
        instead of a bare "failed". Ignored when falsy.
    :param background_task_count: Background tasks (shells) still running at the
        edge, forwarded so the UI can show "N background tasks still running".
        ``None`` omits the field (server leaves its sticky tally untouched) — the
        default for edges that know nothing about background shells.
    :param response_id: Optional id of the assistant turn this status edge
        belongs to. When set, the server attaches it to the ``session.status``
        SSE event so ap-web can drive the bubble's streaming lifecycle — that's
        what makes native forwarded tool cards render LIVE (spinner + elapsed
        timer) rather than as static completed cards. ``None`` (the default)
        preserves the bare, turn-agnostic status edges (e.g. the sub-agent
        quiescence badge) that don't map to a turn.
    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    data: dict[str, object] = {"status": status}
    if output:
        data["output"] = output
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    if response_id is not None:
        data["response_id"] = response_id
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
    )
    resp.raise_for_status()


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
                # Surface this connectivity failure to the harness idle-turn
                # watchdog so a stall caused by unreachable-server posts is
                # reported with its real cause, not a generic "wedged LLM"
                # reason.
                record_native_post_failure(event_type, exc)
                return None
            await sleep(retry_delay(attempt))
            continue
        # Reaching here means the POST got an HTTP response (no transport
        # error), proving the server is reachable — clear any stale
        # connectivity-failure record so the watchdog can't later misattribute
        # it to an unrelated stall.
        note_native_post_success()
        if response.status_code < 400:
            return response
        if response.status_code not in retry_status_codes:
            return response
        if attempt >= max_attempts:
            return response
        await sleep(retry_delay(attempt))
    return None


@dataclass(frozen=True)
class RepostResult:
    """
    Outcome of one dead-letter replay re-POST attempt (#1579).

    Returned by the forwarder-supplied ``repost`` callable so
    :func:`replay_dead_letters` can decide whether to drop the record or keep
    it, and refresh its classification when a retained record's safety changed
    (e.g. a proven-undelivered transport record that now fails *ambiguously*
    must never be auto-replayed again).

    :param delivered: ``True`` when the server accepted the re-POST (a sub-400
        response). The record is removed on success.
    :param delivered_ambiguous: ``True`` when the re-POST failed ambiguously
        (request sent, response lost), so the item may now be committed. The
        record is kept but reclassified so replay never touches it again.
    :param http_status: Final HTTP status code when the server responded, or
        ``None`` for a transport failure that saw no response. Used to refresh
        the retained record's classification.
    """

    delivered: bool
    delivered_ambiguous: bool = False
    http_status: int | None = None


def _dead_letter_record_replayable(
    record: object,
    *,
    retryable_status_codes: frozenset[int],
) -> bool:
    """
    Return whether a dead-letter record is safe to re-POST on startup (#1579).

    Only *proven-undelivered* records are replayable: a transport failure that
    never reached the server, or a retryable status (e.g. ``503``) exhausted
    after the forwarder's bounded retries. Ambiguous failures (the server may
    have committed the item) and permanent rejections (a 4xx the server will
    just reject again) are never replayed.

    Records written before classification was added (#1579) lack the
    ``delivered_ambiguous`` field; they are treated as unsafe (forensic only)
    so a pre-classification ambiguous drop is never replayed into a duplicate.

    :param record: One parsed dead-letter record, or any non-dict entry
        (malformed line) which is never replayable.
    :param retryable_status_codes: HTTP statuses the forwarder treats as
        transient/retryable, e.g. ``frozenset({429, 500, 503})``. A recorded
        status in this set is proven-undelivered-but-recoverable.
    :returns: ``True`` only for proven-undelivered records with the routing
        fields (``session_id``, ``event_type``, ``payload``) needed to re-POST.
    """
    if not isinstance(record, dict):
        return False
    session_id = record.get("session_id")
    event_type = record.get("event_type")
    if not (isinstance(session_id, str) and session_id):
        return False
    if not (isinstance(event_type, str) and event_type):
        return False
    if not isinstance(record.get("payload"), dict):
        return False
    if "delivered_ambiguous" not in record:
        return False
    if record.get("delivered_ambiguous"):
        return False
    http_status = record.get("http_status")
    if http_status is None:
        # Transport failure with no response (ambiguous already excluded above)
        # — proven undelivered, so safe to re-POST.
        return True
    # The server responded: only a retryable status is recoverable; a permanent
    # 4xx would just be rejected again.
    return http_status in retryable_status_codes


def _read_dead_letter_entries(path: Path) -> list[object] | None:
    """
    Read one dead-letter file into ordered entries, preserving malformed lines.

    :param path: Dead-letter file path (current or ``.1`` backup).
    :returns: Ordered entries — parsed ``dict`` records, or the raw ``str`` line
        for any line that failed to parse (kept verbatim so a rewrite never
        drops forensic data) — or ``None`` when the file does not exist.
    """
    if not path.exists():
        return None
    entries: list[object] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(json.loads(stripped))
        except json.JSONDecodeError:
            entries.append(line)
    return entries


def _rewrite_dead_letter_entries(path: Path, entries: list[object]) -> None:
    """
    Atomically rewrite one dead-letter file with the retained entries (#1579).

    Writes a sibling ``.tmp`` then ``os.replace``-es it into place so a crash
    mid-rewrite never leaves a half-written file. An empty entry list removes
    the file.

    :param path: Dead-letter file path to rewrite.
    :param entries: Retained entries in original order (``dict`` records are
        re-serialized; raw ``str`` lines are written verbatim).
    :returns: None.
    """
    if not entries:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    lines = [entry if isinstance(entry, str) else json.dumps(entry) for entry in entries]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


async def replay_dead_letters(
    bridge_dir: Path,
    *,
    repost: Callable[[dict[str, object]], Coroutine[None, None, RepostResult]],
    retryable_status_codes: frozenset[int],
    logger_name: str | None = None,
    max_records: int | None = None,
    deadline_seconds: float | None = None,
) -> int:
    """
    Re-POST proven-undelivered dead-lettered forwards on forwarder startup (#1579).

    Reads the ``.1`` backup first, then the current file, preserving append
    order, and re-POSTs only *proven-undelivered* records (see
    :func:`_dead_letter_record_replayable`) via the supplied ``repost``
    callable. A delivered record is removed; a still-failing one is retained,
    with its classification refreshed from the latest attempt so a record that
    now fails ambiguously (or is permanently rejected) is never auto-replayed
    again. Ambiguous and permanent-4xx records are left untouched as a forensic
    record.

    Bounded so a large dead-letter file or a slow/hung server cannot stall
    startup: at most ``max_records`` records are re-POSTed and the whole drain
    is abandoned once ``deadline_seconds`` elapses. Records left over by either
    bound are retained unchanged (deferred to a later startup) and logged — never
    silently dropped. The remaining latency lever, a short per-POST timeout and a
    single attempt, is the caller's responsibility (via ``repost``).

    Intended to run once at startup, before live forwarding begins, so no other
    writer races the dead-letter files for this ``bridge_dir``.

    :param bridge_dir: Native forwarder bridge directory holding the dead-letter
        files.
    :param repost: Async callable that re-POSTs one record's
        ``(session_id, event_type, payload)`` and returns a :class:`RepostResult`.
    :param retryable_status_codes: HTTP statuses the forwarder treats as
        transient/retryable, used to classify which recorded statuses are
        recoverable.
    :param logger_name: Optional logger name for the recovery summary line;
        defaults to this module's logger.
    :param max_records: Maximum number of records to re-POST this run, e.g.
        ``500``; ``None`` for no cap. Bounds the number of network POSTs (and
        thus startup latency) even against a healthy server.
    :param deadline_seconds: Wall-clock budget for the whole drain, e.g.
        ``30.0``; ``None`` for no deadline. Once exceeded, the remaining
        replayable records are deferred to a later startup.
    :returns: The number of records successfully replayed (and removed).
    """
    log = logging.getLogger(logger_name) if logger_name else _logger
    sources = [
        bridge_dir / _DEAD_LETTER_BACKUP_FILE,
        bridge_dir / _DEAD_LETTER_FILE,
    ]
    loaded: list[tuple[Path, list[object]]] = []
    any_replayable = False
    for path in sources:
        entries = _read_dead_letter_entries(path)
        if entries is None:
            continue
        loaded.append((path, entries))
        if any(
            _dead_letter_record_replayable(entry, retryable_status_codes=retryable_status_codes)
            for entry in entries
        ):
            any_replayable = True
    if not any_replayable:
        # Nothing recoverable — leave the forensic files untouched.
        return 0

    replayed = 0
    attempted = 0
    deferred = 0
    stop = False
    deadline = time.monotonic() + deadline_seconds if deadline_seconds is not None else None
    # ``loaded`` preserves the .1-then-current source order, so records replay
    # in the order they were originally dropped.
    for path, entries in loaded:
        retained: list[object] = []
        changed = False
        for entry in entries:
            if not _dead_letter_record_replayable(
                entry, retryable_status_codes=retryable_status_codes
            ):
                # Forensic record (ambiguous / permanent-4xx / malformed) — keep as is.
                retained.append(entry)
                continue
            if stop:
                deferred += 1
                retained.append(entry)
                continue
            over_records = max_records is not None and attempted >= max_records
            over_deadline = deadline is not None and time.monotonic() >= deadline
            if over_records or over_deadline:
                # Out of budget — defer this and every later replayable record
                # to the next startup rather than stall here.
                stop = True
                deferred += 1
                retained.append(entry)
                continue
            assert isinstance(entry, dict)  # narrowed by _dead_letter_record_replayable
            attempted += 1
            result = await repost(entry)
            if result.delivered:
                replayed += 1
                changed = True
                continue
            if (
                entry.get("delivered_ambiguous") != result.delivered_ambiguous
                or entry.get("http_status") != result.http_status
            ):
                entry = {
                    **entry,
                    "delivered_ambiguous": result.delivered_ambiguous,
                    "http_status": result.http_status,
                }
                changed = True
            retained.append(entry)
        if changed:
            _rewrite_dead_letter_entries(path, retained)
    if replayed:
        log.info(
            "replayed %d proven-undelivered dead-lettered forward(s) on startup",
            replayed,
        )
    if deferred:
        log.info(
            "deferred %d replayable dead-lettered forward(s) to a later startup "
            "(replay budget reached: max_records=%s deadline_seconds=%s)",
            deferred,
            max_records,
            deadline_seconds,
        )
    return replayed
