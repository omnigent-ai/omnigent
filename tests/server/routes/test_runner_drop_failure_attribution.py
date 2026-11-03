"""The failure row for a held turn names the drop that interrupted it."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast

import pytest

from omnigent.runner.transports.ws_tunnel.frames import RUNNER_IDLE_SHUTDOWN_CLOSE_REASON
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession, TunnelEnd
from omnigent.server import disconnect_evidence
from omnigent.server.disconnect_evidence import DisconnectEvidence
from omnigent.server.routes._sessions import helpers
from omnigent.server.schemas import ErrorDetail

_DETAIL = "Runner disconnected unexpectedly."


def _session(end: TunnelEnd | None) -> RunnerSession:
    return cast(RunnerSession, SimpleNamespace(connected_at=1_000.0, ended=end))


def _end(**fields: object) -> TunnelEnd:
    base: dict[str, object] = {
        "at": 1_013.25,
        "peer_code": None,
        "peer_reason": None,
        "server_code": None,
    }
    base.update(fields)
    return TunnelEnd(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("end", "code", "category"),
    [
        (_end(peer_code=1006), 1006, "abnormal"),
        (_end(peer_code=1006, peer_reason="host went to sleep, dave's laptop"), 1006, "abnormal"),
        (
            _end(peer_code=1000, peer_reason=RUNNER_IDLE_SHUTDOWN_CLOSE_REASON),
            1000,
            "idle_shutdown",
        ),
        (_end(peer_code=1001, peer_reason="runner shutting down"), 1001, "runner_closed"),
        (_end(server_code=4003), 4003, "server_closed"),
        # A peer reply to a close this server requested first is still the server's close.
        (_end(server_code=1001, peer_code=1006), 1006, "server_closed"),
        (_end(), None, "none"),
    ],
    ids=[
        "reasonless-1006",
        "1006-with-text",
        "idle-shutdown",
        "runner-close",
        "server-close",
        "server-then-peer",
        "no-close",
    ],
)
def test_drop_evidence_is_a_bounded_category(
    end: TunnelEnd, code: int | None, category: str
) -> None:
    evidence = DisconnectEvidence.from_session(_session(end))

    assert evidence is not None
    assert (evidence.close_code, evidence.close_reason_category) == (code, category)
    assert evidence.connection_age_s == 13.25
    attributes = evidence.log_attributes(hold_s=86400.0, hold_outcome="expired")
    assert attributes["drop_disconnected_at"] == "1970-01-01T00:16:53.250+00:00"
    assert attributes["drop_close_code"] == ("unknown" if code is None else str(code))
    assert attributes["hold_s"] == "86400.0"
    assert "dave" not in repr(attributes)


def test_a_session_without_an_end_snapshot_has_no_evidence() -> None:
    assert DisconnectEvidence.from_session(_session(None)) is None


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
