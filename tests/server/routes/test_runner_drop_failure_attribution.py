"""The failure row for a held turn names the drop that interrupted it."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from omnigent.runner.transports.ws_tunnel.frames import RUNNER_IDLE_SHUTDOWN_CLOSE_REASON
from omnigent.server import disconnect_evidence
from omnigent.server.disconnect_evidence import DisconnectEvidence
from omnigent.server.routes._sessions import helpers
from omnigent.server.schemas import ErrorDetail

_DETAIL = "Runner disconnected unexpectedly."


def _session(**fields: object) -> SimpleNamespace:
    base = {
        "connected_at": 1_000.0,
        "close_code": None,
        "close_reason": None,
        "peer_close_code": None,
        "peer_close_reason": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def test_drop_evidence_names_abnormal_close_without_free_text() -> None:
    session = _session(peer_close_code=1006, peer_close_reason="host went to sleep, dave's laptop")

    evidence = DisconnectEvidence.from_session(session, now=1_013.25)

    assert evidence.close_code == 1006
    assert evidence.close_reason_category == "runner_closed"
    assert evidence.connection_age_s == 13.25
    attributes = evidence.log_attributes(hold_s=86400.0, hold_outcome="expired")
    assert attributes == {
        "drop_disconnected_at": "1970-01-01T00:16:53.250+00:00",
        "drop_close_code": "1006",
        "drop_close_reason_category": "runner_closed",
        "drop_connection_age_s": "13.25",
        "hold_s": "86400.0",
        "hold_outcome": "expired",
    }
    assert "dave" not in repr(attributes)


def test_idle_shutdown_and_server_close_are_categories_not_text() -> None:
    idle = _session(peer_close_code=1000, peer_close_reason=RUNNER_IDLE_SHUTDOWN_CLOSE_REASON)
    assert DisconnectEvidence.from_session(idle, now=1_001.0).close_reason_category == (
        "idle_shutdown"
    )
    server = _session(close_code=4003, close_reason="shard reassigned for host 1234")
    evidence = DisconnectEvidence.from_session(server, now=1_001.0)
    assert evidence.close_code == 4003
    assert evidence.close_reason_category == "server_closed"
    assert evidence.log_attributes(hold_s=None, hold_outcome=None)["hold_s"] == "none"


def test_failure_row_carries_drop_attributes(caplog: pytest.LogCaptureFixture) -> None:
    helpers._session_status_cache["conv_drop1"] = "running"
    evidence = {"drop_close_code": "1006", "hold_outcome": "expired"}
    try:
        with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
            helpers._publish_status(
                "conv_drop1",
                "failed",
                ErrorDetail(code="runner_disconnected", message=_DETAIL),
                failure_origin="runner_offline_sweep",
                disconnect_evidence=evidence,
            )
    finally:
        helpers._session_status_cache.pop("conv_drop1", None)
    [record] = [r for r in caplog.records if "session turn failed" in r.getMessage()]
    assert record.attributes["drop_close_code"] == "1006"
    assert record.attributes["hold_outcome"] == "expired"
    assert record.getMessage().endswith(": Runner disconnected unexpectedly.")


def test_no_evidence_leaves_failure_row_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    helpers._session_status_cache["conv_drop2"] = "running"
    try:
        with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
            helpers._publish_status("conv_drop2", "failed", None, failure_origin="x")
    finally:
        helpers._session_status_cache.pop("conv_drop2", None)
    [record] = [r for r in caplog.records if "session turn failed" in r.getMessage()]
    assert not any(key.startswith("drop_") for key in record.attributes)
    assert disconnect_evidence.drop_attributes(None, hold_s=None, hold_outcome=None) == {}


async def test_offline_sweep_forwards_drop_evidence_to_failure_row(
    db_uri: str, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    row = store.create_conversation()
    store.set_runner_id(row.id, "runner-drop-evidence")
    store.set_session_live_status(row.id, "running")
    sessions._session_status_cache[row.id] = "running"
    try:
        with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
            await orchestration._mark_runner_sessions_offline_impl(
                [store.get_conversation(row.id)],
                ErrorDetail(code="runner_disconnected", message=_DETAIL),
                store,
                disconnect_evidence={"drop_close_code": "1006", "hold_outcome": "expired"},
            )
    finally:
        sessions._session_status_cache.pop(row.id, None)
    [record] = [r for r in caplog.records if "session turn failed" in r.getMessage()]
    assert record.attributes["drop_close_code"] == "1006"
    assert record.attributes["origin"] == "runner_offline_sweep"
