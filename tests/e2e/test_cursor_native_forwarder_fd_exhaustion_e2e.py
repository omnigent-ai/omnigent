"""End-to-end regression: cursor-native transcript polling under fd exhaustion.

The bug
-------
``forward_cursor_store_to_session`` (omnigent/harnesses/cursor_native/forwarder.py)
polls the cursor chat store every ~0.7s, and every poll re-checks chat
ownership via ``_chat_claimed_by_other``, whose ``root.iterdir()`` opens a
directory handle. When the process's file-descriptor table is exhausted
(``EMFILE`` — seen in the field on macOS dev hosts, whose default soft limit
is 256, with several concurrent cursor-native sessions polling at once), that
``iterdir`` raises ``OSError: [Errno 24] Too many open files``. The poll
loop's blanket handler then emits the unstructured ERROR record

    cursor forwarder poll failed; session=<id> store=<...>/store.db

once per poll for an environmental condition — flooding telemetry, tripping
the error-rate KPI, and skipping transcript mirroring for every failing poll.

Journey reproduced
------------------
1. A cursor-native session's transcript mirrors into the web conversation
   (baseline user+assistant bubbles arrive via the real Sessions API).
2. The session's process runs out of file descriptors — the fault is injected
   for real: ``RLIMIT_NOFILE`` is lowered and the remaining table is filled
   and kept pinned full.
3. Polls during the exhaustion window hit ``EMFILE``; on the unfixed build
   each emits the ERROR signature above (the bug).
4. The fd pressure clears; a message appended afterward must still be
   mirrored (the loop survived and recovered).

The test FAILS on the unfixed build at step 3 (ERROR-signature records with
``errno == EMFILE`` are captured) and PASSES once the forwarder treats fd
exhaustion as a transient, structured condition instead of an unhandled
per-poll ERROR — while step 4 keeps any fix honest: the mirror must actually
survive and resume, not be silenced or killed.

Usage::

    pytest tests/e2e/test_cursor_native_forwarder_fd_exhaustion_e2e.py -v

No ``--llm-api-key``, ``--profile``, or cursor login needed — no LLM is
invoked, and the chat store is seeded in cursor's real on-disk format (a
WAL-live SQLite ``blobs`` table, the layout ``tests/test_cursor_native_forwarder.py``
verifies against the real TUI).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import io
import json
import logging
import os
import resource
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Fast poll so the exhaustion window covers many poll iterations.
_POLL_INTERVAL_S = 0.05
#: How long the fd table is kept pinned full (~ 30 poll iterations).
_EXHAUSTION_WINDOW_S = 1.5
#: Deadline for a seeded store row to appear as a mirrored conversation item.
_MIRROR_DEADLINE_S = 30.0

_BASELINE_PROMPT = "fd-exhaustion baseline prompt sentinel"
_BASELINE_REPLY = "fd-exhaustion baseline reply sentinel"
_RECOVERY_REPLY = "post-exhaustion recovery sentinel"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Return an ephemeral TCP port the OS considers free right now."""
    s = socket.socket()
    s.bind(("", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def _build_minimal_agent_bundle() -> bytes:
    """Return a minimal agent bundle (tar.gz bytes) accepted by POST /v1/sessions."""
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "cursor-fwd-emfile-test",
            "executor": {
                "type": "omnigent",
                "config": {"harness": "openai-agents"},
            },
            "llm": {
                "model": "cursor-fwd-emfile-test",
                "connection": {"api_key": "test-key"},
            },
        }
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config)
        tf.addfile(info, io.BytesIO(config))
    return buf.getvalue()


def _make_wal_store(path: Path, rows: list[tuple[str, object]]) -> sqlite3.Connection:
    """Create a cursor-format chat store and return the kept-open writer.

    Matches the layout a live cursor-agent chat has: a ``blobs`` table in WAL
    mode with autocheckpoint disabled, so committed rows live in the ``-wal``
    sidecar while the TUI keeps the writer open (the exact store the forwarder
    tails in production; see ``tests/test_cursor_native_forwarder.py``).
    """
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE blobs(id TEXT PRIMARY KEY, data BLOB)")
    for blob_id, data in rows:
        payload = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        con.execute("INSERT INTO blobs(id, data) VALUES(?, ?)", (blob_id, payload))
    con.commit()
    return con


