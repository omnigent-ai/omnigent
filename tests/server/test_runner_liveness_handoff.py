"""Same-second cross-replica liveness handoff: the old replica's conditional clear
erases the new holder's same-second stamp; the next heartbeat must restore liveness
and neither the grace timer nor the SSE relay may end the in-flight turn."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from omnigent.server.routes._sessions.orchestration import (
    _runner_live_on_another_replica_from_conversations,
)
from omnigent.stores.conversation_store import (
    RUNNER_LIVENESS_TTL_S,
    runner_seen_is_fresh,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_RUNNER_ID = "runner-same-second-handoff"
_HEARTBEAT_S = 30  # PING_INTERVAL_S: the tunnel-holding replica re-stamps every 30s.


@pytest.fixture()
def store(tmp_path: Path) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'conv.db'}")


def test_same_second_handoff_next_heartbeat_restores_and_spares_turn(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(title="cross-replica-handoff")
    assert store.set_runner_id(conv.id, _RUNNER_ID)

    # runner_seen_is_fresh() and the live-elsewhere check compare against the
    # real int(time.time()), so anchor the scenario to wall-clock seconds.
    now = int(time.time())
    handoff_second = now - _HEARTBEAT_S

    # Replica A last stamped at `handoff_second`; the runner then re-tunnels
    # to replica B, which stamps in the same epoch second.
    store.touch_runner_liveness([_RUNNER_ID], now=handoff_second)  # A's last stamp
    store.touch_runner_liveness([_RUNNER_ID], now=handoff_second)  # B's same-second stamp
    reference_stamp = handoff_second  # captured by A's disconnect handler before clearing

    # A's graceful disconnect clears liveness guarded by its own last stamp,
    # erasing B's same-second stamp (the transient-offline window).
    store.clear_runner_liveness(_RUNNER_ID, not_after=reference_stamp)

    # B keeps stamping; its next heartbeat lands one interval later, well
    # inside the liveness TTL.
    assert now - reference_stamp < RUNNER_LIVENESS_TTL_S
    store.touch_runner_liveness([_RUNNER_ID], now=now)

    connectivity = store.get_session_connectivity([conv.id])[conv.id]
    assert runner_seen_is_fresh(connectivity.runner_last_seen), (
        "next heartbeat from the new holder did not restore runner liveness "
        "after the same-second cross-replica clear"
    )

    conv_row = store.get_conversation(conv.id)
    assert conv_row is not None
    assert _runner_live_on_another_replica_from_conversations(
        [conv_row], _RUNNER_ID, reference_stamp
    ), (
        "grace timer / relay would end the turn: the runner's fresh stamp on "
        "the new replica is not recognized as live on another replica"
    )
