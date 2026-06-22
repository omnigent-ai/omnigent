from __future__ import annotations

import pytest
from omnigent_ui_sdk import RichBlockFormatter

from omnigent.repl._repl import COMMANDS, handle_slash_command
from tests.unit.repl.helpers import CapturingHost

_Host = CapturingHost


class _Session:
    def __init__(self) -> None:
        self.reasoning_effort: str | None = None
        self.is_streaming = False

    def set_reasoning_effort(self, effort: str | None) -> None:
        self.reasoning_effort = effort


class _AsyncSession(_Session):
    async def set_reasoning_effort(self, effort: str | None) -> None:
        self.reasoning_effort = effort


@pytest.mark.asyncio
async def test_effort_command_registered() -> None:
    assert "/effort" in COMMANDS
    assert "lists options" in COMMANDS["/effort"][0]


@pytest.mark.asyncio
async def test_effort_show_default_and_options() -> None:
    host = _Host()
    session = _Session()
    await handle_slash_command("/effort", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]
    text = host.text
    assert "reasoning effort: default" in text
    assert "none" in text and "minimal" in text and "xhigh" in text and "max" in text
    assert "default" in text


@pytest.mark.asyncio
async def test_effort_show_current_override_and_options() -> None:
    host = _Host()
    session = _Session()
    session.reasoning_effort = "xhigh"
    await handle_slash_command("/effort", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]
    text = host.text
    assert "reasoning effort: xhigh" in text
    assert "xhigh" in text


@pytest.mark.asyncio
async def test_effort_sets_valid_value() -> None:
    host = _Host()
    session = _Session()
    await handle_slash_command("/effort high", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]
    assert session.reasoning_effort == "high"
    assert "future responses" in host.text


@pytest.mark.asyncio
async def test_effort_awaits_async_sessions_adapter_setter() -> None:
    host = _Host()
    session = _AsyncSession()
    await handle_slash_command("/effort high", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]
    assert session.reasoning_effort == "high"
    assert "future responses" in host.text


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["default", "off", "reset"])
async def test_effort_default_aliases_clear(alias: str) -> None:
    host = _Host()
    session = _Session()
    session.reasoning_effort = "high"
    await handle_slash_command(f"/effort {alias}", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]
    assert session.reasoning_effort is None
    assert "agent default" in host.text


@pytest.mark.asyncio
async def test_effort_invalid_value_does_not_mutate() -> None:
    host = _Host()
    session = _Session()
    session.reasoning_effort = "medium"
    await handle_slash_command("/effort extreme", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]
    assert session.reasoning_effort == "medium"
    assert "Invalid effort" in host.text


@pytest.mark.asyncio
async def test_effort_mentions_current_response_unchanged_when_streaming() -> None:
    host = _Host()
    session = _Session()
    session.is_streaming = True
    await handle_slash_command("/effort low", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]
    assert session.reasoning_effort == "low"
    assert "current response unchanged" in host.text
