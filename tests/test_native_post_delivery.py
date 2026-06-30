"""Tests for the shared native-forwarder POST delivery classifier."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent._native_post_delivery import (
    _DEAD_LETTER_BACKUP_FILE,
    _DEAD_LETTER_FILE,
    _DEAD_LETTER_MAX_BYTES,
    RepostResult,
    _dead_letter_record_replayable,
    append_dead_letter,
    post_may_have_been_delivered,
    replay_dead_letters,
)

# Status codes a forwarder treats as transient/retryable, used by the replay
# classifier to tell a recoverable exhausted-503 from a permanent 4xx.
_RETRYABLE = frozenset({429, 500, 503})


def _dead_letter_record(
    *,
    session_id: str = "conv_abc123",
    event_type: str = "external_conversation_item",
    payload: dict[str, object] | None = None,
    reason: str = "proven-undelivered transport failure after retries",
    delivered_ambiguous: bool = False,
    http_status: int | None = None,
    transport_error: str | None = None,
) -> dict[str, object]:
    """Build a classified dead-letter record for replay fixtures."""
    return {
        "ts": 1.0,
        "session_id": session_id,
        "event_type": event_type,
        "reason": reason,
        "delivered_ambiguous": delivered_ambiguous,
        "http_status": http_status,
        "transport_error": transport_error,
        "payload": payload if payload is not None else {"item_type": "message"},
    }


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    """Write records as one JSON line each, mirroring the dead-letter format."""
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _read_records(path: Path) -> list[dict[str, object]]:
    """Parse a dead-letter file back into records."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    "exc,may_have_been_delivered",
    [
        # Server responded with a status — the events route returns 2xx
        # only after the append + consume publish, so any non-2xx means
        # the item was NOT committed. Safe to retry.
        (
            httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "http://test"),
                response=httpx.Response(503),
            ),
            False,
        ),
        # Connection never established / pool never acquired — no request
        # bytes were sent, so the item was not delivered. Safe to retry.
        (httpx.ConnectError("refused", request=httpx.Request("POST", "http://test")), False),
        (
            httpx.ConnectTimeout("slow connect", request=httpx.Request("POST", "http://test")),
            False,
        ),
        (httpx.PoolTimeout("no slot", request=httpx.Request("POST", "http://test")), False),
        # Request was sent and no response was seen — the server may have
        # committed it. Ambiguous: a retry could duplicate.
        (httpx.ReadTimeout("no response", request=httpx.Request("POST", "http://test")), True),
        (httpx.WriteError("write failed", request=httpx.Request("POST", "http://test")), True),
        (
            httpx.RemoteProtocolError("peer closed", request=httpx.Request("POST", "http://test")),
            True,
        ),
    ],
)
def test_post_may_have_been_delivered_classification(
    exc: httpx.HTTPError, may_have_been_delivered: bool
) -> None:
    """
    Classify which POST failures may have reached + committed the server.

    A forwarder must not retry a POST that may already be committed,
    because external conversation items are not deduped server-side, so
    a retry would surface as a duplicate bubble in the web UI.
    A wrong classification means either duplicates (ambiguous error
    marked safe-to-retry) or lost messages (provably-undelivered error
    marked ambiguous and dropped).

    :param exc: HTTP exception raised while posting an AP event.
    :param may_have_been_delivered: Whether the request may have been
        committed despite the error.
    """
    assert post_may_have_been_delivered(exc) is may_have_been_delivered


