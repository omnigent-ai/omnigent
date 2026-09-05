"""Edge cases of the runner's composed harness stream-failure message.

When ``proxy_stream``'s harness stream dies mid-turn, the runner composes the
user-facing ``response.failed`` message from the real transport cause plus a
bounded live-terminal pane snapshot (a ``Last captured terminal output:``
block the web UI renders as diagnostics). These tests pin the composition's
edges at the runner's HTTP boundary (``POST /v1/sessions/{id}/events``):

- a cause-less exception keeps the plain headline (no dangling colon) while
  the pane snapshot still attaches;
- a blank pane snapshot attaches no diagnostics block;
- a pane read that raises never masks the failure event itself;
- an oversized pane snapshot is bounded by the shared trim budget.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

_CONV_ID = "ac1dbeef245985f541fd686eb2a32b73"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"


class _StreamErrorHarnessClient(_ScriptedHarnessClient):
    """Harness client that emits its scripted frames, then drops mid-stream."""

    def __init__(self, sse_frames: list[str], *, cause: str) -> None:
        super().__init__(sse_frames)
        self._cause = cause

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Return a context manager whose stream errors after the frames."""
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames
        cause = self._cause

        class _ErrCtx:
            status_code = 200

            async def __aenter__(self) -> _StreamErrorHarnessClient._ErrHandle:
                return _StreamErrorHarnessClient._ErrHandle(frames, cause)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _ErrCtx()

    class _ErrHandle:
        """Stream handle that raises ``ReadError`` after yielding its frames."""

        status_code = 200

        def __init__(self, frames: list[str], cause: str) -> None:
            self._frames = frames
            self._cause = cause

        async def aiter_text(self) -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame
            raise httpx.ReadError(self._cause)


def _make_app(*, cause: str, terminal_registry: TerminalRegistry | None = None) -> Any:
    """Build a runner app whose harness stream drops with *cause* mid-turn."""
    harness_client = _StreamErrorHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_drop"}})],
        cause=cause,
    )
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )


def _register_terminal(registry: TerminalRegistry, conv_id: str, instance: Any) -> None:
    """Seed a live instance for *conv_id* (private-attr test convention, no tmux)."""
    registry._by_conversation.setdefault(conv_id, {})[("bash", "main")] = instance


async def _failed_event_message(app: Any, conv_id: str) -> tuple[dict[str, Any], str]:
    """Drive one failing streamed turn; return (failed event, error message)."""
    transport = httpx.ASGITransport(app=app)
    events: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        async with client.stream(
            "POST",
            f"/v1/sessions/{conv_id}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": _AGENT_ID,
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        ) as resp:
            assert resp.status_code == 200, resp.status_code
            buf = ""
            with contextlib.suppress(Exception):
                async for chunk in resp.aiter_text():
                    buf += chunk
            for block in buf.split("\n\n"):
                for line in block.strip().splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        with contextlib.suppress(json.JSONDecodeError):
                            events.append(json.loads(line[len("data:") :].strip()))
    failed = [e for e in events if e.get("type") == "response.failed"]
    assert len(failed) == 1, f"expected exactly one response.failed event, got {events}"
    message = failed[0].get("error", {}).get("message", "")
    assert isinstance(message, str)
    return failed[0], message


@pytest.mark.asyncio
async def test_causeless_failure_keeps_plain_headline_but_attaches_pane(
    tmp_path: Path,
) -> None:
    """An exception with empty text keeps the period headline, pane still attached."""
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "main", tmp_path)
    instance._remember_pane_snapshot("Trust this folder?\n> ")
    _register_terminal(registry, _CONV_ID, instance)

    app = _make_app(cause="", terminal_registry=registry)
    _, message = await _failed_event_message(app, _CONV_ID)

    first_line = message.splitlines()[0]
    # No cause to report: the headline stays the plain sentence, never "error: ".
    assert first_line == "Harness stream connection error.", message
    # The live pane is still the most diagnostic thing available -- attached.
    assert "Last captured terminal output:" in message, message
    assert "Trust this folder?" in message, message


@pytest.mark.asyncio
async def test_blank_pane_snapshot_attaches_no_diagnostics_block(tmp_path: Path) -> None:
    """A whitespace-only pane adds no block; the cause still reaches the user."""
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "main", tmp_path)
    instance._remember_pane_snapshot("   \n\t\n ")
    _register_terminal(registry, _CONV_ID, instance)

    app = _make_app(cause="ReadError(ClosedResourceError())", terminal_registry=registry)
    failed, message = await _failed_event_message(app, _CONV_ID)

    assert failed["error"].get("code") == "connection_error", failed
    assert "ReadError(ClosedResourceError())" in message, message
    assert "last captured" not in message.lower(), message


@pytest.mark.asyncio
async def test_pane_read_error_does_not_mask_the_failure_event(tmp_path: Path) -> None:
    """A raising pane read is swallowed; the failure event still carries the cause."""
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "main", tmp_path)

    def _boom() -> str:
        raise RuntimeError("tmux went away")

    instance.last_pane_text = _boom  # type: ignore[method-assign]
    _register_terminal(registry, _CONV_ID, instance)

    app = _make_app(cause="stream dropped mid-turn", terminal_registry=registry)
    failed, message = await _failed_event_message(app, _CONV_ID)

    assert failed["error"].get("code") == "connection_error", failed
    assert "stream dropped mid-turn" in message, message
    assert "last captured" not in message.lower(), message


@pytest.mark.asyncio
async def test_oversized_pane_snapshot_is_bounded(tmp_path: Path) -> None:
    """A huge pane is trimmed to the shared diagnostics budget, tail preserved."""
    registry = TerminalRegistry()
    instance = make_test_terminal_instance("bash", "main", tmp_path)
    filler = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    instance._remember_pane_snapshot(filler + "\nfinal prompt line")
    _register_terminal(registry, _CONV_ID, instance)

    app = _make_app(cause="stream dropped mid-turn", terminal_registry=registry)
    _, message = await _failed_event_message(app, _CONV_ID)

    assert "Last captured terminal output:" in message, message
    _, _, pane_block = message.partition("Last captured terminal output:\n")
    assert pane_block.startswith("... omitted "), pane_block[:120]
    assert pane_block.endswith("final prompt line"), pane_block[-120:]
    # Bounded by the shared trim budget (40 lines / 4000 chars + marker slack).
    assert len(pane_block) <= 4100, len(pane_block)
