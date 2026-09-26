"""Tests for opencode-native resume helpers (transcript render + rehydration)."""

from __future__ import annotations

from typing import Any

import omnigent.runner.app as app

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
    from omnigent.harnesses.opencode_native.client import OpenCodeClientError

    oc = _FakeOpenCodeClient(error=OpenCodeClientError("synthetic failed: 500"))
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=_FakeServerClient(_ITEMS),
    )
    assert ok is False
