"""The deferred teardown of a session that archived itself waits for its turn to end."""

from types import SimpleNamespace

import pytest

from omnigent.server.routes._sessions import common as sessions_common
from omnigent.server.routes._sessions import orchestration

pytestmark = pytest.mark.asyncio

SESSION = "c" * 32


class _Rows:
    """Returns each scripted read in turn, then repeats the last one."""

    def __init__(self, *reads) -> None:
        self.reads = list(reads)
        self.count = 0

    def get_session_activity(self, session_ids: list[str]):
        assert session_ids == [SESSION]
        read = self.reads[min(self.count, len(self.reads) - 1)]
        self.count += 1
        if isinstance(read, Exception):
            raise read
        return {} if read is None else {SESSION: read}

    def get_conversation(self, _session_id: str):
        raise AssertionError("the wait must not load the whole conversation, labels included")


def _row(live_status: str | None, *, archived: bool | None = True):
    return SimpleNamespace(archived=archived, live_status=live_status)


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(orchestration, "_SELF_ARCHIVE_TURN_POLL_S", 0.0)
    # No relay cache entry: the status comes from the persisted row.
    monkeypatch.delitem(sessions_common._session_status_cache, SESSION, raising=False)


async def test_waits_on_the_persisted_status_until_the_turn_ends():
    rows = _Rows(_row("running"), _row("waiting"), _row("idle"))
    assert await orchestration._await_archived_turn_end(SESSION, rows) is True
    assert rows.count == 3


async def test_an_unknown_status_keeps_waiting_for_the_turn():
    # No relay status on this replica and none persisted yet.
    rows = _Rows(_row(None), _row("idle"))
    assert await orchestration._await_archived_turn_end(SESSION, rows) is True
    assert rows.count == 2


async def test_a_failed_read_keeps_waiting_for_the_turn():
    rows = _Rows(RuntimeError("database unavailable"), _row("running"), _row("idle"))
    assert await orchestration._await_archived_turn_end(SESSION, rows) is True
    assert rows.count == 3


async def test_an_undo_ends_the_wait_without_teardown():
    rows = _Rows(_row("running"), _row("running", archived=False))
    assert await orchestration._await_archived_turn_end(SESSION, rows) is False
    assert rows.count == 2


async def test_a_deleted_session_ends_the_wait_without_teardown():
    rows = _Rows(_row("running"), None)
    assert await orchestration._await_archived_turn_end(SESSION, rows) is False
    assert rows.count == 2


async def test_a_store_without_the_archived_flag_still_waits_for_the_turn():
    rows = _Rows(_row("running", archived=None), _row("idle", archived=None))
    assert await orchestration._await_archived_turn_end(SESSION, rows) is True
    assert rows.count == 2


@pytest.mark.parametrize("read", [_row("running"), RuntimeError("database unavailable")])
async def test_a_turn_that_never_ends_is_torn_down_at_the_deadline(monkeypatch, read):
    # A spent deadline ends the wait after the read that found the turn running
    # (or failed), without depending on how fast the reads run.
    monkeypatch.setattr(orchestration, "_SELF_ARCHIVE_TURN_WAIT_S", 0)
    rows = _Rows(read)
    assert await orchestration._await_archived_turn_end(SESSION, rows) is True
    assert rows.count == 1
