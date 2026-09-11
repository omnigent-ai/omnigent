"""E2E: cursor-native transcript elicitation polling under fd exhaustion.

Reported failure (telemetry, macOS dev build): the cursor-native
transcript-elicitation supervisor's poll pass dies in chat-store discovery with
``OSError: [Errno 24] Too many open files`` (``hash_dir.iterdir()`` inside
``forwarder._scan_hash_dir``, via ``_discover_store``) and is logged at ERROR as
``cursor transcript elicitation poll failed; session=…`` — the measured KPI
signature. The loop retries every ``poll_interval_s``, so one fd-exhaustion
episode emits an ERROR-traceback storm (one per pass), and while the fault
holds a Cursor tool-approval gate waiting in the chat store cannot surface as a
web approval card.

This test drives the reported journey end-to-end against a real server, with
the runner-side supervisor running unmocked in its own process (the deployed
topology) and a *genuine* transient fd-exhaustion window (``RLIMIT_NOFILE``
lowered, descriptors hoarded until ``EMFILE``):

1. a session exists on the live server;
2. the supervisor polls with the session's chat store not yet bound (the
   discovery phase the observed traceback fired in);
3. the process's fd budget is exhausted for a bounded window, then freed;
4. a pending gated tool call is waiting in the (real, on-disk) cursor chat
   store; after the fault clears it must surface as a real pending elicitation
   on the server and be resolvable through the web approval path.

Assertions:

* **regression guard (red on the reported bug)** — a *transient* fd-exhaustion
  window must not emit the ERROR-level KPI signature
  ``cursor transcript elicitation poll failed``; transient OS resource
  exhaustion is an environmental condition, not an omnigent-error per pass;
* **behavior guard (must stay green)** — the supervisor survives the window
  and the pending gate still surfaces to the web session once fds recover, so
  a fix cannot simply swallow poll failures and break approval mirroring.

The real ``cursor-agent`` TUI is not in the loop: CI's ``cursor-agent`` is
unauthenticated (the TUI hangs without an interactive login — see
``test_cursor_native_cli_e2e``), and organic runner-process fd exhaustion is
not externally injectable. The chat store is fabricated on disk in the exact
shape cursor writes (the same fixture shape ``tests/test_cursor_native_permissions``
uses), and everything downstream of it — discovery, store parsing, settling,
the hook POST, the server's elicitation registry, the web approval path — is
real.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e._native_resume_helpers import (
    poll_for_pending_elicitation,
    resolve_elicitation,
)
from tests.e2e.conftest import (
    create_runner_bound_session,
    register_inline_agent,
)

pytestmark = [
    pytest.mark.skipif(
        os.name != "posix",
        reason="RLIMIT_NOFILE-based fd-exhaustion injection requires POSIX",
    ),
    pytest.mark.timeout(180, method="signal"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The KPI signature measured by the bug report (message prefix of the ERROR
# record emitted by the supervisor's catch-all, logger
# omnigent.harnesses.cursor_native.permissions).
_POLL_FAILED_SIGNATURE = "cursor transcript elicitation poll failed"

# One gated Shell call waits in the chat store; its command doubles as the
# needle the server-side pending elicitation is matched by.
_GATED_COMMAND = "touch FD_EXHAUSTION_GATED_MARKER"

# Child-driver timeline (seconds). Poll cadence is compressed the same way the
# unit suite compresses it; the fault window spans many passes so the per-pass
# ERROR storm is unambiguous.
_POLL_INTERVAL_S = 0.05
_SETTLE_S = 0.3
_WARMUP_S = 0.5
_FAULT_HOLD_S = 1.0
_SURFACE_WAIT_S = 20.0

_RESULT_WAIT_S = 60.0
_ELICITATION_WAIT_S = 30.0

# Runs the REAL supervisor loop (unmocked discovery / store parsing / HTTP) in
# a separate process — the runner's topology — so the genuine fd exhaustion is
# contained and cannot destabilize pytest or the server. Reads its scenario
# from argv, reports as JSON (atomic rename), never asserts itself.
_DRIVER = r'''
"""Repro driver: real elicitation supervisor + genuine EMFILE window."""

import asyncio
import hashlib
import json
import logging
import os
import resource
import sqlite3
import sys
import time
import traceback
from pathlib import Path

BASE_URL, SESSION_ID, WORKSPACE, RESULT_PATH, GATED_COMMAND = sys.argv[1:6]
POLL_INTERVAL_S, SETTLE_S, WARMUP_S, FAULT_HOLD_S, SURFACE_WAIT_S = (
    float(v) for v in sys.argv[6:11]
)

from omnigent.harnesses.cursor_native import permissions as cnp
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

records = []


class _Capture(logging.Handler):
    def emit(self, record):
        exc_txt = ""
        if record.exc_info and record.exc_info[1] is not None:
            exc_txt = "".join(traceback.format_exception(*record.exc_info))
        records.append(
            {"level": record.levelname, "msg": record.getMessage(), "exc": exc_txt}
        )


_log = logging.getLogger("omnigent.harnesses.cursor_native")
_log.setLevel(logging.DEBUG)
_log.addHandler(_Capture())


def write_chat(chats_root, chat_name, created_ms, command):
    """Write a cursor-shaped chat dir: meta.json + store.db with one gated call."""
    chat_dir = chats_root / hashlib.md5(WORKSPACE.encode("utf-8")).hexdigest() / chat_name
    chat_dir.mkdir(parents=True)
    (chat_dir / "meta.json").write_text(json.dumps({"createdAtMs": created_ms}))
    pending = {
        "id": "1",
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": "call_fd_gated\nfc",
                "toolName": "Shell",
                "args": {"command": command},
            }
        ],
        "providerOptions": {"cursor": {"pendingToolCallStartedAtMs": created_ms}},
    }
    con = sqlite3.connect(str(chat_dir / "store.db"))
    try:
        con.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
        con.execute(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            ("b0", json.dumps(pending).encode("utf-8")),
        )
        con.commit()
    finally:
        con.close()


def write_result(result):
    tmp = RESULT_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh)
    os.replace(tmp, RESULT_PATH)


async def main():
    chats_root = Path.home() / ".cursor" / "chats"
    chats_root.mkdir(parents=True)
    launch_ms = int(time.time() * 1000)
    # A STALE chat (created long before the discovery floor) keeps the loop in
    # the discovery phase every pass — the phase the observed traceback fired
    # in — while making _scan_hash_dir iterate the exact md5(workspace) dir.
    write_chat(chats_root, "stale-chat", launch_ms - 3_600_000, "true")

    supervisor = asyncio.create_task(
        cnp.supervise_cursor_transcript_elicitations(
            base_url=BASE_URL,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            session_id=SESSION_ID,
            bridge_dir=Path.home() / "bridge",
            workspace=WORKSPACE,
            launch_epoch_ms=launch_ms,
            poll_interval_s=POLL_INTERVAL_S,
            settle_s=SETTLE_S,
        )
    )
    await asyncio.sleep(WARMUP_S)  # clean passes: warm thread pool, no store yet
    warmup_errors = sum(1 for r in records if r["level"] == "ERROR")

    # ── genuine transient fd exhaustion ────────────────────────────────
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
    hoard = []
    hoard_errno = None
    try:
        while True:
            hoard.append(os.open(os.devnull, os.O_RDONLY))
    except OSError as exc:
        hoard_errno = exc.errno
    selfcheck_errno = None
    try:
        os.listdir(str(chats_root))
    except OSError as exc:
        selfcheck_errno = exc.errno

    await asyncio.sleep(FAULT_HOLD_S)  # many poll passes under real EMFILE

    for fd in hoard:
        os.close(fd)
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    # ── fault cleared ──────────────────────────────────────────────────

    # Now the gate the user is waiting on: a live chat (created past the
    # discovery floor) holding one pending Shell call.
    write_chat(chats_root, "live-chat", int(time.time() * 1000), GATED_COMMAND)

    surfaced = False
    deadline = time.monotonic() + SURFACE_WAIT_S
    while time.monotonic() < deadline:
        if any(r["msg"].startswith("cursor elicitation: surfacing") for r in records):
            surfaced = True
            break
        await asyncio.sleep(0.05)

    poll_failed = [
        {"msg": r["msg"], "exc_tail": r["exc"][-800:]}
        for r in records
        if r["level"] == "ERROR" and r["msg"].startswith(POLL_FAILED_SIGNATURE)
    ]
    write_result(
        {
            "hoard_errno": hoard_errno,
            "selfcheck_errno": selfcheck_errno,
            "warmup_errors": warmup_errors,
            "surfaced": surfaced,
            "poll_failed_count": len(poll_failed),
            "poll_failed": poll_failed[:3],
            "error_msgs": [r["msg"] for r in records if r["level"] == "ERROR"][:20],
        }
    )
    # Stay alive so the parked permission hook keeps the server-side pending
    # elicitation open while the parent observes and resolves it; the parent
    # terminates this process.
    await asyncio.sleep(120)
    supervisor.cancel()


asyncio.run(main())
'''.replace("POLL_FAILED_SIGNATURE", repr(_POLL_FAILED_SIGNATURE))


def test_transient_fd_exhaustion_does_not_emit_poll_failed_errors(
    http_client: httpx.Client,
    live_server: str,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> None:
    """A transient EMFILE window must not log the KPI ERROR signature, and the
    pending Cursor gate must still surface as a web approval card afterward."""
    agent_name = register_inline_agent(
        http_client,
        name=f"cursor-fd-repro-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=f"mock-cursor-fd-{uuid.uuid4().hex[:6]}",
        profile="",
        prompt="repro agent (never dispatched)",
        mock_llm_base_url=f"{mock_llm_server_url}/v1" if mock_llm_server_url else None,
    )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result_path = tmp_path / "driver-result.json"
    driver_path = tmp_path / "fd_exhaustion_driver.py"
    driver_path.write_text(_DRIVER)
    driver_log = tmp_path / "driver.log"

    env = {
        **os.environ,
        "HOME": str(fake_home),
        "PYTHONPATH": os.pathsep.join(
            p for p in (str(_REPO_ROOT), os.environ.get("PYTHONPATH")) if p
        ),
    }
    with open(driver_log, "wb") as log_fh:
        child = subprocess.Popen(
            [
                sys.executable,
                str(driver_path),
                live_server,
                session_id,
                str(workspace),
                str(result_path),
                _GATED_COMMAND,
                str(_POLL_INTERVAL_S),
                str(_SETTLE_S),
                str(_WARMUP_S),
                str(_FAULT_HOLD_S),
                str(_SURFACE_WAIT_S),
            ],
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + _RESULT_WAIT_S
            while time.monotonic() < deadline and not result_path.exists():
                if child.poll() is not None:
                    pytest.fail(
                        "driver exited before reporting "
                        f"(rc={child.returncode}):\n{driver_log.read_text()[-4000:]}"
                    )
                time.sleep(0.2)
            if not result_path.exists():
                pytest.fail(
                    f"driver produced no result within {_RESULT_WAIT_S}s:\n"
                    f"{driver_log.read_text()[-4000:]}"
                )
            result = json.loads(result_path.read_text())

            # Fault-injection realism gate: the window must have been a genuine
            # EMFILE (both the hoard and an independent listdir self-check).
            if result["selfcheck_errno"] != errno.EMFILE:
                pytest.fail(
                    "fd-exhaustion injection did not produce a genuine EMFILE "
                    f"(hoard_errno={result['hoard_errno']}, "
                    f"selfcheck_errno={result['selfcheck_errno']}); "
                    "infrastructure problem, not a verdict on the bug"
                )

            # Behavior guard: the supervisor survived the fault window and the
            # pending gate surfaced — visible on the real server as a pending
            # elicitation — and is resolvable through the web approval path.
            assert result["surfaced"], (
                "pending Cursor gate never surfaced after the fd-exhaustion "
                f"window cleared; driver errors: {result['error_msgs']}"
            )
            pending = poll_for_pending_elicitation(
                http_client,
                conversation_id=session_id,
                timeout=_ELICITATION_WAIT_S,
                needle=_GATED_COMMAND,
            )
            resolve_elicitation(
                http_client,
                conversation_id=session_id,
                elicitation_id=str(pending["elicitation_id"]),
                action="accept",
            )

            # Regression guard (the reported bug): a bounded, transient
            # fd-exhaustion episode must not be logged as per-pass omnigent
            # ERRORs — unfixed, every pass inside the window emits one full
            # ERROR traceback (~poll cadence), which is the measured failure.
            assert result["poll_failed_count"] == 0, (
                f"transient EMFILE window produced {result['poll_failed_count']} "
                f"ERROR-level '{_POLL_FAILED_SIGNATURE}' records (one per poll "
                "pass) — the reported KPI signature; transient OS resource "
                "exhaustion must be handled as a transient condition, not an "
                "omnigent error storm. First captured record: "
                f"{json.dumps(result['poll_failed'][:1], indent=2)}"
            )
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
