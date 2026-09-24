"""Session ownership scope for the REPL ``/switch`` command."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from omnigent_client import OmnigentClient
from omnigent_ui_sdk.terminal._host import TerminalHost
from rich.table import Table

from omnigent.repl import _repl as repl_mod


@pytest.mark.parametrize("attach_only, visibility", [(False, "mine"), (True, "all")])
@pytest.mark.parametrize("readonly_view", [False, True])
@pytest.mark.parametrize("arg", ["", "1"])
async def test_switch_lists_and_selects_sessions_for_mode(
    monkeypatch: pytest.MonkeyPatch,
    attach_only: bool,
    visibility: str,
    readonly_view: bool,
    arg: str,
) -> None:
    """Both paths retain shared attach targets and restrict runner-owning switches."""
    selected = SimpleNamespace(
        id="conv_shared" if attach_only else "conv_owned",
        title="Selected session",
        status="idle",
        created_at=0,
    )
    list_sessions = AsyncMock(return_value=[selected])
    client = Mock(spec=OmnigentClient)
    client.sessions = SimpleNamespace(list=list_sessions)
    session = repl_mod._SessionsChatReplAdapter(
        client=client,
        agent_name="test-agent",
        attach_only=attach_only,
    )
    session._readonly_view = readonly_view
    switch_session = AsyncMock()
    monkeypatch.setattr(session, "switch_to_session", switch_session)
    attach_conversation = AsyncMock()
    monkeypatch.setattr(repl_mod, "_attach_to_conversation", attach_conversation)
    host = Mock(spec=TerminalHost)
    fmt = Mock(accent="blue", muted="dim")

    await repl_mod._cmd_switch(arg, session, client, host, fmt)

    list_sessions.assert_awaited_once_with(limit=20, visibility=visibility)
    if arg:
        switch_session.assert_awaited_once_with(selected.id)
        host.clear_subagents.assert_called_once_with()
        attach_conversation.assert_awaited_once_with(
            selected.id,
            session,
            client,
            host,
            fmt,
            ui_name=repl_mod._humanize_agent_name(session.model),
            redraw_screen=True,
        )
    else:
        table = host.output.call_args_list[0].args[0]
        assert isinstance(table, Table)
        assert table.columns[1]._cells == [selected.id]
        switch_session.assert_not_awaited()
        attach_conversation.assert_not_awaited()