def _user_blob(text: str) -> dict:
    """A stored cursor user turn (the TUI wraps the prompt in <user_query>)."""
    return {
        "role": "user",
        "content": [{"type": "text", "text": f"<user_query>\n{text}\n</user_query>"}],
    }


def _assistant_blob(text: str) -> dict:
    """A stored cursor assistant turn."""
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


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


def _fill_fd_table(ballast: list[int]) -> None:
    """Open ``/dev/null`` until the process's fd table is exhausted."""
    with contextlib.suppress(OSError):
        while True:
            ballast.append(os.open(os.devnull, os.O_RDONLY))


async def _wait_for_mirrored_texts(
    client: httpx.AsyncClient, session_id: str, needles: tuple[str, ...], deadline_s: float
) -> None:
    """Poll GET /items until every needle appears in a mirrored content block."""
    deadline = time.monotonic() + deadline_s
    texts: list[str] = []
    while time.monotonic() < deadline:
        resp = await client.get(f"/v1/sessions/{session_id}/items")
        if resp.status_code == 200:
            texts = [
                block.get("text", "")
                for item in resp.json().get("data", [])
                for block in item.get("content", [])
                if isinstance(block, dict)
            ]
            if all(any(needle in text for text in texts) for needle in needles):
                return
        await asyncio.sleep(0.2)
    pytest.fail(
        f"Mirrored items never showed {needles} within {deadline_s}s; "
        f"content blocks seen: {texts!r}"
    )


