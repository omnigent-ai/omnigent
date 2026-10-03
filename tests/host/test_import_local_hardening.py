"""Host side of local-session import: heartbeats, cancel, failure codes, missing SQLite."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError

from omnigent.host import connect as host_connect
from omnigent.host.frames import (
    CAP_IMPORT_SKIP_KNOWN,
    HOST_CAPABILITIES,
    MAX_IMPORT_SKIP_IDS,
    HostImportLocalByIdFrame,
    HostImportLocalCancelFrame,
    HostImportLocalDoneFrame,
    HostImportLocalFrame,
    HostImportLocalProgressFrame,
    HostImportLocalSessionChunkFrame,
    HostImportLocalSessionFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.session_import import local as local_import
from omnigent.session_import.errors import (
    MISSING_SQLITE_FIX_COMMANDS,
    MISSING_SQLITE_MESSAGE,
    ImportErrorCode,
    mentions_missing_sqlite,
)
from tests.server.import_tunnel_harness import (
    RecordingWs,
    local_session,
    make_host,
    serve_local_sessions,
    wait_until,
)

_MISSING_SQLITE = "No module named '_sqlite3'"


def _done(ws: RecordingWs) -> HostImportLocalDoneFrame:
    (done,) = [f for f in ws.frames() if isinstance(f, HostImportLocalDoneFrame)]
    return done


def _host_events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [
        dict(getattr(record, "attributes", {}))
        for record in caplog.records
        if record.name == host_connect.__name__ and getattr(record, "event_name", None) == name
    ]


def test_request_frames_round_trip_the_progress_flag() -> None:
    """Both import request frames carry the server's heartbeat capability."""
    recent = HostImportLocalFrame(request_id="r", source="all", limit=5, progress=True)
    assert decode_host_frame(encode_host_frame(recent)) == recent
    by_id = HostImportLocalByIdFrame(request_id="r", source="codex", session_id="s", progress=True)
    assert decode_host_frame(encode_host_frame(by_id)) == by_id


def test_request_without_progress_flag_decodes_as_unsupported() -> None:
    """A request from a server that predates heartbeats decodes with progress off."""
    legacy = decode_host_frame(
        json.dumps({"kind": "host.import_local", "request_id": "r", "source": "all", "limit": 5})
    )
    assert legacy == HostImportLocalFrame(request_id="r", source="all", limit=5, progress=False)


def test_progress_and_cancel_frames_round_trip() -> None:
    """Heartbeat (with and without a total) and cancel frames survive encode/decode."""
    for frame in (
        HostImportLocalProgressFrame(request_id="r", done=2, total=None),
        HostImportLocalProgressFrame(request_id="r", done=2, total=7),
        HostImportLocalCancelFrame(request_id="r"),
    ):
        assert decode_host_frame(encode_host_frame(frame)) == frame


async def test_host_omits_heartbeats_for_a_server_that_did_not_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the progress flag the host sends only session and done frames."""
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")})
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
    )
    assert [json.loads(text)["kind"] for text in ws.sent] == [
        "host.import_local_session",
        "host.import_local_done",
    ]


async def test_heartbeats_count_sessions_done_of_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heartbeats start before enumeration (no total) and advance before each session."""
    sessions: dict[str, Any] = {"s0": local_session("s0"), "bad": OSError("disk")}
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5, progress=True)
    )
    beats = [(f.done, f.total) for f in ws.frames() if isinstance(f, HostImportLocalProgressFrame)]
    # A failed session advances the count too.
    assert beats == [(0, None), (0, 2), (1, 2)]


async def test_heartbeats_cover_a_slow_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transcript read slower than the heartbeat interval keeps sending heartbeats."""
    monkeypatch.setattr(host_connect, "_IMPORT_PROGRESS_INTERVAL_S", 0.05)
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")}, load_delay_s=0.4)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5, progress=True)
    )
    beats = [f for f in ws.frames() if isinstance(f, HostImportLocalProgressFrame)]
    # Start, before the session, and several during the 0.4 s read.
    assert len(beats) >= 4
    # Heartbeats stop with the import.
    sent = len(ws.sent)
    await asyncio.sleep(0.15)
    assert len(ws.sent) == sent


class _HeartbeatFailingWs(RecordingWs):
    """Records frames, but heartbeats sent from the background task raise ``exc``."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc
        self.handler_task: asyncio.Task[Any] | None = None
        self.heartbeat_attempts = 0

    async def send(self, text: str) -> None:
        is_progress = json.loads(text)["kind"] == "host.import_local_progress"
        if is_progress and asyncio.current_task() is not self.handler_task:
            self.heartbeat_attempts += 1
            raise self.exc
        await super().send(text)


