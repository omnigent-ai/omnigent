"""REPL agent lookup on a remote server: an unknown agent name is reported safely."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.repl._repl import _SessionsChatReplAdapter


@pytest.mark.asyncio
async def test_remote_repl_unknown_agent_error_is_escaped() -> None:
    """A missing agent name keeps its LookupError, with control characters escaped."""

    async def _resolve_agent(name: str) -> object:
        raise LookupError(f"No agent named {name!r} is registered on this server.")

    adapter = _SessionsChatReplAdapter(
        SimpleNamespace(sessions=SimpleNamespace(resolve_agent=_resolve_agent)),  # type: ignore[arg-type]
        "evil\x1b[2J",
    )

    # The REPL renders exceptions at its UI boundary; keep the LookupError contract.
    with pytest.raises(LookupError) as excinfo:
        await adapter._create_session_from_registered_agent(pending_debug=False)

    assert "\x1b" not in str(excinfo.value) and "\\x1b" in str(excinfo.value)