# ---------------------------------------------------------------------------
# Fixture: minimal Omnigent server subprocess (same pattern as
# tests/e2e/test_claude_native_forwarder_retry_dedup_e2e.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def emfile_server() -> Iterator[str]:
    """Start a minimal Omnigent server subprocess; yield its base URL."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    tmp_root = Path(tempfile.mkdtemp(prefix="cursor-fwd-emfile-e2e-"))
    db_path = tmp_root / "ap.db"
    artifact_dir = tmp_root / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_root / "server.log"

    env = os.environ.copy()
    env["OPENAI_API_KEY"] = "stub-not-used"
    # Pin single-user header auth: the test drives headerless requests, and an
    # ambient developer/CI shell exporting OMNIGENT_AUTH_ENABLED (or OIDC vars)
    # would boot the server in login mode and 401 every call.
    env["OMNIGENT_AUTH_PROVIDER"] = "header"
    env["OMNIGENT_LOCAL_SINGLE_USER"] = "1"
    for var in list(env):
        if (
            var.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or var.endswith("_SECRET")
            or var
            in (
                "ANTHROPIC_API_KEY",
                "OMNIGENT_AUTH_ENABLED",
                "OMNIGENT_RUNNER_TUNNEL_TOKEN",
            )
        ):
            env.pop(var, None)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(_REPO_ROOT)
    )

    log_handle = open(log_path, "w")  # noqa: SIM115 — subprocess holds the FD
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.ReadError):
                pass
            time.sleep(0.2)
        else:
            proc.terminate()
            log_handle.close()
            log_text = log_path.read_text(errors="replace")
            raise RuntimeError(
                f"Omnigent server failed to start within 30 s. Log tail:\n{log_text[-2000:]}"
            )
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()


@pytest.fixture(scope="module")
def emfile_session_id(emfile_server: str) -> str:
    """Create a real session on the test server and return its id."""
    bundle = _build_minimal_agent_bundle()
    with httpx.Client(base_url=emfile_server, timeout=30.0) as client:
        resp = client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({})},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
    assert resp.status_code in (200, 201), (
        f"Session create failed {resp.status_code}: {resp.text[:400]}"
    )
    data = resp.json()
    session_id: str = data.get("id") or data.get("session_id") or ""
    assert session_id, f"No session id in response: {data}"
    return session_id


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


async def test_fd_exhaustion_polls_do_not_emit_error_signature_and_mirror_recovers(
    emfile_server: str, emfile_session_id: str, tmp_path: Path
) -> None:
    """fd exhaustion during store polling must not emit the per-poll ERROR signature.

    Runs the real forwarder loop against the real server, mirrors a baseline
    exchange, genuinely exhausts the process's fd table across many polls,
    releases it, and requires (a) no ERROR-level ``cursor forwarder poll
    failed`` record with ``errno == EMFILE`` was emitted, and (b) mirroring
    resumed afterward. On the unfixed build (a) fails: every poll inside the
    window crashes at ``_chat_claimed_by_other``'s ``iterdir`` and logs the
    exact signature tracked by the ticket.
    """
    from omnigent.harnesses.cursor_native import forwarder as fwd

    capture = _RecordCapture()
    fwd_logger = logging.getLogger("omnigent.harnesses.cursor_native.forwarder")
    fwd_logger.addHandler(capture)

    # A cursor-format chat store, laid out exactly as the TUI writes it:
    # ~/.cursor/chats/<md5(workspace)>/<chat-id>/store.db (WAL-live).
    workspace = str(tmp_path / "ws")
    chat_id = "0ef42bbf-3b80-4bec-ac39-ca46531cbc47"
    chat_dir = tmp_path / "chats" / hashlib.md5(workspace.encode()).hexdigest() / chat_id
    chat_dir.mkdir(parents=True)
    store = chat_dir / "store.db"
    writer = _make_wal_store(
        store,
        [
            ("u1", _user_blob(_BASELINE_PROMPT)),
            ("a1", _assistant_blob(_BASELINE_REPLY)),
        ],
    )

    # Per-session bridge dir under a shared root — the root _chat_claimed_by_other
    # iterates every poll (the raise site in the field stack).
    bridge_dir = tmp_path / "cursor-native" / "session-under-test"
    bridge_dir.mkdir(parents=True)
    launch_ms = int(time.time() * 1000)
    # Bind the forwarder to the store the way the product's own cold-resume
    # preseed does (preseed_resume_state persists exactly this state).
    fwd._write_state(
        bridge_dir,
        fwd._ForwardState(store_path=str(store), last_rowid=0, launch_epoch_ms=launch_ms),
    )

    task = asyncio.create_task(
        fwd.forward_cursor_store_to_session(
            base_url=emfile_server,
            headers={},
            session_id=emfile_session_id,
            bridge_dir=bridge_dir,
            agent_name="cursor-native-e2e",
            workspace=workspace,
            launch_epoch_ms=launch_ms,
            poll_interval_s=_POLL_INTERVAL_S,
        )
    )
    ballast: list[int] = []
    saved_limits: tuple[int, int] | None = None
    try:
        async with httpx.AsyncClient(base_url=emfile_server, timeout=10.0) as probe:
            # 1. Baseline: the seeded exchange mirrors into the conversation.
            await _wait_for_mirrored_texts(
                probe,
                emfile_session_id,
                (_BASELINE_PROMPT, _BASELINE_REPLY),
                _MIRROR_DEADLINE_S,
            )

            # 2. The fault: exhaust the process's fd table for real, and keep
            # it pinned full across the window (a poll thread transiently
            # closing a handle would otherwise free a slot). Nothing inside
            # this window may touch files or the network from the test side.
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            saved_limits = (soft, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(soft, 256), hard))
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
            writer.execute(
                "INSERT INTO blobs(id, data) VALUES(?, ?)",
                ("a2", json.dumps(_assistant_blob(_RECOVERY_REPLY)).encode("utf-8")),
            )
            writer.commit()
            await _wait_for_mirrored_texts(
                probe, emfile_session_id, (_RECOVERY_REPLY,), _MIRROR_DEADLINE_S
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
        fwd_logger.removeHandler(capture)
        writer.close()

    # The bug-keyed assertion: fd exhaustion must not surface as the
    # unstructured per-poll ERROR the telemetry fingerprints (message prefix
    # "cursor forwarder poll failed; session=" with an EMFILE OSError).
    emfile_errors = [
        record
        for record in capture.records
        if record.levelno >= logging.ERROR
        and record.getMessage().startswith("cursor forwarder poll failed")
        and record.exc_info is not None
        and isinstance(record.exc_info[1], OSError)
        and record.exc_info[1].errno == errno.EMFILE
    ]
    assert not emfile_errors, (
        f"fd exhaustion during store polling emitted the per-poll ERROR signature "
        f"{len(emfile_errors)} time(s) (one per ~{_POLL_INTERVAL_S}s poll; ~0.7s cadence "
        f"in production). First captured record:\n{_format_record(emfile_errors[0])}"
    )
