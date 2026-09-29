"""A managed sandbox that is still provisioning must surface as such in Slack.

Journey (the real user path): a user configured for a server-provisioned
(managed) sandbox @mentions the bot in a channel. The bot creates the managed
session and submits the first message while the server is still provisioning
the session's sandbox; the server answers ``503 runner_unavailable`` with its
curated "still provisioning; try again shortly" reason. The user must see the
bot-composed, cause-neutral "sandbox isn't ready yet — try again" notice (the
same 503 also covers a failed sandbox launch, so it must not promise the
sandbox is merely starting), not the generic "Something went wrong" failure —
and the server's raw reason must never be echoed into the channel.

Same vertical harness as ``test_integration.py``: the REAL
``SlackOmnigentService`` and ``OmnigentClient`` (real ``httpx``) against
:class:`FakeOmnigentServer` (the ``respx`` router that owns the API contract),
with :class:`RecordingSlackClient` recording exactly what the user is shown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import respx
from fakes import FakeOmnigentServer, RecordingSlackClient
from omnigent_slack.models import UserConfig
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.service import _MANAGED_SANDBOX_NOT_READY_TEXT, SlackOmnigentService
from omnigent_slack.store import SQLiteStore
from omnigent_slack.text import GENERIC_FAILURE_TEXT

_SERVER = "http://omnigent.test"

# The server's curated reason when a message POST outlives the managed-launch
# rendezvous window (see ``_await_settled_managed_launch`` in
# ``omnigent/server/routes/_sessions/helpers.py``).
_PROVISIONING_REASON = "The session's managed sandbox is still provisioning; try again shortly"

# Generous ceiling for the turn wait below — event-driven, so a healthy run
# returns near-instantly; this only bounds a genuine hang.
_WAIT_TIMEOUT_S = 10.0


async def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    return store


async def _configure_managed_user(store: SQLiteStore, team_id: str, user_id: str) -> None:
    """Configure a user whose sessions run in a server-provisioned sandbox."""
    await store.upsert_user_config(
        team_id,
        user_id,
        UserConfig(agent_id="ag_1", agent_name="debby", workspace="", host_type="managed"),
    )


async def _wait_for_turns(service: SlackOmnigentService, timeout: float = _WAIT_TIMEOUT_S) -> None:
    """Wait until the service's spawned turn tasks have finished (event-driven)."""
    tasks = list(service._turn_tasks)
    if not tasks:
        return
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)


class _NoopSetup:
    """SetupFlow stand-in for turns where the user is already configured."""

    async def prompt_unconfigured(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("configured user should not be prompted to set up")

    async def prompt_relogin(self, *args: object, **kwargs: object) -> bool:
        return True


@respx.mock
async def test_managed_still_provisioning_surfaces_curated_notice(tmp_path: Path) -> None:
    """The still-provisioning 503 must yield a recoverable notice, not the
    generic failure."""
    server = FakeOmnigentServer(_SERVER)
    server.managed_sandboxes_enabled = True
    server.sandbox_provider = "modal"
    # Every submit 503s runner_unavailable: the sandbox launch never settles
    # within the rendezvous window while this turn runs.
    server.submit_runner_unavailable_message = _PROVISIONING_REASON
    server.install(respx.mock)

    store = await _store(tmp_path)
    await _configure_managed_user(store, "T1", "U1")
    pool = OmnigentClientPool()
    service = SlackOmnigentService(store=store, pool=pool, setup=_NoopSetup(), server_url=_SERVER)
    client = RecordingSlackClient()

    try:
        await service.handle_app_mention(
            body={"team_id": "T1", "event_id": "Ev1"},
            event={"channel": "C1", "ts": "100.1", "user": "U1", "text": "<@B1> review this"},
            client=client,
            context={"bot_user_id": "B1"},
        )
        await _wait_for_turns(service)
    finally:
        await service.shutdown()
        await pool.aclose_all()

    # Server side: the managed create ran (no host id / workspace), the message
    # was submitted (and 503'd), and no runner launch was ever posted — the
    # server owns a managed session's sandbox, so there is no host to launch on.
    create = server.assert_request(
        "POST", "/v1/sessions", json_contains={"agent_id": "ag_1", "host_type": "managed"}
    )
    assert "host_id" not in create[3]
    assert f"/v1/sessions/{server.session_id}/events" in server.paths("POST")
    assert not any(path.endswith("/runners") for path in server.paths("POST"))

    # Slack side: everything the user was shown in the thread.
    surfaced = " ".join(p.get("text", "") for p in client.posts)
    for stream in client.streams:
        surfaced += " " + stream.text
    surfaced = surfaced.strip()
    assert surfaced, "the failed turn must deliver a user-facing notice"

    # The not-ready sandbox must NOT read as a server failure...
    assert GENERIC_FAILURE_TEXT not in surfaced, (
        f"a still-provisioning managed sandbox surfaced as the generic failure: {surfaced!r}"
    )
    # ...the user must see exactly the bot-composed, cause-neutral notice...
    assert _MANAGED_SANDBOX_NOT_READY_TEXT in surfaced, (
        f"notice does not communicate the not-ready sandbox state: {surfaced!r}"
    )
    # ...and the server's raw curated reason must never be echoed into the
    # channel (the "server error bodies are never echoed" rule in DESIGN.md).
    assert _PROVISIONING_REASON not in surfaced, (
        f"the server's raw 503 reason leaked into the channel: {surfaced!r}"
    )
