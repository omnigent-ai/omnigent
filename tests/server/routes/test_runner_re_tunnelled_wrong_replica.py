"""Tests for the runner-stamp cross-replica guard in the message-dispatch path.

A server rollout closes the old pod's tunnels; the runner reconnects to a new
pod within seconds and that pod stamps ``runner_last_seen`` in the shared store.
A message that races onto a *different* new pod must re-address
(``WRONG_REPLICA``) rather than record a spurious ``runner_failed_to_start``
turn against a runner that is healthy on the sibling pod.
"""

import time

import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import routes_events


class _Store:
    def __init__(self, conv):
        self._conv = conv

    def get_conversation(self, session_id):
        return self._conv if self._conv is not None and session_id == self._conv.id else None


def _conv(runner_id, runner_last_seen):
    return Conversation(
        id="conv_1",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_1",
        runner_id=runner_id,
        host_id="host_1",
        runner_last_seen=runner_last_seen,
    )


async def _run(fresh_conv, monkeypatch, *, classified_runner_id, own_stamp):
    monkeypatch.setattr(routes_events, "last_liveness_stamp", lambda _rid: own_stamp)
    await routes_events._raise_if_runner_re_tunnelled_to_another_replica(
        "conv_1", classified_runner_id, _Store(fresh_conv)
    )


@pytest.mark.asyncio
async def test_fresh_sibling_stamp_raises_wrong_replica(monkeypatch):
    """A fresh stamp this process never wrote is a live sibling → WRONG_REPLICA."""
    conv = _conv("runner_abc", int(time.time()))
    with pytest.raises(OmnigentError) as exc:
        await _run(conv, monkeypatch, classified_runner_id="runner_abc", own_stamp=None)
    assert exc.value.code == ErrorCode.WRONG_REPLICA


@pytest.mark.asyncio
async def test_own_stamp_does_not_raise(monkeypatch):
    """A stamp this replica itself wrote is not evidence of a sibling."""
    now = int(time.time())
    conv = _conv("runner_abc", now)
    # Our own last stamp is at least as new as the row's → not a sibling.
    await _run(conv, monkeypatch, classified_runner_id="runner_abc", own_stamp=now)


@pytest.mark.asyncio
async def test_sibling_stamp_newer_than_own_raises(monkeypatch):
    """A row stamp newer than this replica's own write is the re-tunnel case:
    the runner re-registered on a sibling after we last stamped it."""
    now = int(time.time())
    conv = _conv("runner_abc", now)
    with pytest.raises(OmnigentError) as exc:
        await _run(conv, monkeypatch, classified_runner_id="runner_abc", own_stamp=now - 50)
    assert exc.value.code == ErrorCode.WRONG_REPLICA


@pytest.mark.asyncio
async def test_stale_stamp_does_not_raise(monkeypatch):
    """A stamp past the liveness TTL is not a live runner anywhere."""
    conv = _conv("runner_abc", int(time.time()) - 10_000)
    await _run(conv, monkeypatch, classified_runner_id="runner_abc", own_stamp=None)


@pytest.mark.asyncio
async def test_no_runner_id_does_not_raise(monkeypatch):
    """A session with no bound runner has nothing to re-address."""
    conv = _conv(None, int(time.time()))
    await _run(conv, monkeypatch, classified_runner_id=None, own_stamp=None)


@pytest.mark.asyncio
async def test_rebound_runner_does_not_raise(monkeypatch):
    """A concurrent relaunch rebound the row to a new runner; the old runner's
    retained stamp must not be read as the old runner being live elsewhere."""
    # Row now bound to runner_new (fresh stamp), but we are classifying runner_old.
    conv = _conv("runner_new", int(time.time()))
    await _run(conv, monkeypatch, classified_runner_id="runner_old", own_stamp=None)


@pytest.mark.asyncio
async def test_missing_row_does_not_raise(monkeypatch):
    """A row that can't be re-read yields no false WRONG_REPLICA."""
    await _run(None, monkeypatch, classified_runner_id="runner_abc", own_stamp=None)