def test_append_dead_letter_writes_parseable_line(tmp_path: Path) -> None:
    """
    A dropped forward payload is appended as one parseable JSON line.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_conversation_item",
        payload={"item_type": "message", "item_data": {"role": "assistant"}},
        reason="permanent HTTP failure after retries",
    )

    dl_path = tmp_path / _DEAD_LETTER_FILE
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "conv_abc123"
    assert record["event_type"] == "external_conversation_item"
    assert record["reason"] == "permanent HTTP failure after retries"
    assert record["payload"] == {
        "item_type": "message",
        "item_data": {"role": "assistant"},
    }
    assert isinstance(record["ts"], (int, float))


def test_append_dead_letter_rotates_at_cap_keeping_newest(tmp_path: Path) -> None:
    """
    At the cap the file rotates to a ``.1`` backup and keeps the newest item.

    A sustained outage must retain the most recent drops, not stop at the
    oldest: the full file moves to ``dead_letter.jsonl.1`` and the new record
    lands in a fresh ``dead_letter.jsonl``.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    dl_path = tmp_path / _DEAD_LETTER_FILE
    backup_path = tmp_path / _DEAD_LETTER_BACKUP_FILE
    # Use a sparse file (truncate) to reach the cap without writing 50 MB.
    with dl_path.open("wb") as fh:
        fh.truncate(_DEAD_LETTER_MAX_BYTES + 1)
    capped_size = dl_path.stat().st_size

    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_session_usage",
        payload={"context_tokens": 1},
        reason="post failed",
    )

    # Old content rotated out to the backup; the newest record is in the
    # fresh active file as the sole line.
    assert backup_path.stat().st_size == capped_size
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "external_session_usage"
    assert record["payload"] == {"context_tokens": 1}


def test_append_dead_letter_never_raises_on_unwritable_dir() -> None:
    """A bogus / unwritable bridge dir is swallowed, not raised."""
    bogus = Path("/this/path/does/not/exist/and/cannot/be/made\x00")
    # Must return without raising despite the invalid path.
    append_dead_letter(
        bogus,
        session_id="conv_abc123",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="post failed",
    )


def test_append_dead_letter_writes_classification_fields(tmp_path: Path) -> None:
    """
    The structured classification replay reads is persisted on each record (#1579).

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="ambiguous transport failure (may already be committed)",
        delivered_ambiguous=True,
        http_status=None,
        transport_error="ReadTimeout",
    )

    record = _read_records(tmp_path / _DEAD_LETTER_FILE)[0]
    assert record["delivered_ambiguous"] is True
    assert record["http_status"] is None
    assert record["transport_error"] == "ReadTimeout"


def test_append_dead_letter_classification_defaults(tmp_path: Path) -> None:
    """A record written without classification args defaults to non-ambiguous."""
    append_dead_letter(
        tmp_path,
        session_id="conv_abc123",
        event_type="external_session_usage",
        payload={"context_tokens": 1},
        reason="post failed",
    )

    record = _read_records(tmp_path / _DEAD_LETTER_FILE)[0]
    assert record["delivered_ambiguous"] is False
    assert record["http_status"] is None
    assert record["transport_error"] is None


@pytest.mark.parametrize(
    "record,replayable",
    [
        # Proven-undelivered transport failure (no response, not ambiguous).
        (_dead_letter_record(), True),
        # Retryable status exhausted after the forwarder's bounded retries.
        (_dead_letter_record(http_status=503), True),
        # Ambiguous: the server may have committed it — never replay.
        (_dead_letter_record(delivered_ambiguous=True), False),
        # Permanent 4xx: the server rejected it; a replay just re-rejects.
        (_dead_letter_record(http_status=400), False),
        # Non-retryable 5xx that is not in the retry set is also not recoverable.
        (_dead_letter_record(http_status=501), False),
        # Missing routing fields — cannot be re-POSTed.
        (_dead_letter_record(session_id=""), False),
        # A non-dict (malformed line) is never replayable.
        ("not-a-record", False),
        # Legacy record written before classification existed (#1579): no
        # ``delivered_ambiguous`` field, so treated as unsafe (forensic only).
        (
            {
                "ts": 1.0,
                "session_id": "conv_abc123",
                "event_type": "external_conversation_item",
                "reason": "post failed",
                "payload": {"item_type": "message"},
            },
            False,
        ),
    ],
)
def test_dead_letter_record_replayable_classification(record: object, replayable: bool) -> None:
    """
    Only proven-undelivered records are replayable; everything else is forensic.

    A wrong classification either duplicates a committed item (ambiguous or
    permanent record wrongly replayed) or re-rejects forever.

    :param record: A parsed dead-letter record (or non-dict) to classify.
    :param replayable: Whether the record is safe to re-POST.
    """
    assert _dead_letter_record_replayable(record, retryable_status_codes=_RETRYABLE) is replayable


async def test_replay_drains_backup_then_current_in_order(tmp_path: Path) -> None:
    """
    Replay re-POSTs the ``.1`` backup first, then the current file, in order (#1579).

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    backup = tmp_path / _DEAD_LETTER_BACKUP_FILE
    current = tmp_path / _DEAD_LETTER_FILE
    _write_records(
        backup, [_dead_letter_record(payload={"n": 0}), _dead_letter_record(payload={"n": 1})]
    )
    _write_records(
        current, [_dead_letter_record(payload={"n": 2}), _dead_letter_record(payload={"n": 3})]
    )

    seen: list[int] = []

    async def repost(record: dict[str, object]) -> RepostResult:
        seen.append(record["payload"]["n"])  # type: ignore[index]
        return RepostResult(delivered=True)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost, retryable_status_codes=_RETRYABLE
    )

    assert replayed == 4
    # .1 backup (0, 1) precedes the current file (2, 3), each in append order.
    assert seen == [0, 1, 2, 3]
    # All delivered, so both files are removed.
    assert not backup.exists()
    assert not current.exists()


