"""Decide from its row whether a dropped runner's host is offline everywhere."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from omnigent.db.utils import now_epoch
from omnigent.server.routes._sessions.orchestration import _host_row_offline
from omnigent.stores.host_store import Host

_HOST_ID = "host_0123456789abcdef"


def _host(*, status: str = "offline", sandbox_provider: str | None = None) -> Host:
    now = now_epoch()
    return Host(
        host_id=_HOST_ID,
        name="test-laptop",
        user_id="user@example.com",
        status=status,
        created_at=now - 3600,
        updated_at=now,
        sandbox_provider=sandbox_provider,
    )


class _HostStore:
    def __init__(
        self, host: Callable[[], Host] | None = None, *, error: Exception | None = None
    ) -> None:
        # Build the row at read time so its last-seen is fresh when the test runs.
        self.host = host
        self.error = error

    def get_host(self, host_id: str) -> Host | None:
        if self.error is not None:
            raise self.error
        return self.host() if self.host is not None else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "expected"),
    [
        # A laptop that went to sleep, gracefully or not.
        (_HostStore(_host), True),
        (_HostStore(lambda: replace(_host(status="online"), updated_at=0)), True),
        # Live by its row, possibly on another replica.
        (_HostStore(lambda: _host(status="online")), False),
        # A managed sandbox has its own wake path; a missing row has no host to wait on.
        (_HostStore(lambda: _host(sandbox_provider="e2b")), False),
        (_HostStore(), False),
        (None, False),
        # An unreadable row is inconclusive either way.
        (_HostStore(error=RuntimeError("store down")), None),
    ],
)
async def test_host_row_offline(store: _HostStore | None, expected: bool | None) -> None:
    """Only a user-run host whose row reads gone counts as offline."""
    assert await _host_row_offline(store, _HOST_ID) is expected  # type: ignore[arg-type]
