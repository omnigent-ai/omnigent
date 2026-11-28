"""The operator switch decides whether an attached file is sent at all.

The attachment policy itself is covered in ``test_attachments``; this file is
about the gate in front of it, which is what makes uploads off by default.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from fakes import (
    FakeOmnigent,
    FakePool,
    FakeUser,
    IncomingMessage,
    RecordingChannel,
    delta_event,
    status_event,
)
from omnigent_discord.models import UserConfig
from omnigent_discord.notifications import DiscordNotifier
from omnigent_discord.service import DiscordOmnigentService
from omnigent_discord.store import SQLiteStore

pytestmark = pytest.mark.asyncio

SERVER = "https://omnigent.example"
BOT_ID = "42"
LOGGER = logging.getLogger("test")
OWNER = FakeUser("900000000000000001", name="owner")
ANSWER_STREAM = [
    status_event("running", "resp_1"),
    delta_event("Here is the answer.", "m1"),
    status_event("idle", "resp_1"),
]


class FakeAttachment:
    def __init__(self, filename: str, payload: bytes = b"pixels") -> None:
        self.filename = filename
        self.payload = payload
        self.size = len(payload)
        self.saved = False

    async def save(self, destination: Any, **_kwargs: Any) -> None:
        self.saved = True
        Path(destination).write_bytes(self.payload)


async def _build(
    tmp_path: Path,
    *,
    operator_allows: bool,
) -> tuple[DiscordOmnigentService, FakeOmnigent, RecordingChannel]:
    store = SQLiteStore(tmp_path / "bot.sqlite3")
    await store.initialize()
    await store.upsert_user_config(
        str(OWNER.id),
        UserConfig(
            agent_id="ag_1",
            agent_name="debby",
            workspace="/srv/work",
            host_id="h1",
        ),
    )
    client = FakeOmnigent(list(ANSWER_STREAM))
    dm_channel = RecordingChannel("700")

    async def dm_resolver(_user_id: str) -> RecordingChannel:
        return dm_channel

    service = DiscordOmnigentService(
        store=store,
        pool=FakePool(client),  # type: ignore[arg-type]
        notifier=DiscordNotifier(server_url=SERVER, logger=LOGGER, dm_resolver=dm_resolver),
        server_url=SERVER,
        bot_user_id=BOT_ID,
        stream_edit_interval_seconds=0.0,
        elicitation_timeout_seconds=0.05,
        allow_file_upload=operator_allows,
    )
    return service, client, dm_channel


async def _send(service: DiscordOmnigentService, channel: RecordingChannel, text: str) -> None:
    message = IncomingMessage(
        content=text,
        author=OWNER,
        channel=channel,
        attachments=[FakeAttachment("avatar.png")],
    )
    await service.handle_message(message)
    while service._turn_tasks:
        await asyncio.gather(*list(service._turn_tasks), return_exceptions=True)


async def test_the_operator_switch_on_sends_the_file(tmp_path: Path) -> None:
    service, client, _ = await _build(tmp_path, operator_allows=True)
    await _send(service, RecordingChannel("600"), "what is this?")
    assert client.submitted_blocks
    (blocks,) = client.submitted_blocks
    assert [block["filename"] for block in blocks] == ["avatar.png"]
    assert blocks[0]["type"] == "input_image"


async def test_the_operator_switch_off_sends_nothing(tmp_path: Path) -> None:
    # The default on a fresh install.
    service, client, _ = await _build(tmp_path, operator_allows=False)
    await _send(service, RecordingChannel("601"), "what is this?")
    assert client.submitted_blocks == [[]]


async def test_a_refused_file_never_downloads(tmp_path: Path) -> None:
    service, _, _ = await _build(tmp_path, operator_allows=False)
    channel = RecordingChannel("602")
    message = IncomingMessage(
        content="what is this?",
        author=OWNER,
        channel=channel,
        attachments=[FakeAttachment("avatar.png")],
    )
    await service.handle_message(message)
    while service._turn_tasks:
        await asyncio.gather(*list(service._turn_tasks), return_exceptions=True)
    assert message.attachments[0].saved is False


async def test_the_turn_still_runs_when_the_file_is_refused(tmp_path: Path) -> None:
    # A refused attachment must not swallow the message it came with.
    service, client, _ = await _build(tmp_path, operator_allows=False)
    await _send(service, RecordingChannel("603"), "what is this?")
    assert client.submitted == ["what is this?"]
