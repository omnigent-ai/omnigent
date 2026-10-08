"""E2E regression test: blank cold resume when a large /items page 500s.

Reproduces the user-reported bug: resuming a **large** claude-native
conversation starts a **blank** Claude session when the server's
``GET /v1/sessions/<id>/items?limit=1000`` fails on a large page while the
same history paginates fine at smaller page sizes. Observed in production
(Databricks Apps / Lakebase Postgres): pages with ``limit<=400`` return 200,
``limit>=500`` return ``500 internal_error``. The client chain then:

* ``_fetch_all_session_items_for_claude_resume`` hardcodes ``limit=1000`` and
  raises ``ClickException`` on any >=400 — no retry at a smaller page size;
* ``_ensure_local_claude_resume_transcript`` propagates the raise (no fallback
  to an intact local ``~/.claude/projects/.../<sid>.jsonl``);
* ``_auto_create_claude_terminal`` catches everything, warns
  "launching without --resume", and starts a **fresh, empty** Claude session
  that silently becomes the live conversation.

This test drives the REAL user journey end to end — a real ``omnigent
server`` subprocess (with the deployed failure signature injected at the
conversation-store seam so the real route + exception handler produce the
production 500 body), a real runner subprocess, a real claude-native session
with 600 seeded history items and a bound prior Claude session id — and then
triggers the resume the way the web UI / a daemon relaunch does: binding the
session to the runner, which auto-creates the Claude terminal.

Desired behavior (asserted): the history IS recoverable — every page at
``limit<=400`` serves fine — so the relaunched Claude terminal must resume the
prior conversation (``--resume <external_session_id>``). On the buggy build
the terminal launches with no ``--resume`` at all, so this test FAILS with the
launched argv in the failure message.

The Claude CLI itself is replaced with a tiny stub that records its argv and
parks, so the test needs no Claude login and asserts on the launch decision —
the exact seam where the blank session is born.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_cold_resume_items_500_e2e.py -v
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner

# CI shells can carry an egress proxy in the environment; every HTTP call in
# this test targets 127.0.0.1, so bypass proxy autodetection entirely.
_http = httpx.Client(trust_env=False)


# Bootstrap for the spawned server: monkeypatch the conversation store so
# item pages ABOVE the deployed failure threshold raise — the REAL route and
# the REAL app-level exception handler then produce the production
# ``500 {"error":{"code":"internal_error",...}}`` body — while smaller pages
# (the reporter's working ``limit=100`` probe) keep succeeding. This mirrors
# the exact signature observed on the Databricks Apps deployment (200 at
# <=400, 500 at >=500) without needing that Postgres stack in CI.
_SERVER_BOOTSTRAP = """
import omnigent.stores.conversation_store.sqlalchemy_store as _s

_orig = _s.SqlAlchemyConversationStore.list_items

def _failing_list_items(self, conversation_id, limit=100, *args, **kwargs):
    if limit > 400:
        raise RuntimeError(
            "simulated deployed-DB failure reading a large item page "
            f"(limit={limit})"
        )
    return _orig(self, conversation_id, limit, *args, **kwargs)

_s.SqlAlchemyConversationStore.list_items = _failing_list_items

from omnigent.cli import main

main()
"""

# The prior Claude session id bound to the conversation (what a previous
# run's hook capture persisted as ``external_session_id``).
_EXTERNAL_SID = "11111111-2222-4333-8444-555566667777"

_POLL_S = 1.0
# Terminal auto-create includes bridge prep + tmux boot; generous for CI.
_ARGV_TIMEOUT_S = 180.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native terminals run inside tmux; tmux not installed",
)


def _seed_large_history(database_uri: str, session_id: str) -> None:
    """Append ~9MB / 600 message items straight into the server's store.

    Matches the reporter's failing conversation shape (multi-MB transcript,
    hundreds of items). Direct store writes are the same seeding pattern the
    ``tests/e2e_ui`` suite uses — there is no REST bulk-append.

    :param database_uri: The spawned server's SQLite URI.
    :param session_id: Conversation to append to.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    store = SqlAlchemyConversationStore(database_uri)
    chunk = "x" * 15000
    items = []
    for i in range(600):
        role = "user" if i % 2 == 0 else "assistant"
        items.append(
            NewConversationItem(
                type="message",
                response_id=f"resp_{i // 2}",
                data=MessageData(
                    role=role,
                    content=[
                        {
                            "type": "input_text" if role == "user" else "output_text",
                            "text": f"turn {i}: {chunk}",
                        }
                    ],
                    agent="claude" if role == "assistant" else None,
                ),
            )
        )
    for start in range(0, len(items), 50):
        store.append(session_id, items[start : start + 50])


