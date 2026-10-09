"""An idle session must survive handoff when the old replica cannot read state.

Two real server processes share storage and a real runner reconnects through
a TCP proxy. Only the old replica's store reads are fault-injected. A completed
transcript without a saved live status models an imported/legacy idle session;
the runner sends heartbeats, not a new turn that would warm the status cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.e2e import test_runner_tunnel_mid_turn_reconnect_grace_e2e as rig
from tests.e2e.conftest import lookup_agent_id, register_inline_agent

_READ_OUTAGE_BOOTSTRAP = """
import sys
from pathlib import Path

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

fault_file = Path(sys.argv.pop(1))
sys.argv.pop(1)

def unavailable_when_armed(method):
    def read(self, *args, **kwargs):
        if fault_file.exists():
            raise ConnectionError('UNAVAILABLE: failed to connect to session backend')
        return method(self, *args, **kwargs)
    return read

for name in ('get_conversation', 'get_runner_liveness'):
    setattr(SqlAlchemyConversationStore, name,
            unavailable_when_armed(getattr(SqlAlchemyConversationStore, name)))

from omnigent.cli import main
main()
"""


@pytest.mark.timeout(240)
def test_idle_session_stays_healthy_after_unreadable_replica_handoff(
    page: Page,
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late old-replica timeout cannot add a disconnect error after recovery."""
    monkeypatch.setattr(rig, "_UNAVAILABLE_BACKEND_BOOTSTRAP", _READ_OUTAGE_BOOTSTRAP)
    stack = rig._ReconnectStack(mock_llm_server_url, tmp_path, fail_conversation_reads=True)
    replica = None
    try:
        stack.start()
        agent_name = register_inline_agent(
            stack.client,
            name="idle-handoff",
            harness="openai-agents",
            model="idle-handoff-model",
            profile="",
            prompt="Answer briefly.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        # Seed legacy/imported history, without a lifecycle edge in this process.
        store = SqlAlchemyConversationStore(stack._database_uri, stack._conversation_database_uri)
        session_id = store.create_conversation(
            agent_id=lookup_agent_id(stack.client, agent_name), runner_id=stack.runner_id
        ).id
        store.append(
            session_id,
            [
                NewConversationItem(
                    type="message",
                    response_id="imported-completed-turn",
                    data=MessageData(
                        role="user",
                        content=[{"type": "input_text", "text": "Check this session."}],
                    ),
                ),
                NewConversationItem(
                    type="message",
                    response_id="imported-completed-turn",
                    data=MessageData(
                        role="assistant",
                        agent="idle-handoff",
                        content=[{"type": "output_text", "text": "Check complete."}],
                    ),
                ),
            ],
        )
        conversation = store.get_conversation(session_id)
        assert conversation is not None and conversation.live_status is None

        replica = stack.start_second_replica()
        # Adoption reads existing history with a cold cache, as after a restart.
        assert stack.proxy is not None
        stack.proxy.begin_blackout()
        stack.proxy.end_blackout()
        rig._poll_until(
            lambda: (
                f"runner stream ready for session={session_id}" in stack.process_log.read_text()
            ),
            timeout=30,
            what="replica A to adopt the existing idle session",
        )

        # Leave A's chat unopened: a snapshot request actively refreshes status.
        assert stack.proxy is not None
        stack.proxy.begin_blackout()
        stack.proxy.retarget("127.0.0.1", replica.port)
        stack.proxy.end_blackout()
        rig._poll_until(replica.runner_online, timeout=30, what="runner recovery on replica B")
        rig._poll_until(
            lambda: (
                f"runner stream ready for session={session_id}" in replica.process_log.read_text()
            ),
            timeout=30,
            what="the recovered session stream on replica B",
        )
        # The recovered runner is healthy before A's 90-second grace expires.
        assert stack.conversation_read_fault is not None
        stack.conversation_read_fault.touch()
        page.goto(f"{replica.base_url}/c/{session_id}")
        expect(page.get_by_text("Check complete.", exact=True)).to_be_visible()
        expect(page.get_by_test_id("error-pill")).to_have_count(0)

        rig._poll_until(
            lambda: (
                f"Runner disconnect for session={session_id}:" in stack.process_log.read_text()
            ),
            timeout=110,
            what="the old replica's disconnect decision after its real grace period",
        )
        log = stack.process_log.read_text()
        assert f"live-status read failed for session={session_id}" in log
        assert f"liveness lookup failed for session={session_id}" in log
        assert replica.runner_online()

        # A browser request can return to A once its backend reads recover.
        stack.conversation_read_fault.unlink()
        page.goto(f"{stack.base_url}/c/{session_id}")
        expect(page.get_by_text("Check complete.", exact=True)).to_be_visible()
        pill = page.get_by_test_id("error-pill")
        if pill.count():
            pill.get_by_role("button", expanded=False).first.click()
            expect(pill).to_contain_text("Runner disconnected unexpectedly.")
        snapshot = rig._session_snapshot(replica.client, session_id)
        expect(
            pill, f"Recovered idle session was falsely failed: {json.dumps(snapshot)}"
        ).to_have_count(0)
        assert snapshot["status"] == "idle"
        assert snapshot.get("last_task_error") is None
        assert not rig._FAILURE_SIGNATURE.search(log)
    finally:
        if stack.conversation_read_fault is not None:
            stack.conversation_read_fault.unlink(missing_ok=True)
        if replica is not None:
            replica.teardown()
        stack.teardown()