async def test_replay_skips_ambiguous_and_permanent_4xx(tmp_path: Path) -> None:
    """
    Ambiguous and permanent-4xx records are never re-POSTed; they stay forensic.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    current = tmp_path / _DEAD_LETTER_FILE
    _write_records(
        current,
        [
            _dead_letter_record(payload={"n": 0}, delivered_ambiguous=True),
            _dead_letter_record(payload={"n": 1}, http_status=400),
            _dead_letter_record(payload={"n": 2}),
            _dead_letter_record(payload={"n": 3}, http_status=503),
        ],
    )

    seen: list[int] = []

    async def repost(record: dict[str, object]) -> RepostResult:
        seen.append(record["payload"]["n"])  # type: ignore[index]
        return RepostResult(delivered=True)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost, retryable_status_codes=_RETRYABLE
    )

    assert replayed == 2
    # Only the proven-undelivered transport (2) and exhausted-503 (3) replayed.
    assert seen == [2, 3]
    # Ambiguous (0) and permanent-4xx (1) retained, untouched, in order.
    retained = _read_records(current)
    assert [record["payload"]["n"] for record in retained] == [0, 1]


async def test_replay_success_removes_and_failure_retains(tmp_path: Path) -> None:
    """
    A delivered record is removed; one that still fails is retained (#1579).

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    current = tmp_path / _DEAD_LETTER_FILE
    _write_records(
        current,
        [_dead_letter_record(payload={"n": 0}), _dead_letter_record(payload={"n": 1})],
    )

    async def repost(record: dict[str, object]) -> RepostResult:
        if record["payload"]["n"] == 0:  # type: ignore[index]
            return RepostResult(delivered=True)
        # Still proven-undelivered (transport failed again).
        return RepostResult(delivered=False, http_status=None)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost, retryable_status_codes=_RETRYABLE
    )

    assert replayed == 1
    retained = _read_records(current)
    assert [record["payload"]["n"] for record in retained] == [1]
    # The retained record stays proven-undelivered (replayable on a later run).
    assert retained[0]["delivered_ambiguous"] is False
    assert retained[0]["http_status"] is None