def test_cold_resume_resumes_history_when_large_item_page_500s(
    tmp_path: Path,
) -> None:
    """
    Cold resume must restore history when /items fails only at large pages.

    Journey (the reporter's): a large claude-native conversation exists on
    the server with a bound prior Claude session id; the server 500s on
    ``/items`` pages above the deployed size threshold while smaller pages
    succeed; the user resumes the session (relaunch -> the runner
    auto-creates the Claude terminal).

    Expected: the history is recoverable (small pages work), so Claude must
    be launched with ``--resume <external_session_id>``. Buggy behavior:
    the hardcoded ``limit=1000`` fetch 500s, the failure is swallowed, and a
    blank fresh Claude session is silently launched (argv has no
    ``--resume``) — losing the user's conversation.

    :param tmp_path: Per-test temp dir (server DB, stub claude, runner HOME).
    """
    # Stub Claude CLI: records its argv (the launch decision under test) and
    # parks so the tmux pane stays alive. No Claude login needed. The runner
    # also runs headless ``claude -p "/model"`` catalog probes against the
    # stub, so every invocation APPENDS one unit-separator-joined record and
    # the assertion filters for the interactive terminal launch.
    argv_file = tmp_path / "claude_argv.txt"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(
        "#!/bin/sh\n"
        f"{{ printf '%s\\037' \"$@\"; printf '\\n'; }} >> \"{argv_file}\"\n"
        "exec sleep 600\n"
    )
    stub.chmod(0o755)

    def _terminal_launch_argv() -> list[str] | None:
        """The interactive terminal launch's argv, once recorded.

        :returns: The argv of the first recorded non-headless invocation
            (the catalog probes all run ``-p "/model"``), or ``None``.
        """
        if not argv_file.exists():
            return None
        for line in argv_file.read_text().splitlines():
            argv = line.split("\x1f")[:-1]
            if argv and "-p" not in argv:
                return argv
        return None

    with server_runner(tmp_path, server_bootstrap=_SERVER_BOOTSTRAP) as stack:
        base_url, runner_id = stack.base_url, stack.runner_id
        database_uri = stack.database_uri
        stack.start_runner(
            env={
                "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            }
        )

        # A prior large claude-native conversation with the Claude session
        # id captured — the state a user resumes into.
        session_id = str(create_native_session(_http, base_url, harness="claude")["session_id"])
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"external_session_id": _EXTERNAL_SID},
            timeout=10.0,
        ).raise_for_status()
        _seed_large_history(database_uri, session_id)

        # Sanity: the deployed failure signature is live — the reporter's
        # probe table (200 at <=400, 500 at >=500 / the client's 1000).
        ok = _http.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "asc"},
            timeout=60.0,
        )
        assert ok.status_code == 200, "small item pages must keep working"
        assert len(ok.json()["data"]) == 100
        # Uvicorn closes connections after unhandled errors. Use a disposable
        # client so the resume PATCH cannot race with this connection closing.
        big = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 1000, "order": "asc"},
            timeout=60.0,
            trust_env=False,
        )
        assert big.status_code == 500, "large-page failure signature must be live"
        assert big.json()["error"]["code"] == "internal_error"

        # THE RESUME: bind the session to the runner (what the web UI / a
        # daemon relaunch does) -> the runner auto-creates the Claude
        # terminal, deciding whether to pass --resume.
        # The bind can block on the runner-side terminal bring-up (the model
        # catalog probe alone holds a 20-30s budget against the parked stub),
        # so give it the same generous budget as the argv wait.
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=_ARGV_TIMEOUT_S,
        ).raise_for_status()

        deadline = time.monotonic() + _ARGV_TIMEOUT_S
        argv: list[str] | None = None
        while time.monotonic() < deadline:
            argv = _terminal_launch_argv()
            if argv is not None:
                break
            time.sleep(_POLL_S)
        assert argv is not None, (
            f"claude terminal never launched; runner log:\n"
            f"{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        # The bug: with the item history fully recoverable at smaller page
        # sizes, the relaunch must resume the prior Claude session. The
        # buggy build swallows the 500 and launches blank (no --resume).
        assert "--resume" in argv and _EXTERNAL_SID in argv, (
            "cold resume silently launched a BLANK claude session (no "
            "--resume) after the limit=1000 /items page 500'd, even though "
            "the same history serves fine at limit<=400 — the prior "
            f"conversation is lost. launched argv: {argv}"
        )