@pytest.mark.parametrize(
    ("exc", "stops"),
    [(ConnectionClosedError(None, None), True), (RuntimeError("send failed"), False)],
    ids=["connection-closed", "other-error"],
)
async def test_heartbeat_stops_on_a_closed_tunnel_and_survives_other_errors(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, stops: bool
) -> None:
    """A heartbeat on a closed tunnel gives up after one try; other send errors keep it going."""
    monkeypatch.setattr(host_connect, "_IMPORT_PROGRESS_INTERVAL_S", 0.02)
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")}, load_delay_s=0.3)
    ws = _HeartbeatFailingWs(exc)
    request = HostImportLocalFrame(request_id="r", source="all", limit=5, progress=True)
    task = asyncio.create_task(make_host()._handle_import_local(ws.as_ws(), request))
    ws.handler_task = task
    await task
    # The import itself still finishes; only the background beats failed.
    assert _done(ws).status == "ok"
    if stops:
        assert ws.heartbeat_attempts == 1
    else:
        assert ws.heartbeat_attempts >= 3


async def test_cancel_frame_stops_the_named_import(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A cancel frame stops its in-flight import, which records itself as cancelled."""
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")}, load_delay_s=1.0)
    host = make_host()
    ws = RecordingWs()
    request = HostImportLocalFrame(request_id="req-2", source="all", limit=5)
    with caplog.at_level(logging.INFO, logger=host_connect.__name__):
        task = asyncio.create_task(host._handle_import_local(ws.as_ws(), request))
        await wait_until(lambda: "req-2" in host._import_tasks)
        await host._handle_raw_message(
            ws.as_ws(), encode_host_frame(HostImportLocalCancelFrame(request_id="req-2"))
        )
        with pytest.raises(asyncio.CancelledError):
            await task
    assert host._import_tasks == {}
    # Cancelled before the done frame: nothing terminal is sent.
    assert not [f for f in ws.frames() if isinstance(f, HostImportLocalDoneFrame)]
    (finished,) = _host_events(caplog, "import_local_finished")
    assert finished["status"] == "cancelled"


async def test_unknown_frame_and_unmatched_cancel_are_ignored() -> None:
    """An unknown kind and a cancel for no in-flight import are dropped silently."""
    host = make_host()
    ws = RecordingWs()
    await host._handle_raw_message(
        ws.as_ws(), json.dumps({"kind": "host.import_local_future", "request_id": "r"})
    )
    await host._handle_raw_message(
        ws.as_ws(), encode_host_frame(HostImportLocalCancelFrame(request_id="nope"))
    )
    assert ws.sent == []


async def test_unexpected_session_error_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected per-session error reports a generic reason without its text or a code."""
    sessions: dict[str, Any] = {"bad": RuntimeError("/secret/path"), "ok": local_session("ok")}
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
    )
    assert _done(ws).failures == [
        {
            "external_session_id": "bad",
            "source": "claude",
            "reason": "This session could not be read.",
        }
    ]


async def test_missing_sqlite_session_failure_carries_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that fails on a missing SQLite module is reported with the fix and its code."""
    sessions: dict[str, Any] = {
        "bad": ModuleNotFoundError(_MISSING_SQLITE),
        "ok": local_session("ok"),
    }
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="all", limit=5)
    )
    done = _done(ws)
    assert done.status == "ok"
    assert done.failures == [
        {
            "external_session_id": "bad",
            "source": "claude",
            "reason": MISSING_SQLITE_MESSAGE,
            "code": ImportErrorCode.HOST_PYTHON_MISSING_SQLITE,
        }
    ]


async def _list_failing_with(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> RecordingWs:
    """Import one harness whose session listing raises ``exc``."""

    def _broken(_source: str, *, limit: int) -> list[str]:
        raise exc

    monkeypatch.setattr(local_import, "list_recent_local_session_ids", _broken)
    ws = RecordingWs()
    await make_host()._handle_import_local(
        ws.as_ws(), HostImportLocalFrame(request_id="r", source="codex", limit=5)
    )
    return ws


async def test_listing_missing_sqlite_passes_its_text_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing-SQLite listing error keeps its text so the server can name the fix."""
    ws = await _list_failing_with(monkeypatch, ModuleNotFoundError(_MISSING_SQLITE))
    done = _done(ws)
    assert done.status == "failed"
    assert done.error == _MISSING_SQLITE


