from __future__ import annotations

import pytest
from omnigent_ui_sdk import RichBlockFormatter

from omnigent.repl._repl import COMMANDS, handle_slash_command
from tests.repl.helpers import CapturingHost


class _Session:
    """Fake session that records every ``set_model_override`` call.

    ``override_calls`` stays empty unless the command actually persists a
    model id, which lets the display-keyword tests prove ``/model show`` &
    friends never route through a switch.
    """

    def __init__(self) -> None:
        self.model_override: str | None = None
        self.is_streaming = False
        self.override_calls: list[str | None] = []

    def set_model_override(self, target: str | None) -> None:
        self.override_calls.append(target)
        self.model_override = target


@pytest.mark.asyncio
async def test_model_command_registered() -> None:
    assert "/model" in COMMANDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keyword",
    ["show", "list", "status", "current", "SHOW", "List", "Status", "CURRENT"],
)
async def test_model_show_keywords_display_not_switch(keyword: str) -> None:
    host = CapturingHost()
    session = _Session()
    await handle_slash_command(
        f"/model {keyword}",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    # Display keyword must NOT be persisted as a model id.
    assert session.override_calls == []
    assert session.model_override is None
    # It must emit the same active-credential readout as bare `/model`.
    assert "Active:" in host.text


@pytest.mark.asyncio
async def test_model_no_arg_shows_readout() -> None:
    host = CapturingHost()
    session = _Session()
    await handle_slash_command("/model", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]
    assert session.override_calls == []
    assert "Active:" in host.text


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["default", "off", "reset"])
async def test_model_default_aliases_clear(alias: str) -> None:
    host = CapturingHost()
    session = _Session()
    session.model_override = "claude-sonnet-4-6"
    await handle_slash_command(
        f"/model {alias}",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    assert session.override_calls == [None]
    assert session.model_override is None
    assert "reset to agent default" in host.text


@pytest.mark.asyncio
async def test_model_sets_valid_id() -> None:
    host = CapturingHost()
    session = _Session()
    await handle_slash_command(
        "/model claude-sonnet-4-6",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    assert session.override_calls == ["claude-sonnet-4-6"]
    assert session.model_override == "claude-sonnet-4-6"
    assert "for future responses" in host.text