async def test_replay_reclassifies_record_that_now_fails_ambiguously(tmp_path: Path) -> None:
    """
    A replay attempt that fails *ambiguously* reclassifies the record so it is
    never auto-replayed again — guarding against a duplicate (#1579).

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    current = tmp_path / _DEAD_LETTER_FILE
    _write_records(current, [_dead_letter_record(payload={"n": 0})])

    async def repost_ambiguous(record: dict[str, object]) -> RepostResult:
        return RepostResult(delivered=False, delivered_ambiguous=True)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost_ambiguous, retryable_status_codes=_RETRYABLE
    )
    assert replayed == 0
    retained = _read_records(current)
    assert len(retained) == 1
    assert retained[0]["delivered_ambiguous"] is True

    # A second startup must NOT re-POST the now-ambiguous record.
    calls: list[object] = []

    async def repost_record(record: dict[str, object]) -> RepostResult:
        calls.append(record)
        return RepostResult(delivered=True)

    replayed_again = await replay_dead_letters(
        tmp_path, repost=repost_record, retryable_status_codes=_RETRYABLE
    )
    assert replayed_again == 0
    assert calls == []


async def test_replay_no_replayable_records_leaves_files_untouched(tmp_path: Path) -> None:
    """
    With nothing recoverable, replay never calls ``repost`` or rewrites the file.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    current = tmp_path / _DEAD_LETTER_FILE
    _write_records(
        current,
        [
            _dead_letter_record(payload={"n": 0}, delivered_ambiguous=True),
            _dead_letter_record(payload={"n": 1}, http_status=404),
        ],
    )
    before = current.read_bytes()

    called = False

    async def repost(record: dict[str, object]) -> RepostResult:
        nonlocal called
        called = True
        return RepostResult(delivered=True)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost, retryable_status_codes=_RETRYABLE
    )

    assert replayed == 0
    assert called is False
    # File is byte-identical: forensic records are left exactly as written.
    assert current.read_bytes() == before


async def test_replay_with_no_dead_letter_files_is_a_noop(tmp_path: Path) -> None:
    """Replay on a bridge dir with no dead-letter file does nothing."""
    called = False

    async def repost(record: dict[str, object]) -> RepostResult:
        nonlocal called
        called = True
        return RepostResult(delivered=True)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost, retryable_status_codes=_RETRYABLE
    )
    assert replayed == 0
    assert called is False


async def test_replay_respects_max_records(tmp_path: Path) -> None:
    """
    At most ``max_records`` records are re-POSTed; the rest are deferred (#1579).

    Bounds startup latency even against a healthy server with a huge file. The
    deferred records are retained unchanged for a later startup, not dropped.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    current = tmp_path / _DEAD_LETTER_FILE
    _write_records(current, [_dead_letter_record(payload={"n": i}) for i in range(3)])

    seen: list[int] = []

    async def repost(record: dict[str, object]) -> RepostResult:
        seen.append(record["payload"]["n"])  # type: ignore[index]
        return RepostResult(delivered=True)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost, retryable_status_codes=_RETRYABLE, max_records=2
    )

    assert replayed == 2
    # Only the first two were attempted, in order.
    assert seen == [0, 1]
    # The third was deferred and kept for next startup.
    retained = _read_records(current)
    assert [record["payload"]["n"] for record in retained] == [2]


async def test_replay_respects_deadline(tmp_path: Path) -> None:
    """
    Once the deadline is exceeded, replay defers everything (no re-POST) (#1579).

    A zero budget defers before the first attempt, leaving the file untouched.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    current = tmp_path / _DEAD_LETTER_FILE
    _write_records(
        current,
        [_dead_letter_record(payload={"n": 0}), _dead_letter_record(payload={"n": 1})],
    )
    before = current.read_bytes()

    called = False

    async def repost(record: dict[str, object]) -> RepostResult:
        nonlocal called
        called = True
        return RepostResult(delivered=True)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost, retryable_status_codes=_RETRYABLE, deadline_seconds=0.0
    )

    assert replayed == 0
    assert called is False
    # Nothing changed, so the file is left byte-identical for the next startup.
    assert current.read_bytes() == before


async def test_replay_preserves_malformed_lines(tmp_path: Path) -> None:
    """
    A malformed (unparseable) line is retained verbatim across a replay rewrite.

    :param tmp_path: Pytest temp dir standing in for a bridge dir.
    """
    current = tmp_path / _DEAD_LETTER_FILE
    current.write_text(
        "not-json garbage line\n" + json.dumps(_dead_letter_record(payload={"n": 0})) + "\n",
        encoding="utf-8",
    )

    async def repost(record: dict[str, object]) -> RepostResult:
        return RepostResult(delivered=True)

    replayed = await replay_dead_letters(
        tmp_path, repost=repost, retryable_status_codes=_RETRYABLE
    )

    assert replayed == 1
    # The parseable record was delivered and removed; the garbage line survives.
    remaining = current.read_text(encoding="utf-8").splitlines()
    assert remaining == ["not-json garbage line"]
