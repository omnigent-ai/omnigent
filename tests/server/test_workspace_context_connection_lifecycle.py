"""Pending draft requests and viewers wake when their host tunnel disappears."""

import asyncio

import pytest

from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_host_disconnect_wakes_draft_requests_and_streams(replace):
    registry = HostRegistry()
    hello = HostHelloFrame("1", 1, "host", workspace_contexts=True)
    conn = registry.register("h", object(), hello, owner="u")
    future = asyncio.get_running_loop().create_future()
    conn.pending_workspace_contexts["request"] = future
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(object())
    conn.workspace_context_streams["channel"] = queue
    if replace:
        registry.register("h", object(), hello, owner="u")
    else:
        registry.deregister("h", conn=conn)
    with pytest.raises(ConnectionError):
        await future
    close = queue.get_nowait()
    assert close.channel_id == "channel"
    assert close.close_code == 1012
    assert not conn.pending_workspace_contexts
    assert not conn.workspace_context_streams
