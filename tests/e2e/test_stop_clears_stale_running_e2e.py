"""End-to-end test: Stop must clear a stale ``running`` on an idle session with a live runner.

A finished session can keep ``live_status = running`` on its row while its runner is healthy
and idle, for example when a turn's ``idle`` edge is lost across a reconnect. The snapshot and
the session list trust that row, so the UI shows "Working…" until Stop clears it, and Stop
reaches the runner with no turn to cancel. ``test_orphaned_running_session_stop`` covers the
same symptom when the runner is gone; this covers it with the runner still connected.

Journey: run one in-process turn to completion, persist ``running`` on the row, restart the
server so its status cache is cold, then send ``interrupt`` or ``stop_session`` and expect the
snapshot, session list, and stored row to report ``idle``.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_stop_clears_stale_running_e2e.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
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


def _snapshot_status(client: httpx.Client, session_id: str) -> str | None:
    """Return the session snapshot's status, or ``None`` while the server is unavailable."""
    try:
        response = client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return response.json().get("status")


def _listed_status(client: httpx.Client, session_id: str) -> str | None:
    """Return the session's status in the session list, which drives the sidebar spinner."""
    response = client.get("/v1/sessions", params={"visibility": "all", "limit": 100})
    response.raise_for_status()
    for item in response.json().get("data", []):
        if item.get("id") == session_id:
            return item.get("status")
    return None


@pytest.mark.timeout(300)
@pytest.mark.parametrize("stop_type", ["interrupt", "stop_session"])
def test_stop_clears_a_stale_running_session(
    tmp_path: Path, isolated_mock_llm_server_url: str, stop_type: str
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
                    and _snapshot_status(client, session_id) == "idle"
                ),
                timeout_s=120,
            )

            # A lost idle edge: running on the row, then a restart so no cache contradicts it.
            store = SqlAlchemyConversationStore(stack.database_uri)
            store.set_session_live_status(session_id, "running")
            stored = store.get_conversation(session_id)
            assert stored is not None and stored.live_status == "running", "precondition"
            stack.restart_server()
            assert _snapshot_status(client, session_id) == "running", "precondition"
            assert _listed_status(client, session_id) == "running", "precondition"

            stopped = client.post(f"/v1/sessions/{session_id}/events", json={"type": stop_type})
            assert stopped.status_code < 400, stopped.text
            poll_until(lambda: _snapshot_status(client, session_id) == "idle", timeout_s=30)
            assert _listed_status(client, session_id) == "idle"
            poll_until(
                lambda: (
                    (stored := store.get_conversation(session_id)) is not None
                    and stored.live_status == "idle"
                ),
                timeout_s=30,
            )