@pytest.mark.parametrize(
    "exc",
    [
        OSError("permission denied: /Users/alice/.codex/sessions"),
        ImportError("No module named 'yaml' (/Users/alice/venv)"),
        RuntimeError("index corrupt at /Users/alice/.codex/session_index.jsonl"),
    ],
    ids=["os-error", "other-import-error", "unexpected"],
)
async def test_listing_error_is_reported_without_its_text(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """Any other listing error fails the import with a generic message, never local paths."""
    ws = await _list_failing_with(monkeypatch, exc)
    done = _done(ws)
    assert done.status == "failed"
    assert done.error == "Local sessions could not be listed on the host."
    assert not any("/Users/alice" in text for text in ws.sent)


async def test_host_logs_start_and_finish_with_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The host records each import's start and its sent, chunked, and failed counts."""
    monkeypatch.setattr("omnigent.host.frames.IMPORT_SESSION_CHUNK_CHARS", 256)
    sessions: dict[str, Any] = {
        "ok": local_session("ok"),
        "unreadable": OSError("disk"),
        "huge": local_session("huge", items=20, text="z" * 50),
    }
    serve_local_sessions(monkeypatch, sessions)
    ws = RecordingWs()
    request = HostImportLocalFrame(
        request_id="req-1", source="all", limit=5, progress=True, allow_session_chunks=True
    )
    with caplog.at_level(logging.INFO, logger=host_connect.__name__):
        await make_host()._handle_import_local(ws.as_ws(), request)
    (started,) = _host_events(caplog, "import_local_started")
    assert (started["request_id"], started["source"], started["progress"]) == (
        "req-1",
        "all",
        True,
    )
    assert started["allow_session_chunks"] is True
    (finished,) = _host_events(caplog, "import_local_finished")
    assert finished["status"] == "ok"
    assert (finished["total"], finished["sent"], finished["chunked"], finished["failed"]) == (
        3,
        2,
        1,
        1,
    )
    assert isinstance(finished["duration_ms"], int)
    assert any(isinstance(f, HostImportLocalSessionChunkFrame) for f in ws.frames())
    assert any(isinstance(f, HostImportLocalSessionFrame) for f in ws.frames())


def test_missing_sqlite_message_is_actionable_and_short() -> None:
    """The missing-SQLite message names the module and comes with pasteable per-OS fixes."""
    assert len(MISSING_SQLITE_MESSAGE) < 450
    assert "_sqlite3" in MISSING_SQLITE_MESSAGE
    assert "omnigent host" in MISSING_SQLITE_MESSAGE
    labels = [fix["label"] for fix in MISSING_SQLITE_FIX_COMMANDS]
    assert labels[0].startswith("macOS")
    assert labels[1] == "Linux"
    # Each command is pasteable as-is: no label or prose inside it.
    for fix in MISSING_SQLITE_FIX_COMMANDS:
        assert ":" not in fix["command"]
        assert "(" not in fix["command"]
    assert MISSING_SQLITE_FIX_COMMANDS[0]["command"] == (
        "brew install sqlite && pyenv install --force 3.12"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ModuleNotFoundError: No module named '_sqlite3'", True),
        ("No module named 'sqlite3'", True),
        ("No module named 'yaml'", False),
        (None, False),
    ],
)
def test_mentions_missing_sqlite_detects_both_spellings(text: object, expected: bool) -> None:
    """Both spellings of the missing-SQLite import error are recognized."""
    assert mentions_missing_sqlite(text) is expected


