"""Tests for re-delivering sub-agent wakes lost to a server outage.

A sub-agent completion is delivered to the parent's inbox locally, then a
``[System: ... waiting in inbox]`` wake notice is POSTed to the parent's event
stream — the sole signal that makes an idle parent surface the result. When
the server is unreachable at completion time (redeploy, tunnel blip) the wake
POST exhausts its bounded retries and, before this fix, nothing ever
re-attempted it: the result sat stranded until the user manually bumped the
parent. These tests pin the recovery paths: the tunnel-reconnect catch-up scan
re-attempts stranded wakes, and a drained inbox clears the stranded record.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _OutageServerClient(NullServerClient):
    """Fails wake POSTs to a parent's ``/events`` while ``down``; 200 after.

    Every other runner→server call gets :class:`NullServerClient`'s benign
    stub 200. POSTs to the watched parent's ``/events`` path return a real
    ``httpx.Response`` so ``raise_for_status`` behaves like production: 503
    while the outage lasts, 200 once ``down`` is cleared.
    """

    def __init__(self, parent_id: str) -> None:
        """
        :param parent_id: Parent session whose ``/events`` POSTs to intercept,
            e.g. ``"22b91e208e5501fb8d2b502837391f04"``.
        """
        self._parent_events_path = f"/v1/sessions/{parent_id}/events"
        self.down = True
        self.failed_posts: list[dict[str, Any]] = []
        self.delivered_posts: list[dict[str, Any]] = []
        self.delivered_seen = asyncio.Event()
        self.failure_seen = asyncio.Event()

    async def post(self, url: str, **kwargs: Any) -> Any:
        """Return 503 (down) or 200 (up) for the parent wake path.

        :param url: Request URL, e.g. ``"/v1/sessions/<parent>/events"``.
        :param kwargs: Request kwargs; the wake notice body is in ``json``.
        :returns: Real ``httpx.Response`` for the wake path, stub otherwise.
        """
        if url != self._parent_events_path:
            return await super().post(url, **kwargs)
        body = kwargs.get("json")
        request = httpx.Request("POST", f"http://runner.test{url}")
        if self.down:
            if isinstance(body, dict):
                self.failed_posts.append(body)
            self.failure_seen.set()
            return httpx.Response(503, request=request, json={"error": "RUNNER_UNAVAILABLE"})
        if isinstance(body, dict):
            self.delivered_posts.append(body)
        self.delivered_seen.set()
        return httpx.Response(200, request=request, json={})


async def _strand_a_completion(
    client: Any,
    server_client: _OutageServerClient,
    parent_id: str,
    child_id: str,
) -> None:
    """Complete *child_id* into a dead server so its wake POST is stranded.

    :param client: HTTP test client bound to the runner app.
    :param server_client: The outage-simulating server client (must be down).
    :param parent_id: Parent session id being watched.
    :param child_id: Child session id to report terminal.
    """
    resp = await client.post(
        f"/v1/sessions/{child_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "idle", "output": "CHILD_DONE"},
        },
    )
    assert resp.status_code == 204, resp.text
    await asyncio.wait_for(server_client.failure_seen.wait(), timeout=5.0)
    # Let the wake task burn through its bounded retries and give up.
    deadline = asyncio.get_running_loop().time() + 5.0
    while len(server_client.failed_posts) < 3:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"wake POST made only {len(server_client.failed_posts)} attempt(s) "
                f"within 5s; expected all 3 bounded retries against the dead server"
            )
        await asyncio.sleep(0.01)
    for _ in range(10):
        await asyncio.sleep(0)
    assert not server_client.delivered_posts, "no wake should be delivered while down"


@pytest.mark.asyncio
async def test_reconnect_redelivers_wake_stranded_by_server_outage(
    _no_wake_backoff: list[float],
) -> None:
    """The catch-up scan must re-POST a wake that failed during an outage.

    A child completes while the server is unreachable: the result lands in the
    parent's inbox but the wake POST exhausts its bounded retries. When the
    tunnel reconnects (server back up), the catch-up scan must re-attempt the
    wake so the idle parent learns its child finished — without this, the
    completion is stranded until the next user message.
    """
    from omnigent.runner import app as runner_app

    parent_id = "5f0d5a5f7f2a4be7b6a13f6a9f1c2d3e"
    child_id = "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _OutageServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="wake-loss",
    )
    try:
        async with _runner_client(app) as client:
            await _strand_a_completion(client, server_client, parent_id, child_id)
            assert session_inbox.qsize() == 1, "completion must be in the parent inbox"

            # Recovery: the server is back and the tunnel reconnected.
            server_client.down = False
            await app.state.catch_up_scan()
            try:
                await asyncio.wait_for(server_client.delivered_seen.wait(), timeout=5.0)
            except TimeoutError:
                raise AssertionError(
                    "The catch-up scan never re-attempted the stranded wake: the "
                    "sub-agent completion sits in the parent inbox with no wake "
                    "notice delivered after the reconnect, so an idle parent "
                    "never learns its child finished."
                ) from None
            assert len(server_client.delivered_posts) == 1, (
                f"Expected exactly one re-delivered wake, got "
                f"{len(server_client.delivered_posts)}."
            )
            notice_text = str(server_client.delivered_posts[0])
            assert "waiting in inbox" in notice_text, (
                f"Re-delivered POST is not a wake notice: {notice_text!r}"
            )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_reconnect_skips_stranded_wake_for_drained_inbox(
    _no_wake_backoff: list[float],
) -> None:
    """A stranded wake whose inbox has since drained must NOT be re-posted.

    If the parent drained its inbox between the failed wake and the reconnect
    (e.g. the user bumped it manually), a re-delivered wake would announce
    work that no longer exists; the catch-up scan must stay silent.
    """
    from omnigent.runner import app as runner_app

    parent_id = "1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f"
    child_id = "6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _OutageServerClient(parent_id)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="wake-loss",
    )
    try:
        async with _runner_client(app) as client:
            await _strand_a_completion(client, server_client, parent_id, child_id)

            # The user bumped the parent; the inbox drained before reconnect.
            session_inbox.get_nowait()

            server_client.down = False
            await app.state.catch_up_scan()
            # Generous yields so a (wrongly) scheduled wake would land.
            for _ in range(20):
                await asyncio.sleep(0)
            assert not server_client.delivered_posts, (
                f"A wake was re-delivered for an already-drained inbox: "
                f"{server_client.delivered_posts}."
            )
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
