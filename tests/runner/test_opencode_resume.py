"""Tests for opencode-native resume helpers (transcript render + rehydration)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.runner.app as app
from omnigent.harnesses.opencode_native.client import OpenCodeClientError, OpenCodeSession
from omnigent.harnesses.opencode_native.provider import ASK_ALL_PERMISSIONS
from omnigent.runner.native.orchestration import (
    _OpenCodeNativeLaunchConfig,
    _resolve_opencode_session,
)

_ITEMS: list[dict[str, Any]] = [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yo"}]},
]


class _Resp:
    def __init__(self, data: list[dict[str, Any]]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"data": self._data}


class _FakeServerClient:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    async def get(self, url: str, **kwargs: Any) -> _Resp:
        return _Resp(self._items)


class _FakeOpenCodeClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.seeded: tuple[str, str] | None = None
        self._error = error

    async def seed_context(self, session_id: str, text: str) -> None:
        if self._error is not None:
            raise self._error
        self.seeded = (session_id, text)


# ── _render_opencode_transcript_text ────────────────────────────────────────


def test_render_transcript_extracts_user_assistant_text() -> None:
    assert app._render_opencode_transcript_text(_ITEMS) == "User: hi\n\nAssistant: yo"


def test_render_transcript_skips_non_message_and_other_roles() -> None:
    items = [
        {"type": "reasoning", "text": "ignored"},
        {"type": "message", "role": "tool", "content": [{"text": "ignored"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    ]
    assert app._render_opencode_transcript_text(items) == "User: hi"


# ── _rehydrate_opencode_session_from_transcript ─────────────────────────────


async def test_rehydrate_seeds_transcript_without_model() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=_FakeServerClient(_ITEMS),
    )
    assert ok is True
    assert oc.seeded is not None
    session_id, text = oc.seeded
    assert session_id == "ses_1"
    assert text.startswith("[Resumed session")
    assert "User: hi" in text and "Assistant: yo" in text


async def test_rehydrate_no_server_client_returns_false() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=None,
    )
    assert ok is False
    assert oc.seeded is None


async def test_rehydrate_empty_transcript_returns_false() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=_FakeServerClient([]),
    )
    assert ok is False
    assert oc.seeded is None


async def test_rehydrate_seed_failure_returns_false() -> None:
    oc = _FakeOpenCodeClient(error=OpenCodeClientError("synthetic failed: 500"))
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=_FakeServerClient(_ITEMS),
    )
    assert ok is False


# ── _resolve_opencode_session ───────────────────────────────────────────────


class _FakeSessionClient:
    """OpenCode client stub for the session-resolution paths."""

    def __init__(
        self,
        *,
        existing: str | None = None,
        fork_error: Exception | None = None,
    ) -> None:
        self._existing = existing
        self._fork_error = fork_error
        self.created: list[dict[str, Any]] = []
        self.forked: list[tuple[str, str | None]] = []
        self.seeded: list[tuple[str, str]] = []

    async def get_session(self, session_id: str) -> OpenCodeSession | None:
        return OpenCodeSession(id=session_id) if session_id == self._existing else None

    async def create_session(self, **kwargs: Any) -> OpenCodeSession:
        self.created.append(kwargs)
        return OpenCodeSession(id="ses_new")

    async def fork(self, session_id: str, *, before: str | None = None) -> OpenCodeSession:
        if self._fork_error is not None:
            raise self._fork_error
        self.forked.append((session_id, before))
        return OpenCodeSession(id="ses_forked")

    async def seed_context(self, session_id: str, text: str) -> None:
        self.seeded.append((session_id, text))


def _config(**overrides: Any) -> _OpenCodeNativeLaunchConfig:
    values: dict[str, Any] = {
        "workspace": Path("/repo"),
        "policy_server_url": "http://127.0.0.1:8123",
        "terminal_launch_args": None,
        "model_override": None,
        "external_session_id": None,
    }
    values.update(overrides)
    return _OpenCodeNativeLaunchConfig(**values)


async def _resolve(
    client: _FakeSessionClient,
    config: _OpenCodeNativeLaunchConfig,
    *,
    fork_source_session_id: str | None = None,
    fresh: bool = False,
) -> str:
    return await _resolve_opencode_session(
        client=client,  # type: ignore[arg-type]
        launch_config=config,
        omnigent_session_id="conv_1",
        workspace="/repo",
        server_client=_FakeServerClient(_ITEMS),  # type: ignore[arg-type]
        fork_source_session_id=fork_source_session_id,
        fresh=fresh,
    )


async def test_resolve_resumes_existing_session() -> None:
    client = _FakeSessionClient(existing="ses_old")
    assert await _resolve(client, _config(external_session_id="ses_old")) == "ses_old"
    assert client.created == [] and client.seeded == []


async def test_resolve_lost_session_creates_and_seeds() -> None:
    client = _FakeSessionClient()
    assert await _resolve(client, _config(external_session_id="ses_gone")) == "ses_new"
    assert client.created == [
        {
            "title": "omnigent:conv_1",
            "directory": "/repo",
            "permissions": ASK_ALL_PERMISSIONS,
            "metadata": {"omnigent_conversation": "conv_1"},
        }
    ]
    assert [sid for sid, _ in client.seeded] == ["ses_new"]


async def test_resolve_new_session_is_not_seeded() -> None:
    client = _FakeSessionClient()
    assert await _resolve(client, _config()) == "ses_new"
    assert client.seeded == []


async def test_resolve_native_fork() -> None:
    client = _FakeSessionClient()
    session_id = await _resolve(
        client, _config(fork_carry_history=True), fork_source_session_id="ses_src"
    )
    assert session_id == "ses_forked"
    assert client.forked == [("ses_src", None)]
    assert client.created == [] and client.seeded == []


@pytest.mark.parametrize(
    "error",
    [OpenCodeClientError("fork failed: 404"), httpx.ConnectError("refused")],
)
async def test_resolve_fork_failure_falls_back_to_preamble(error: Exception) -> None:
    client = _FakeSessionClient(fork_error=error)
    session_id = await _resolve(
        client, _config(fork_carry_history=True), fork_source_session_id="ses_src"
    )
    assert session_id == "ses_new"
    assert [sid for sid, _ in client.seeded] == ["ses_new"]


async def test_resolve_fresh_ignores_resume_and_fork() -> None:
    client = _FakeSessionClient(existing="ses_old")
    session_id = await _resolve(
        client,
        _config(external_session_id="ses_old", fork_carry_history=True),
        fork_source_session_id="ses_src",
        fresh=True,
    )
    assert session_id == "ses_new"
    assert client.forked == [] and client.seeded == []