def test_host_import_modules_load_without_sqlite(tmp_path: Path) -> None:
    """The host daemon and transcript readers import on a Python built without SQLite."""
    Path(tmp_path, "state_5.sqlite").write_text("not a db")
    # A fresh interpreter, because this one already has sqlite3 loaded.
    script = textwrap.dedent(
        f"""
        import sys
        sys.modules["_sqlite3"] = None
        sys.modules["sqlite3"] = None
        from pathlib import Path
        import omnigent.host.connect
        from omnigent.session_import import local
        assert local._codex_native_title(Path({str(tmp_path)!r}), "thread-1") is None
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert result.stdout.strip().endswith("ok")


def test_skip_fields_round_trip_and_stay_off_the_wire_when_empty() -> None:
    """Skip lists and skipped counts survive encode/decode and are omitted when empty."""
    request = HostImportLocalFrame(request_id="r", source="all", skip_external_session_ids=["a"])
    assert decode_host_frame(encode_host_frame(request)) == request
    plain = json.loads(encode_host_frame(HostImportLocalFrame(request_id="r", source="all")))
    assert "skip_external_session_ids" not in plain
    done = HostImportLocalDoneFrame(request_id="r", status="ok", skipped=3)
    assert decode_host_frame(encode_host_frame(done)) == done
    assert "skipped" not in json.loads(
        encode_host_frame(HostImportLocalDoneFrame(request_id="r", status="ok"))
    )
    progress = HostImportLocalProgressFrame(request_id="r", done=4, total=9, skipped=2)
    assert decode_host_frame(encode_host_frame(progress)) == progress


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("s0", []),
        (["a", 1, ""], ["a"]),
        (
            [f"s{i}" for i in range(MAX_IMPORT_SKIP_IDS + 5)],
            [f"s{i}" for i in range(MAX_IMPORT_SKIP_IDS)],
        ),
    ],
    ids=["not-a-list", "mixed", "oversized"],
)
def test_malformed_or_oversized_skip_list_is_tolerated(raw: object, expected: list[str]) -> None:
    """A bad skip list reads as its valid ids (capped), never a decode failure."""
    msg = {"kind": "host.import_local", "request_id": "r", "source": "all", "limit": 5}
    frame = decode_host_frame(json.dumps({**msg, "skip_external_session_ids": raw}))
    assert isinstance(frame, HostImportLocalFrame)
    assert frame.skip_external_session_ids == expected


def test_encoder_caps_the_skip_list() -> None:
    """A server never puts more than the cap on the wire."""
    ids = [f"s{i}" for i in range(MAX_IMPORT_SKIP_IDS + 5)]
    frame = HostImportLocalFrame(request_id="r", source="all", skip_external_session_ids=ids)
    sent = json.loads(encode_host_frame(frame))["skip_external_session_ids"]
    assert sent == ids[:MAX_IMPORT_SKIP_IDS]


def test_host_advertises_skip_known() -> None:
    """This host build tells the server it honors skip lists."""
    assert CAP_IMPORT_SKIP_KNOWN in HOST_CAPABILITIES


async def test_host_skips_listed_sessions_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listed sessions are counted as skipped without being read or sent."""
    loaded: list[str] = []
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(3)}
    serve_local_sessions(monkeypatch, sessions)
    served = local_import.load_local_session

    def _tracking_load(source: Any, session_id: str) -> Any:
        loaded.append(session_id)
        return served(source, session_id)

    monkeypatch.setattr(local_import, "load_local_session", _tracking_load)
    ws = RecordingWs()
    request = HostImportLocalFrame(
        request_id="r",
        source="all",
        limit=5,
        progress=True,
        skip_external_session_ids=["s0", "s2", "not-on-this-host"],
    )
    await make_host()._handle_import_local(ws.as_ws(), request)
    assert loaded == ["s1"]
    sent = [
        f.session.external_session_id
        for f in ws.frames()
        if isinstance(f, HostImportLocalSessionFrame)
    ]
    assert sent == ["s1"]
    done = _done(ws)
    assert (done.status, done.skipped, done.failed) == ("ok", 2, 0)
    beats = [
        (f.done, f.skipped) for f in ws.frames() if isinstance(f, HostImportLocalProgressFrame)
    ]
    # Before each session: s2 (skipped), s1, s0 (skipped).
    assert beats == [(0, 0), (0, 0), (1, 1), (2, 1)]


@pytest.mark.parametrize("total", [True, -1, "3"], ids=["bool", "negative", "string"])
def test_progress_total_that_is_not_a_count_decodes_as_unknown(total: object) -> None:
    """A heartbeat whose total isn't a non-negative int reads as an unknown total."""
    raw = json.dumps(
        {"kind": "host.import_local_progress", "request_id": "r", "done": 1, "total": total}
    )
    frame = decode_host_frame(raw)
    assert isinstance(frame, HostImportLocalProgressFrame)
    assert (frame.done, frame.total) == (1, None)
