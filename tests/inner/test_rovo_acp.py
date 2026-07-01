"""Unit tests for the ACP client (:mod:`omnigent.inner.rovo_acp`).

These spawn a fake ACP server (``tests/inner/_fake_acp_server.py``) over stdio,
so they exercise the real subprocess + JSON-RPC framing without needing the
``acli`` CLI installed.
"""

from __future__ import annotations

import sys

import pytest

from omnigent.inner.rovo_acp import (
    AcpClient,
    _auto_allow_permission,
    default_acp_command,
)


def _fake_server_command() -> list[str]:
    return [sys.executable, "-m", "tests.inner._fake_acp_server"]


def test_default_acp_command_builds_expected_argv() -> None:
    assert default_acp_command() == ["acli", "rovodev", "acp"]
    assert default_acp_command(
        acli_path="/opt/acli", config_file="/c.yml", site_url="https://x"
    ) == [
        "/opt/acli",
        "rovodev",
        "acp",
        "--config-file",
        "/c.yml",
        "--site-url",
        "https://x",
    ]


def test_auto_allow_prefers_allow_once() -> None:
    out = _auto_allow_permission(
        {
            "options": [
                {"optionId": "r1", "kind": "reject_once"},
                {"optionId": "a1", "kind": "allow_once"},
                {"optionId": "a2", "kind": "allow_always"},
            ]
        }
    )
    assert out == {"outcome": {"outcome": "selected", "optionId": "a1"}}


def test_auto_allow_falls_back_to_any_allow_kind() -> None:
    out = _auto_allow_permission({"options": [{"optionId": "x", "kind": "allow_for_session"}]})
    assert out["outcome"]["optionId"] == "x"


def test_auto_allow_falls_back_to_first_option_when_no_allow() -> None:
    out = _auto_allow_permission({"options": [{"optionId": "only", "kind": "reject_once"}]})
    assert out["outcome"]["optionId"] == "only"


def test_auto_allow_handles_no_options() -> None:
    out = _auto_allow_permission({})
    assert out == {"outcome": {"outcome": "selected"}}


@pytest.mark.asyncio
async def test_initialize_and_session_new() -> None:
    client = AcpClient(command=_fake_server_command())
    await client.start()
    try:
        init = await client.initialize()
        assert init["protocolVersion"] == 1
        assert init["agentCapabilities"]["mcpCapabilities"]["http"] is True

        result = await client.session_new(cwd=".")
        assert result["sessionId"] == "sess-1"
        names = [m["name"] for m in result["models"]["availableModels"]]
        assert "Claude Sonnet 4.6" in names
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_session_prompt_streams_updates_and_returns_stop_reason() -> None:
    client = AcpClient(command=_fake_server_command())
    await client.start()
    collected: list[dict] = []

    async def on_update(update: dict) -> None:
        collected.append(update)

    try:
        await client.initialize()
        result = await client.session_new(cwd=".")
        stop = await client.session_prompt(
            result["sessionId"],
            [{"type": "text", "text": "hi"}],
            on_update=on_update,
        )
    finally:
        await client.close()

    assert stop == "end_turn"
    kinds = [u["sessionUpdate"] for u in collected]
    assert "agent_thought_chunk" in kinds
    assert kinds.count("agent_message_chunk") == 2
    texts = [
        u["content"]["text"] for u in collected if u["sessionUpdate"] == "agent_message_chunk"
    ]
    assert "".join(texts) == "PONG"
