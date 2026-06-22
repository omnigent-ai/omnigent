"""Tests for ``_SessionsChatReplAdapter._ensure_session`` model handoff.

The user can type ``/model foo`` before sending the first message —
which is before the session row exists. The adapter caches the value
locally, then PATCHes it onto the session immediately after
``POST /v1/sessions`` returns so the first event's workflow already
sees ``conv.model_override``. These tests pin that handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl._repl import _SessionsChatReplAdapter

pytestmark = pytest.mark.asyncio


@dataclass
class _StubSession:
    """Minimal Session-shaped dataclass for snapshot returns."""

    id: str
    agent_id: str
    runner_id: str | None
    reasoning_effort: str | None
    model_override: str | None
    # None mirrors an old server that omits agent_name — hydrate keeps
    # the adapter's launch-time name (these tests don't exercise the
    # agent-switch rename path).
    agent_name: str | None = None
    llm_model: str | None = None
    context_window: int | None = None
    last_total_tokens: int | None = None
    harness: str | None = None


def _build_adapter(create_returns: _StubSession) -> _SessionsChatReplAdapter:
    """
    Build an adapter wired to mock client + bundle, ready for ensure_session.

    :param create_returns: The Session the mocked ``sessions.create``
        returns. Use a populated ``model_override`` to simulate a
        resumed session, ``None`` to simulate a fresh one.
    :returns: An adapter with mocked client + stubbed bind/recovery so
        ``_ensure_session`` can run end-to-end without HTTP.
    """
    client = MagicMock()
    client.sessions.create = AsyncMock(return_value=create_returns)
    # set_model_override returns the canonical session post-PATCH;
    # mirror the input value so the test can also assert the local
    # cache picks up the server-canonical form.
    # ``silent`` ignored by the stub (server-side responsibility) but
    # accepted as a kwarg so the adapter's call signature is honoured.
    client.sessions.set_model_override = AsyncMock(
        side_effect=lambda session_id, *, model_override, silent=False: _StubSession(
            id=session_id,
            agent_id=create_returns.agent_id,
            runner_id=create_returns.runner_id,
            reasoning_effort=create_returns.reasoning_effort,
            model_override=model_override,
        )
    )

    adapter = _SessionsChatReplAdapter(
        client=client,
        agent_name="test-agent",
        session_bundle=b"fake-bundle",
        session_bundle_filename="agent.tar.gz",
    )
    # Short-circuit the runner-bind and stream-pump steps so
    # _ensure_session reaches the model-override branch without
    # needing live HTTP / asyncio plumbing.
    adapter._bind_runner_if_needed = AsyncMock(return_value=None)  # type: ignore[method-assign]
    adapter._recover_runner_if_needed = AsyncMock(return_value=None)  # type: ignore[method-assign]
    adapter._stream_pump = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return adapter


async def test_pending_model_override_patched_after_create() -> None:
    """``/model`` typed before the first send PATCHes after create."""
    fresh = _StubSession(
        id="conv_new",
        agent_id="ag_x",
        runner_id=None,
        reasoning_effort=None,
        model_override=None,
    )
    adapter = _build_adapter(create_returns=fresh)

    # Simulate ``/model claude-opus-4-7`` typed before any send.
    await adapter.set_model_override("claude-opus-4-7")
    assert adapter.model_override == "claude-opus-4-7"

    await adapter._ensure_session()

    # The create POST does not carry model_override (no metadata
    # field), so without the post-create PATCH the first event would
    # silently fall back to the spec default. ``silent=True`` skips
    # the tmux ``/model`` forward; without it a fresh session would
    # render a leading "Command model X" item before any user input.
    adapter._client.sessions.set_model_override.assert_awaited_once_with(  # type: ignore[attr-defined]
        "conv_new",
        model_override="claude-opus-4-7",
        silent=True,
    )
    # Local cache reflects the server-canonical value.
    assert adapter.model_override == "claude-opus-4-7"


async def test_no_patch_when_server_already_has_override() -> None:
    """Resume path: server returned an override, no double-PATCH."""
    resumed = _StubSession(
        id="conv_resume",
        agent_id="ag_x",
        runner_id=None,
        reasoning_effort=None,
        model_override="claude-sonnet-4-6",
    )
    adapter = _build_adapter(create_returns=resumed)
    # User has no pending pick — nothing to hand off.
    assert adapter.model_override is None

    await adapter._ensure_session()

    adapter._client.sessions.set_model_override.assert_not_awaited()  # type: ignore[attr-defined]
    # Hydration picked up the server-side override.
    assert adapter.model_override == "claude-sonnet-4-6"


async def test_no_patch_when_no_pending_override() -> None:
    """Fresh session with no /model typed → no PATCH."""
    fresh = _StubSession(
        id="conv_blank",
        agent_id="ag_x",
        runner_id=None,
        reasoning_effort=None,
        model_override=None,
    )
    adapter = _build_adapter(create_returns=fresh)

    await adapter._ensure_session()

    adapter._client.sessions.set_model_override.assert_not_awaited()  # type: ignore[attr-defined]
    assert adapter.model_override is None


async def test_patch_failure_logs_and_clears_local_cache() -> None:
    """A server-rejected pending override fails safe.

    Without the try/except guard, a PATCH failure would propagate out
    of ``_ensure_session`` and surface as an opaque error on the
    user's send. The guard logs + clears the local cache so the next
    send goes through with the spec default instead.
    """
    fresh = _StubSession(
        id="conv_reject",
        agent_id="ag_x",
        runner_id=None,
        reasoning_effort=None,
        model_override=None,
    )
    adapter = _build_adapter(create_returns=fresh)
    adapter._client.sessions.set_model_override = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("validator rejected model name"),
    )
    await adapter.set_model_override("not-a-real-model")

    # Should NOT raise; failure is handled internally.
    await adapter._ensure_session()

    # Local cache cleared so the user's next send doesn't keep trying
    # the rejected value.
    assert adapter.model_override is None


async def test_resume_path_skips_patch_even_when_pending_local_value() -> None:
    """If both server AND local have a value, server wins, no PATCH.

    Resume scenario: the user previously persisted a model override
    on this session; locally the picker remembers the same (or a
    different) value. The adapter's hydrate clobbers the local cache
    with the server snapshot, and the post-create PATCH branch is
    gated on ``session.model_override is None`` so it does not fire.
    """
    resumed = _StubSession(
        id="conv_both",
        agent_id="ag_x",
        runner_id=None,
        reasoning_effort=None,
        model_override="claude-sonnet-4-6",
    )
    adapter = _build_adapter(create_returns=resumed)
    await adapter.set_model_override("claude-opus-4-7")
    assert adapter.model_override == "claude-opus-4-7"

    await adapter._ensure_session()

    adapter._client.sessions.set_model_override.assert_not_awaited()  # type: ignore[attr-defined]
    # Hydration replaced the local pick with the server's
    # authoritative value.
    assert adapter.model_override == "claude-sonnet-4-6"
