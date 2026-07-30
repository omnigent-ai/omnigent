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
    def __init__(self) -> None:
        self.seeded: tuple[str, str, str | None, str | None] | None = None

    async def seed_context(
        self,
        session_id: str,
        text: str,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> bool:
        self.seeded = (session_id, text, provider_id, model_id)
        return True


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


async def test_rehydrate_seeds_transcript_with_model() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=_FakeServerClient(_ITEMS),
        model_override="anthropic/claude-sonnet-4-5",
    )
    assert ok is True
    assert oc.seeded is not None
    session_id, text, provider_id, model_id = oc.seeded
    assert session_id == "ses_1"
    assert "User: hi" in text and "Assistant: yo" in text
    assert (provider_id, model_id) == ("anthropic", "claude-sonnet-4-5")


async def test_rehydrate_no_server_client_returns_false() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="s",
        omnigent_session_id="c",
        server_client=None,
        model_override=None,
    )
    assert ok is False
    assert oc.seeded is None


async def test_rehydrate_empty_transcript_returns_false() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="s",
        omnigent_session_id="c",
        server_client=_FakeServerClient([]),
        model_override=None,
    )
    assert ok is False
    assert oc.seeded is None
