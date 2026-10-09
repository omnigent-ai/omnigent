"""End-to-end regression: claude-native transcript forwarder under fd exhaustion.

On macOS the runner is a launchd child with a soft ``RLIMIT_NOFILE`` of 256.
Once a long session fills that table, every poll of
``forward_claude_transcript_to_session`` raises ``[Errno 24] Too many open
files`` opening the transcript/bridge files. fd exhaustion is a process-wide,
environmental condition: the loop must treat it as one paused outage (a
rate-limited WARNING, mirroring resumes when descriptors free up), not emit the
full-traceback ``Claude transcript forwarder loop failed`` ERROR on every poll.

Stand-in (environment fidelity)
-------------------------------
This CI host is Linux, so the condition is recreated for real: ``RLIMIT_NOFILE``
is lowered and the process's fd table is filled and pinned full while the
in-process forwarder loop polls a real server. This exercises the real forwarder
code path and logging; it does not reproduce launchd's inherited-limit mechanics.

Usage::

    pytest tests/e2e/test_claude_native_forwarder_fd_exhaustion_e2e.py -v

No ``--llm-api-key`` / login is needed: no LLM is invoked, and the Claude JSONL
transcript is seeded in the on-disk shape a live Claude CLI writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import resource
import time
import traceback
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.native.fd_exhaustion import fd_exhaustion_errno
from tests._helpers.live_server import isolated_local_server
from tests._helpers.native_session import create_native_session

#: launchd's inherited soft fd limit on the reported macOS environment.
_INHERITED_SOFT_LIMIT = 256
#: Fast poll so the exhaustion window covers many poll iterations.
_POLL_INTERVAL_S = 0.05
#: How long the fd table is kept pinned full; spans several backed-off polls.
_EXHAUSTION_WINDOW_S = 1.5
#: Deadline for a seeded transcript turn to appear as a mirrored conversation item.
_MIRROR_DEADLINE_S = 30.0

_BASELINE_USER = "fd-exhaustion-baseline-user-marker"
_BASELINE_ASSISTANT = "fd-exhaustion-baseline-assistant-marker"
_RECOVERY_USER = "post-exhaustion-recovery-user-marker"
_RECOVERY_ASSISTANT = "post-exhaustion-recovery-assistant-marker"


def _user(uuid: str, text: str) -> dict[str, Any]:
    """A Claude JSONL user record, shaped as the live CLI writes it."""
    return {
        "type": "user",
        "isSidechain": False,
        "uuid": uuid,
        "message": {"role": "user", "content": text},
        "promptSource": "typed",
        "userType": "external",
    }


def _assistant(uuid: str, text: str) -> dict[str, Any]:
    """A Claude JSONL assistant record."""
    return {
        "type": "assistant",
        "isSidechain": False,
        "uuid": uuid,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _append_turn(transcript_path: Path, records: list[dict[str, Any]]) -> None:
    """Append JSONL records to the transcript the way a live turn would."""
    with transcript_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class _RecordCapture(logging.Handler):
    """Collect every record the forwarder logger emits, verbatim."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _format_record(record: logging.LogRecord) -> str:
    """Render a captured record with its traceback for assertion messages."""
    text = f"{record.levelname} {record.name} {record.getMessage()}"
    if record.exc_info:
        text += "\n" + "".join(traceback.format_exception(*record.exc_info))
    return text


def _fd_exhaustion_in_chain(exc: BaseException | None) -> bool:
    """True if *exc* or its explicit cause chain is an EMFILE/ENFILE OSError."""
    return exc is not None and fd_exhaustion_errno(exc) is not None


def _fill_fd_table(ballast: list[int]) -> None:
    """Open ``/dev/null`` until the process's fd table is exhausted."""
    with contextlib.suppress(OSError):
        while True:
            ballast.append(os.open(os.devnull, os.O_RDONLY))


def _mirrored_texts(http: httpx.Client, base_url: str, session_id: str) -> list[str]:
    """Return every text block committed to the conversation store."""
    resp = http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    if resp.status_code != 200:
        return []
    return [
        block.get("text", "")
        for item in resp.json().get("data", [])
        for block in item.get("content", [])
        if isinstance(block, dict)
    ]


def _wait_for_mirrored(
    http: httpx.Client, base_url: str, session_id: str, needles: tuple[str, ...]
) -> None:
    """Block until every needle appears in a mirrored content block."""
    deadline = time.monotonic() + _MIRROR_DEADLINE_S
    texts: list[str] = []
    while time.monotonic() < deadline:
        texts = _mirrored_texts(http, base_url, session_id)
        if all(any(needle in text for text in texts) for needle in needles):
            return
        time.sleep(0.2)
    pytest.fail(
        f"Mirrored items never showed {needles} within {_MIRROR_DEADLINE_S}s; "
        f"content blocks seen: {texts!r}"
    )


def _seed_transcript(bridge_dir: Path) -> Path:
    """Seed a one-turn Claude transcript + Stop hook; return the transcript path."""
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"
    _append_turn(
        transcript_path,
        [
            _user("baseline-user-uuid", _BASELINE_USER),
            _assistant("baseline-assistant-uuid", _BASELINE_ASSISTANT),
        ],
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session-fd-exhaustion",
            "transcript_path": str(transcript_path),
        },
    )
    return transcript_path


