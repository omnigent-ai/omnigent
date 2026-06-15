"""Tests for :class:`omnigent.inner.mimo_executor.MimoExecutor`."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import mimo_executor as me
from omnigent.inner.executor import ExecutorConfig, TextChunk, TurnComplete
from omnigent.inner.mimo_executor import MimoExecutor


def _make_fake_acp_client(captured: dict[str, Any]) -> type:
    """Build a FakeAcpClient that records constructor + call args into *captured*."""

    class FakeAcpClient:
        running = True

        def __init__(
            self,
            path: str,
            *,
            env: dict[str, str],
            cwd: str | None,
            extra_args: list[str] | None = None,
        ) -> None:
            captured["path"] = path
            captured["env"] = env
            captured["cwd"] = cwd
            captured["extra_args"] = extra_args

        async def start(self) -> dict[str, Any]:
            return {}

        async def new_session(
            self,
            *,
            cwd: str,
            model: str | None,
            mcp_servers: list[dict[str, Any]] | None = None,
        ) -> str:
            captured["new_session"] = {"cwd": cwd, "model": model, "mcp_servers": mcp_servers}
            return "s1"

        async def prompt_stream(self, session_id: str, blocks: list[dict[str, Any]]):
            captured["prompt"] = {"session_id": session_id, "blocks": blocks}
            yield ("update", {"sessionUpdate": "agent_message_chunk", "content": {"text": "ok"}})
            yield ("result", {"stopReason": "end_turn"})

        async def close(self) -> None:
            pass

    return FakeAcpClient


def _make_executor(**kwargs: Any) -> MimoExecutor:
    with patch("omnigent.inner.mimo_executor._find_mimo", return_value="/usr/bin/mimo"):
        return MimoExecutor(**kwargs)


def test_missing_mimo_raises_import_error() -> None:
    with patch("omnigent.inner.mimo_executor._find_mimo", return_value=None):
        with pytest.raises(ImportError, match="mimo"):
            MimoExecutor()


def test_clean_mimo_env_allows_mimo_prefix_denies_secrets(monkeypatch) -> None:
    monkeypatch.setenv("MIMOCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("MIMO_CONFIG", "1")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = me._clean_mimo_env()

    assert env["MIMOCODE_SERVER_PASSWORD"] == "pw"
    assert env["MIMO_CONFIG"] == "1"
    assert env["PATH"] == "/usr/bin"
    assert "DATABRICKS_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


async def _run_one_turn(executor: MimoExecutor, config: ExecutorConfig | None) -> list[Any]:
    return [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello", "session_id": "conv1"}],
            [],
            "system",
            config,
        )
    ]


@pytest.mark.asyncio
async def test_mimo_model_and_cwd_passed_to_acp(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(me, "AcpClient", _make_fake_acp_client(captured))
    executor = _make_executor(cwd="/repo", model="anthropic/claude-sonnet-4")

    events = await _run_one_turn(executor, ExecutorConfig(model="openai/gpt-5"))

    assert captured["path"] == "/usr/bin/mimo"
    assert captured["cwd"] == "/repo"
    assert captured["extra_args"] == ["--cwd", "/repo"]
    assert captured["new_session"] == {
        "cwd": "/repo",
        "model": "openai/gpt-5",
        "mcp_servers": [],
    }
    assert captured["prompt"]["blocks"] == [{"type": "text", "text": "system\n\nhello"}]
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "ok"
    assert isinstance(events[1], TurnComplete)
    assert events[1].response == "ok"


@pytest.mark.asyncio
async def test_mimo_defaults_primary_model_to_mimo_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no model configured, the inline config forces ``mimo/mimo-auto``.

    Regression: ``mimo acp``'s ``session/new`` model only sets the title
    subagent; the primary agent reads ``MIMOCODE_CONFIG_CONTENT``'s ``model``.
    Without it Mimo falls back to a key-less default and silently no-ops.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(me, "AcpClient", _make_fake_acp_client(captured))
    executor = _make_executor(cwd="/repo")

    await _run_one_turn(executor, None)

    config = json.loads(captured["env"]["MIMOCODE_CONFIG_CONTENT"])
    assert config["model"] == "mimo/mimo-auto"
    # No model resolved, so the session param stays unset (config drives it).
    assert captured["new_session"]["model"] is None


@pytest.mark.asyncio
async def test_mimo_explicit_model_drives_config_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit model lands in the inline config's ``model`` field."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(me, "AcpClient", _make_fake_acp_client(captured))
    executor = _make_executor(cwd="/repo", model="anthropic/claude-sonnet-4")

    await _run_one_turn(executor, ExecutorConfig(model="openai/gpt-5"))

    config = json.loads(captured["env"]["MIMOCODE_CONFIG_CONTENT"])
    assert config["model"] == "openai/gpt-5"


@pytest.mark.asyncio
async def test_mimo_preserves_caller_config_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model in an inherited MIMOCODE_CONFIG_CONTENT wins over the default."""
    monkeypatch.setenv(
        "MIMOCODE_CONFIG_CONTENT", json.dumps({"model": "custom/model", "theme": "dark"})
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(me, "AcpClient", _make_fake_acp_client(captured))
    executor = _make_executor(cwd="/repo")

    await _run_one_turn(executor, None)

    config = json.loads(captured["env"]["MIMOCODE_CONFIG_CONTENT"])
    assert config["model"] == "custom/model"
    assert config["theme"] == "dark"
