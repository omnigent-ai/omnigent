"""Unit tests for omnigent.runtime.memory (automatic long-term memory)."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.runtime import memory as auto_memory
from omnigent.spec.types import MemoryConfig


def _cfg(**overrides: Any) -> MemoryConfig:
    base: dict[str, Any] = {"enabled": True, "api_key": "k"}
    base.update(overrides)
    return MemoryConfig(**base)


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeRecallResponse:
    def __init__(self, texts: list[str]) -> None:
        self.results = [_FakeResult(t) for t in texts]


class _FakeClient:
    """Records calls and returns canned recall results."""

    def __init__(self, *, recall_texts: list[str] | None = None) -> None:
        self._recall_texts = recall_texts or []
        self.retained: list[str] = []
        self.created_banks: list[str] = []

    def create_bank(self, *, bank_id: str, name: str) -> None:
        self.created_banks.append(bank_id)

    def recall(self, **kwargs: Any) -> _FakeRecallResponse:
        return _FakeRecallResponse(self._recall_texts)

    def retain(self, *, bank_id: str, content: str) -> None:
        self.retained.append(content)


# --- extract_latest_user_text ------------------------------------------------


def test_extract_from_block_list() -> None:
    content = [
        {"type": "message", "role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]
    assert auto_memory.extract_latest_user_text(content) == "hello"


def test_extract_picks_last_user_message() -> None:
    content = [
        {"type": "message", "role": "user", "content": [{"type": "text", "text": "first"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "reply"}]},
        {"type": "message", "role": "user", "content": [{"type": "text", "text": "second"}]},
    ]
    assert auto_memory.extract_latest_user_text(content) == "second"


def test_extract_from_plain_string_content() -> None:
    content = [{"type": "message", "role": "user", "content": "  plain  "}]
    assert auto_memory.extract_latest_user_text(content) == "plain"


def test_extract_joins_multiple_text_blocks() -> None:
    content = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        }
    ]
    assert auto_memory.extract_latest_user_text(content) == "a\nb"


def test_extract_no_user_message_returns_none() -> None:
    content = [
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "x"}]}
    ]
    assert auto_memory.extract_latest_user_text(content) is None


def test_extract_non_list_returns_none() -> None:
    assert auto_memory.extract_latest_user_text(None) is None
    assert auto_memory.extract_latest_user_text("not a list") is None


def test_extract_bare_role_shape_without_type() -> None:
    # Inbound-turn items may omit the ``type`` field (executor message shape).
    content = [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert auto_memory.extract_latest_user_text(content) == "hi"


# --- prepend_memory_to_content -----------------------------------------------


def test_prepend_memory_to_block_list() -> None:
    content = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    ]
    result = auto_memory.prepend_memory_to_content(content, "MEM")
    assert isinstance(result, list)
    blocks = result[0]["content"]
    assert blocks[0] == {"type": "input_text", "text": "MEM"}
    assert blocks[1] == {"type": "input_text", "text": "hi"}


def test_prepend_memory_targets_latest_user_message() -> None:
    content = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "old"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "new"}]},
    ]
    result = auto_memory.prepend_memory_to_content(content, "MEM")
    assert result[0]["content"][0]["text"] == "old"  # untouched
    assert result[1]["content"][0]["text"] == "MEM"
    assert result[1]["content"][1]["text"] == "new"


def test_prepend_memory_to_string_content() -> None:
    content = [{"type": "message", "role": "user", "content": "plain"}]
    result = auto_memory.prepend_memory_to_content(content, "MEM")
    blocks = result[0]["content"]
    assert blocks[0] == {"type": "input_text", "text": "MEM"}
    assert blocks[1] == {"type": "input_text", "text": "plain"}


def test_prepend_memory_does_not_mutate_input() -> None:
    content = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]
    auto_memory.prepend_memory_to_content(content, "MEM")
    assert content[0]["content"] == [{"type": "input_text", "text": "hi"}]


def test_prepend_memory_unrecognised_shape_returns_unchanged() -> None:
    assert auto_memory.prepend_memory_to_content(None, "MEM") is None
    no_user = [{"type": "message", "role": "assistant", "content": []}]
    assert auto_memory.prepend_memory_to_content(no_user, "MEM") is no_user


def test_prepend_memory_bare_role_shape_without_type() -> None:
    # Inbound-turn items may omit the ``type`` field (executor message shape).
    content = [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    result = auto_memory.prepend_memory_to_content(content, "MEM")
    assert result[0]["content"][0] == {"type": "input_text", "text": "MEM"}


# --- resolve_bank ------------------------------------------------------------


def test_resolve_bank_prefers_config() -> None:
    assert auto_memory.resolve_bank(_cfg(bank_id="cfg"), "agent", "conv") == "cfg"


def test_resolve_bank_falls_back_to_agent_then_conversation() -> None:
    assert auto_memory.resolve_bank(_cfg(), "agent", "conv") == "agent"
    assert auto_memory.resolve_bank(_cfg(), "", "conv") == "conv"


def test_resolve_bank_none_when_nothing() -> None:
    assert auto_memory.resolve_bank(_cfg(), "", "") is None
    assert auto_memory.resolve_bank(_cfg(), None, None) is None


# --- recall_instructions -----------------------------------------------------


async def test_recall_formats_block(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(recall_texts=["likes tea", "based in NYC"])
    monkeypatch.setattr(auto_memory, "_build_client", lambda cfg, **_: client)
    block = await auto_memory.recall_instructions(_cfg(), "bank", "query")
    assert block is not None
    assert "- likes tea" in block
    assert "- based in NYC" in block
    assert "long-term memory" in block.lower()


async def test_recall_empty_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auto_memory, "_build_client", lambda cfg, **_: _FakeClient(recall_texts=[])
    )
    assert await auto_memory.recall_instructions(_cfg(), "bank", "query") is None


async def test_recall_backend_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(cfg: MemoryConfig, **_: object) -> Any:
        raise RuntimeError("backend down")

    monkeypatch.setattr(auto_memory, "_build_client", _boom)
    assert await auto_memory.recall_instructions(_cfg(), "bank", "query") is None


async def test_recall_times_out_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _slow_recall(cfg: MemoryConfig, bank: str, query: str) -> list[str]:
        import time

        time.sleep(0.5)
        return ["late"]

    backend = auto_memory._MemoryBackend(recall=_slow_recall, retain=lambda *_: None)
    monkeypatch.setattr(auto_memory, "_backend_for", lambda cfg: backend)
    result = await auto_memory.recall_instructions(_cfg(recall_timeout=0.05), "bank", "query")
    assert result is None


async def test_recall_client_uses_recall_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recall builds its client with recall_timeout so a timed-out call unwinds
    promptly instead of pinning a worker thread for the default 30s."""
    seen: dict[str, object] = {}

    def _capture(cfg: MemoryConfig, **kwargs: object) -> _FakeClient:
        seen["timeout"] = kwargs.get("timeout")
        return _FakeClient(recall_texts=["x"])

    monkeypatch.setattr(auto_memory, "_build_client", _capture)
    await auto_memory.recall_instructions(_cfg(recall_timeout=3.5), "bank", "q")
    assert seen["timeout"] == 3.5