async def _drive_forwarder_through_fd_exhaustion(
    http: httpx.Client,
    base_url: str,
    session_id: str,
    bridge_dir: Path,
    transcript_path: Path,
) -> None:
    """Run the real forwarder loop through a genuine fd-exhaustion window.

    Mirrors a baseline turn, exhausts the process's fd table for real across
    many polls, releases it, then appends a recovery turn that must still
    mirror. The forwarder runs in this process, so lowering ``RLIMIT_NOFILE``
    and filling the table starves exactly the file opens its poll loop makes.
    """
    import omnigent.harnesses.claude_native.forwarder as fwd

    task = asyncio.create_task(
        fwd.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=_POLL_INTERVAL_S,
        )
    )
    ballast: list[int] = []
    saved_limits: tuple[int, int] | None = None
    try:
        # 1. Baseline: the seeded turn mirrors into the conversation.
        await asyncio.to_thread(
            _wait_for_mirrored, http, base_url, session_id, (_BASELINE_USER, _BASELINE_ASSISTANT)
        )

        # 2. The fault: exhaust the fd table for real and keep it pinned full
        # across the window (a poll transiently closing a handle would free a
        # slot otherwise).
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        saved_limits = (soft, hard)
        # Cap just above what the process already holds so ballast can still open.
        cap = min(soft, max(_INHERITED_SOFT_LIMIT, len(os.listdir("/dev/fd")) + 32))
        resource.setrlimit(resource.RLIMIT_NOFILE, (cap, hard))
        _fill_fd_table(ballast)
        assert ballast, "fd ballast could not be established"
        with pytest.raises(OSError):
            os.open(os.devnull, os.O_RDONLY)  # prove the table is full
        window_deadline = time.monotonic() + _EXHAUSTION_WINDOW_S
        while time.monotonic() < window_deadline:
            _fill_fd_table(ballast)  # re-pin any transiently freed slot
            await asyncio.sleep(_POLL_INTERVAL_S)

        # 3. Release the pressure.
        for fd in ballast:
            with contextlib.suppress(OSError):
                os.close(fd)
        ballast.clear()
        resource.setrlimit(resource.RLIMIT_NOFILE, saved_limits)
        saved_limits = None

        # 4. Recovery: a turn appended after the outage must still mirror.
        from omnigent.harnesses.claude_native.bridge import record_hook_event

        _append_turn(
            transcript_path,
            [
                _user("recovery-user-uuid", _RECOVERY_USER),
                _assistant("recovery-assistant-uuid", _RECOVERY_ASSISTANT),
            ],
        )
        record_hook_event(
            bridge_dir,
            {
                "hook_event_name": "Stop",
                "session_id": "claude-session-fd-exhaustion",
                "transcript_path": str(transcript_path),
            },
        )
        await asyncio.to_thread(
            _wait_for_mirrored, http, base_url, session_id, (_RECOVERY_USER, _RECOVERY_ASSISTANT)
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for fd in ballast:
            with contextlib.suppress(OSError):
                os.close(fd)
        if saved_limits is not None:
            resource.setrlimit(resource.RLIMIT_NOFILE, saved_limits)


@pytest.mark.timeout(300)
def test_fd_exhaustion_polls_do_not_storm_the_forwarder_error_log(tmp_path: Path) -> None:
    """fd exhaustion during transcript polling must not storm the per-poll ERROR log.

    Runs the real claude-native forwarder loop against a real server, mirrors a
    baseline turn, genuinely exhausts the process's fd table across many polls,
    releases it, and requires (a) no ERROR-level ``Claude transcript forwarder
    loop failed`` record whose traceback carries an ``EMFILE`` OSError, and (b)
    mirroring resumed afterward.

    :param tmp_path: Per-test temp dir (server DB, artifacts, bridge dir).
    """
    from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    capture = _RecordCapture()
    fwd_logger = logging.getLogger("omnigent.harnesses.claude_native.forwarder")
    fwd_logger.addHandler(capture)
    try:
        # CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
        with (
            isolated_local_server(tmp_path) as base_url,
            httpx.Client(trust_env=False) as http,
        ):
            session_id = str(create_native_session(http, base_url, harness="claude")["session_id"])
            bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)
            transcript_path = _seed_transcript(bridge_dir)

            asyncio.run(
                _drive_forwarder_through_fd_exhaustion(
                    http, base_url, session_id, bridge_dir, transcript_path
                )
            )
    finally:
        fwd_logger.removeHandler(capture)

    assert any(
        record.levelno == logging.WARNING and "hit fd exhaustion" in record.getMessage()
        for record in capture.records
    ), "the forwarder never observed fd exhaustion; the fault did not reach its poll loop"
    emfile_errors = [
        record
        for record in capture.records
        if record.levelno >= logging.ERROR
        and record.getMessage().startswith("Claude transcript forwarder loop failed")
        and record.exc_info is not None
        and _fd_exhaustion_in_chain(record.exc_info[1])
    ]
    assert not emfile_errors, (
        f"fd exhaustion during transcript polling emitted the per-poll ERROR signature "
        f"{len(emfile_errors)} time(s) (one per ~{_POLL_INTERVAL_S}s poll). First captured "
        f"record:\n{_format_record(emfile_errors[0])}"
    )
