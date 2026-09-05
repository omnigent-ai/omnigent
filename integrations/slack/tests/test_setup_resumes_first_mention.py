"""E2E: an unconfigured user's first mention must be resumed after setup.

The reported journey: a Slack user who has never configured the bot
@-mentions it with a request; the bot answers with the setup DM; the user
completes setup — and their original message silently vanishes. Nothing runs
until they notice and re-send the exact same mention.

This test drives that journey through the vertical-integration harness
(real ``SlackOmnigentService`` + ``SetupFlow`` + ``SQLiteStore`` and a real
``OmnigentClient`` over httpx against :class:`FakeOmnigentServer`; Slack/Bolt
transport replaced by :class:`RecordingSlackClient`, handlers invoked directly
— see ``test_integration.py``) and asserts the un-dropped behavior: once setup
completes, the mention that triggered it runs without being re-sent — a
session is created, the mention text is submitted as the turn prompt, and the
answer streams back into Slack.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import respx
from fakes import FakeOmnigentServer, RecordingSlackClient
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.service import SlackOmnigentService
from omnigent_slack.setup import AGENT_BLOCK, HOST_BLOCK, WORKSPACE_BLOCK, SetupFlow
from omnigent_slack.store import SQLiteStore

_SERVER = "http://omnigent.test"
_MENTION_TEXT = "summarize the plan in this thread"

# Generous ceiling for the event-driven waits below: a healthy (fixed) run
# returns almost immediately; only the broken path spends this budget.
_WAIT_TIMEOUT_S = 10.0


async def _noop_ack(**kwargs: object) -> None:
    return None


def _select_view() -> dict:
    """The Block Kit state Slack posts when the user submits the setup modal."""
    return {
        "state": {
            "values": {
                AGENT_BLOCK: {
                    "agent_select": {
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "debby"},
                            "value": "ag_1",
                        }
                    }
                },
                HOST_BLOCK: {
                    "host_select": {
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "Host One"},
                            "value": "h1",
                        }
                    }
                },
                WORKSPACE_BLOCK: {"workspace_input": {"value": "/home/bot/work"}},
            }
        },
    }


async def _wait_for_turns(service: SlackOmnigentService, timeout: float) -> None:
    """Await the service's tracked background turn tasks (event-driven)."""
    tasks = list(service._turn_tasks)
    if not tasks:
        return
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)


async def _wait_for_resumed_turn(
    server: FakeOmnigentServer,
    service: SlackOmnigentService,
    client: RecordingSlackClient,
    timeout: float = _WAIT_TIMEOUT_S,
) -> None:
    """Give a post-setup resumed turn every chance to spawn and finish.

    The resume may be scheduled asynchronously by setup completion, so poll
    briefly for the turn to appear, then await it; return as soon as the turn
    delivered its streamed answer. Only the broken path exhausts the timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await _wait_for_turns(service, timeout)
        if (
            server.find("POST", "/v1/sessions") is not None
            and client.streams
            and client.streams[-1].stopped
        ):
            return
        await asyncio.sleep(0.05)


@respx.mock
async def test_first_mention_is_resumed_after_setup(tmp_path: Path) -> None:
    """The mention that triggered setup runs once setup completes — un-dropped."""
    server = FakeOmnigentServer(_SERVER)
    server.install(respx.mock)

    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    pool = OmnigentClientPool()
    # Wired as app.py wires them: SetupFlow built first, then the service
    # taking it as a dependency, then the completion hook attached once both
    # exist so finishing setup can replay the stashed message.
    setup = SetupFlow(store=store, pool=pool, server_url=_SERVER, auth_manager=None)
    service = SlackOmnigentService(store=store, pool=pool, setup=setup, server_url=_SERVER)
    setup.set_completion_hook(service.resume_pending_message)
    client = RecordingSlackClient()

    try:
        # Steps 1-2: the first-ever mention from an unconfigured user is
        # consumed by the setup gate — the bot DMs the setup button and no
        # session exists yet.
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={
                "channel": "C1",
                "ts": "100.1",
                "user": "U1",
                "text": f"<@B1> {_MENTION_TEXT}",
            },
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_turns(service, _WAIT_TIMEOUT_S)
        dm_texts = [
            p.get("text", "") for p in client.posts if str(p.get("channel", "")).startswith("D-")
        ]
        assert any("Set up Omnigent" in t for t in dm_texts), "expected the setup DM"
        assert server.find("POST", "/v1/sessions") is None, (
            "no session may be created before setup completes"
        )

        # Step 3: the user completes setup in the DM modal.
        await setup._handle_select_submit(
            _noop_ack,
            body={"team": {"id": "T1"}, "user": {"id": "U1"}},
            view=_select_view(),
            client=client,
        )
        assert await store.get_user_config("T1", "U1") is not None, (
            "setup must have persisted the user config"
        )

        # Step 4: the original mention must now run WITHOUT being re-sent.
        await _wait_for_resumed_turn(server, service, client)

        assert server.find("POST", "/v1/sessions") is not None, (
            "first-time mention was dropped: setup completed but the message "
            "that triggered it was never resumed — no session was created "
            "until the user re-sends the same mention"
        )
        server.assert_request("POST", "/v1/sessions", json_contains={"agent_id": "ag_1"})
        events = server.find("POST", f"/v1/sessions/{server.session_id}/events")
        assert events is not None, (
            "the resumed turn never submitted the original mention to the session"
        )
        assert _MENTION_TEXT in json.dumps(events[3]), (
            f"the resumed turn must carry the original mention text; got {events[3]!r}"
        )
        assert "Here is the answer." in client.streamed_text, (
            "the resumed turn's answer never streamed back to the user"
        )
    finally:
        await service.shutdown()
        await pool.aclose_all()
