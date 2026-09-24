"""End-to-end scenarios across the real HTTP seam.

These drive a REAL :class:`OmnigentClient` (real ``httpx``, real SSE parsing)
against a ``respx`` stand-in for the Omnigent API, with the recording Discord
channel on the other side. That lets each test assert *both* halves of a
scenario: the requests the bot issued (method, path, bearer, JSON body) and what
the user actually saw in Discord.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import respx
from fakes import (
    FakeGuild,
    FakeOmnigentServer,
    FakeUser,
    IncomingMessage,
    RecordingChannel,
    sse_delta,
    sse_status,
)
from omnigent_bot_core.omnigent import OmnigentClientPool
from omnigent_discord.auth_manager import AuthManager
from omnigent_discord.models import UserConfig
from omnigent_discord.notifications import DiscordNotifier
from omnigent_discord.service import DiscordOmnigentService
from omnigent_discord.store import SQLiteStore
from omnigent_discord.tokens import InMemoryTokenStore

BASE = "http://omnigent.test"
BOT_ID = "42"
OWNER = FakeUser("1001")
BOT = FakeUser(BOT_ID, bot=True)
GUILD = FakeGuild("900")
LOGGER = logging.getLogger("test")


class Wired:
    def __init__(
        self,
        service: DiscordOmnigentService,
        server: FakeOmnigentServer,
        store: SQLiteStore,
        dm_channel: RecordingChannel,
    ) -> None:
        self.service = service
        self.server = server
        self.store = store
        self.dm_channel = dm_channel

    async def deliver(self, message: IncomingMessage) -> None:
        import asyncio

        await self.service.handle_message(message)
        while self.service._turn_tasks:
            await asyncio.gather(*list(self.service._turn_tasks), return_exceptions=True)


async def _wire(
    tmp_path: Path,
    router: respx.MockRouter,
    *,
    token: str | None = "delegated-token",
) -> Wired:
    store = SQLiteStore(tmp_path / "bot.sqlite3")
    await store.initialize()
    await store.upsert_user_config(
        str(OWNER.id),
        UserConfig(agent_id="ag_1", agent_name="debby", workspace="/srv/work", host_id="h1"),
    )
    tokens = InMemoryTokenStore()
    await tokens.initialize()
    if token is not None:
        await tokens.put(str(OWNER.id), BASE, access_token=token, refresh_token="refresh")

    pool = OmnigentClientPool()
    auth = AuthManager(tokens, on_token_changed=lambda u, s: pool.invalidate(s, u))
    pool.set_auth_resolver(auth.resolve_auth)

    server = FakeOmnigentServer(BASE).install(router)
    dm_channel = RecordingChannel("700")

    async def dm_resolver(_user_id: str) -> RecordingChannel:
        return dm_channel

    service = DiscordOmnigentService(
        store=store,
        pool=pool,
        notifier=DiscordNotifier(server_url=BASE, logger=LOGGER, dm_resolver=dm_resolver),
        server_url=BASE,
        bot_user_id=BOT_ID,
        stream_edit_interval_seconds=0.0,
        elicitation_timeout_seconds=0.05,
    )
    return Wired(service, server, store, dm_channel)


def _dm(text: str, channel: RecordingChannel | None = None) -> IncomingMessage:
    return IncomingMessage(content=text, author=OWNER, channel=channel or RecordingChannel("600"))


# ── the happy path ────────────────────────────────────────────────────────


async def test_dm_creates_a_session_launches_a_runner_and_streams_the_answer(
    tmp_path: Path,
) -> None:
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        message = _dm("what broke the build?")
        await wired.deliver(message)

    # The server saw a spec-correct session lifecycle …
    wired.server.assert_request("POST", "/v1/sessions", json_contains={"agent_id": "ag_1"})
    wired.server.assert_request(
        "POST", "/v1/hosts/h1/runners", json_contains={"workspace": "/srv/work"}
    )
    submit = wired.server.assert_request("POST", "/v1/sessions/conv_1/events")
    assert submit[3]["data"]["content"][0]["text"] == "what broke the build?"
    assert "/v1/sessions/conv_1/stream" in wired.server.paths("GET")
    # … and the user saw the answer.
    assert "Here is the answer." in message.channel.answer


async def test_every_call_carries_the_users_delegated_bearer(tmp_path: Path) -> None:
    # No Omnigent credential passes through Discord; each user acts as
    # themselves against the server.
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        await wired.deliver(_dm("hello"))

    for method, path in (
        ("POST", "/v1/sessions"),
        ("POST", "/v1/hosts/h1/runners"),
        ("POST", "/v1/sessions/conv_1/events"),
        ("GET", "/v1/sessions/conv_1/stream"),
    ):
        wired.server.assert_bearer(method, path, "delegated-token")


async def test_session_is_mapped_to_the_channel_for_the_next_message(
    tmp_path: Path,
) -> None:
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        channel = RecordingChannel("600")
        await wired.deliver(_dm("first", channel))
        await wired.deliver(_dm("second", channel))

    # One create, one launch, two submits.
    assert wired.server.paths("POST").count("/v1/sessions") == 1
    assert wired.server.paths("POST").count("/v1/hosts/h1/runners") == 1
    assert wired.server.paths("POST").count("/v1/sessions/conv_1/events") == 2


async def test_guild_mention_runs_the_whole_turn_inside_the_new_thread(
    tmp_path: Path,
) -> None:
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        message = IncomingMessage(
            content=f"<@{BOT_ID}> inspect the failure",
            author=OWNER,
            channel=RecordingChannel("500"),
            guild=GUILD,
            mentions=[BOT],
        )
        await wired.deliver(message)

    assert message.thread is not None
    assert message.channel.sent == []
    assert "Here is the answer." in message.thread.answer


# ── recovery paths ────────────────────────────────────────────────────────


async def test_dead_runner_is_relaunched_and_the_turn_retried(tmp_path: Path) -> None:
    # A session whose bound runner died 503s the submit; the client launches a
    # fresh runner and runs the turn once more rather than failing the user.
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        wired.server.first_submit_runner_unavailable = True
        message = _dm("hello")
        await wired.deliver(message)

    assert wired.server.paths("POST").count("/v1/hosts/h1/runners") == 2
    assert "Here is the answer." in message.channel.answer


async def test_severed_stream_reconnects_and_the_answer_is_not_duplicated(
    tmp_path: Path,
) -> None:
    # A proxy duration cap severs the long-lived response mid-turn; on re-open
    # the server replays the streamed-so-far text as one cumulative delta.
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        wired.server.stream_legs = [
            sse_status("running", "r1") + sse_delta("Hello ", "m1") + "<DROP>",
            sse_delta("Hello world.", "m1") + sse_status("idle", "r1"),
        ]
        message = _dm("hello")
        await wired.deliver(message)

    assert "Hello world." in message.channel.answer
    assert message.channel.answer.count("Hello") == 1


async def test_offline_host_tells_the_user_how_to_start_one(tmp_path: Path) -> None:
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        wired.server.launch_status = 409
        message = _dm("hello")
        await wired.deliver(message)

    assert f"omni host --server {BASE}" in message.channel.texts[-1]


async def test_unconfigured_harness_shows_the_servers_curated_guidance(
    tmp_path: Path,
) -> None:
    # The one server-authored message safe to surface: it names the fix.
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        wired.server.harness_not_configured_message = "Run `omnigent setup` on the host."
        message = _dm("hello")
        await wired.deliver(message)

    assert "omnigent setup" in " ".join(message.channel.texts)


async def test_expired_token_asks_the_user_to_sign_in_again(tmp_path: Path) -> None:
    # No token at all is the same shape as a grant that can no longer refresh.
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router, token=None)
        wired.server.auth_required = True
        message = _dm("hello")
        await wired.deliver(message)

    assert message.channel.live == []
    assert "login has expired" in wired.dm_channel.texts[-1]


async def test_committed_only_answer_is_recovered_from_the_items_endpoint(
    tmp_path: Path,
) -> None:
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        wired.server.sse_body = sse_status("running", "r1") + sse_status("idle", "r1")
        wired.server.latest_items = [
            {
                "id": "item_9",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "The committed answer."}],
            }
        ]
        message = _dm("hello")
        await wired.deliver(message)

    assert "The committed answer." in message.channel.answer


# ── elicitations over the wire ────────────────────────────────────────────


async def test_approval_card_is_declined_at_turn_end_over_the_real_endpoint(
    tmp_path: Path,
) -> None:
    # An unanswered park would otherwise wedge the session for every later
    # message in the channel.
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        wired.server.sse_body = (
            sse_status("running", "r1")
            + 'data: {"type":"response.elicitation_request","elicitation_id":"el_1",'
            '"params":{"message":"Approve?"}}\n\n' + sse_status("idle", "r1")
        )
        message = _dm("do it")
        await wired.deliver(message)

    resolve = wired.server.assert_request("POST", "/v1/sessions/conv_1/elicitations/el_1/resolve")
    assert resolve[3] == {"action": "decline"}
    assert any(m.embed is not None for m in message.channel.live)


@pytest.mark.parametrize("mode", ["form", "url"])
async def test_url_mode_approval_is_still_rendered_natively(tmp_path: Path, mode: str) -> None:
    # Classification is by decision SHAPE, not the server's delivery mode: a
    # url-mode binary approval is still an Approve/Deny the bot can collect.
    async with respx.mock(assert_all_called=False) as router:
        wired = await _wire(tmp_path, router)
        wired.server.sse_body = (
            sse_status("running", "r1")
            + 'data: {"type":"response.elicitation_request","elicitation_id":"el_1",'
            f'"params":{{"message":"Approve?","mode":"{mode}"}}}}\n\n' + sse_status("idle", "r1")
        )
        message = _dm("do it")
        await wired.deliver(message)

    # Rendered as a card in the channel, not deflected to a web-UI link.
    assert len([m for m in message.channel.live if m.embed is not None]) == 1
    assert not any("/approve/" in (m.content or "") for m in message.channel.live)