# --- provider dispatch -------------------------------------------------------


def test_backend_for_hindsight() -> None:
    assert auto_memory._backend_for(_cfg(provider="hindsight")) is not None


def test_backend_for_unknown_returns_none() -> None:
    assert auto_memory._backend_for(_cfg(provider="nope")) is None


async def test_recall_unknown_provider_returns_none() -> None:
    # Provider is validated at parse time; the runtime still guards defensively.
    assert await auto_memory.recall_instructions(_cfg(provider="nope"), "bank", "q") is None


async def test_schedule_retain_unknown_provider_noop() -> None:
    auto_memory.schedule_retain(_cfg(provider="nope"), "bank", "content")
    await _drain_retain_tasks()  # must not raise or schedule work


# --- schedule_retain ---------------------------------------------------------


async def _drain_retain_tasks() -> None:
    for task in list(auto_memory._RETAIN_TASKS):
        await task


async def test_schedule_retain_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(auto_memory, "_build_client", lambda cfg, **_: client)
    monkeypatch.setattr(auto_memory, "_CREATED_BANKS", set())
    auto_memory.schedule_retain(_cfg(), "bank", "remember this")
    await _drain_retain_tasks()
    assert client.retained == ["remember this"]
    assert client.created_banks == ["bank"]


async def test_schedule_retain_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(cfg: MemoryConfig, **_: object) -> Any:
        raise RuntimeError("nope")

    monkeypatch.setattr(auto_memory, "_build_client", _boom)
    auto_memory.schedule_retain(_cfg(), "bank", "content")
    # Must not raise into the caller even though retain failed.
    await _drain_retain_tasks()
