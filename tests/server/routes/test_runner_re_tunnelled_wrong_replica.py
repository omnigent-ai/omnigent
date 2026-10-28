"""Fenced cross-replica guard in the message-dispatch path."""

import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.conversation_store import RunnerConnectionLiveness


class _Store:
    def __init__(self, conv, connection):
        self._conv = conv
        self._connection = connection

    def get_conversation(self, session_id):
        return self._conv if self._conv is not None and session_id == self._conv.id else None

    def get_runner_connection(self, runner_id):
        return self._connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bound_runner, own_epoch, epoch, active, last_seen, raises",
    [
        ("runner", None, "sibling", True, 1000, True),
        ("runner", "own", "own", True, 1000, False),
        ("runner", "own", "sibling", True, 1000, True),
        ("runner", None, "sibling", True, 1, False),
        ("runner", None, "sibling", False, None, False),
        ("runner", None, None, False, None, False),
        (None, None, "sibling", True, 1000, False),
        ("rebound", None, "sibling", True, 1000, False),
        ("missing", None, "sibling", True, 1000, False),
    ],
    ids=[
        "sibling",
        "own",
        "same-second",
        "expired",
        "closed",
        "legacy-only",
        "unbound",
        "rebound",
        "missing",
    ],
)
async def test_wrong_replica_requires_fresh_different_active_epoch(
    monkeypatch, bound_runner, own_epoch, epoch, active, last_seen, raises
):
    conv = (
        None
        if bound_runner == "missing"
        else Conversation(
            id="conv_1",
            created_at=1,
            updated_at=1,
            root_conversation_id="conv_1",
            runner_id=bound_runner,
            runner_last_seen=1000,
        )
    )
    connection = RunnerConnectionLiveness(
        runner_id=bound_runner,
        epoch=epoch,
        heartbeat_epoch=epoch,
        active=active,
        last_seen=last_seen,
        last_connected=1000,
        observed_at=1000,
    )
    monkeypatch.setattr(routes_events, "runner_connection_epoch", lambda _rid: own_epoch)
    call = routes_events._raise_if_runner_re_tunnelled_to_another_replica(
        "conv_1", None if bound_runner is None else "runner", _Store(conv, connection)
    )
    if raises:
        with pytest.raises(OmnigentError) as exc:
            await call
        assert exc.value.code == ErrorCode.WRONG_REPLICA
    else:
        await call
