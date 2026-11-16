"""End-to-end test: Stop must clear a stale ``running`` on an idle session.

A finished session can keep ``live_status = running`` on its row while its
runner is healthy and idle, for example when a turn's ``idle`` edge is lost
across a reconnect. The snapshot trusts that row, so the UI shows "Working…"
until Stop clears it. Stop reaches the runner with no turn to cancel.

Journey: run one in-process turn to completion, stop the server, persist
``running`` on the row, restart the server with a cold status cache, then send
``interrupt`` (the web UI's Stop) and expect the session to settle to ``idle``.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_stop_clears_stale_running_e2e.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.db.enum_codecs import SESSION_LIVE_STATUS
from omnigent.util.uuid import uuid_to_bytes
from tests._helpers.live_server import terminate_process
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import (
    bind_session_runner,
    bundle_files,
    post_session_bundle,
    send_user_text,
)
from tests.e2e.conftest import configure_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_MODEL = "mock-stop-stale-running"
_REPLY = "STOP_STALE_RUNNING_REPLY"


def _status(client: httpx.Client, session_id: str) -> str | None:
    """Return the session snapshot's status, or ``None`` while the server is unavailable."""
    try:
        response = client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return response.json().get("status")


def _persist_stale_running(database: Path, session_id: str) -> None:
    """Leave ``live_status = running`` on the session's row, as a lost idle edge would."""
    with sqlite3.connect(database) as connection:
        cursor = connection.execute(
            "UPDATE omnigent_conversation_metadata SET live_status = ? WHERE id = ?",
            (SESSION_LIVE_STATUS["running"], uuid_to_bytes(session_id)),
        )
        assert cursor.rowcount == 1, f"expected one session row, updated {cursor.rowcount}"


@pytest.mark.timeout(300)
def test_stop_clears_a_stale_running_session(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """Stop on an idle session whose row still says ``running`` settles it to ``idle``."""
    mock = isolated_mock_llm_server_url
    env = {
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
        "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    base_env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TMPDIR"}}
    spec = {
        "name": "stop-stale-running",
        "prompt": "Reply with the marker.",
        "executor": {
            "harness": "openai-agents",
            "model": _MODEL,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock}/v1"},
        },
        "skills_filter": "none",
    }
    configure_mock_llm(mock, [{"text": _REPLY}], key=_MODEL)

    with server_runner(
        tmp_path / "stack", server_cwd=_REPO, server_env=env, base_env=base_env
    ) as stack:
        stack.start_runner(env=env, cwd=_REPO)
        with httpx.Client(base_url=stack.base_url, timeout=60, trust_env=False) as client:
            created = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()}),
            )
            created.raise_for_status()
            session_id = created.json()["session_id"]
            bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id)
            send_user_text(client, session_id=session_id, text="Say the marker.")
            poll_until(
                lambda: (
                    _REPLY in json.dumps(client.get(f"/v1/sessions/{session_id}").json())
                    and _status(client, session_id) == "idle"
                ),
                timeout_s=120,
            )

            # A lost idle edge: running on the row, and a restarted server with a cold cache.
            terminate_process(stack.server)
            _persist_stale_running(stack.root / "chat.db", session_id)
            stack.start_server()
            poll_until(
                lambda: client.get(f"/v1/runners/{stack.runner_id}/status").json().get("online"),
                timeout_s=60,
            )
            assert _status(client, session_id) == "running", "precondition: stale running shows"

            stopped = client.post(f"/v1/sessions/{session_id}/events", json={"type": "interrupt"})
            assert stopped.status_code < 400, stopped.text
            poll_until(lambda: _status(client, session_id) == "idle", timeout_s=30)
            # Later reads must not reopen the stale status.
            later_reads = []
            for _ in range(6):
                time.sleep(0.5)
                later_reads.append(_status(client, session_id))
            assert later_reads == ["idle"] * 6, later_reads
