"""Tests for cursor-native CLI orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent import cursor_native


class _FakeAsyncClient:
    """Minimal async client for cursor-native daemon orchestration tests."""

    def __init__(self, *, terminal_running: bool) -> None:
        self.terminal_running = terminal_running
        self.terminal_gets = 0
        self.patch_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any] | None]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def get(self, url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if url == "/v1/sessions/conv_cursor":
            return httpx.Response(
                200,
                json={"labels": {"omnigent.wrapper": "cursor-native-ui"}},
                request=request,
            )
        if url.endswith("/resources/terminals/terminal_cursor_main"):
            self.terminal_gets += 1
            if not self.terminal_running and self.terminal_gets == 1:
                return httpx.Response(404, request=request)
            return httpx.Response(
                200,
                json={
                    "id": "terminal_cursor_main",
                    "metadata": {
                        "running": True,
                        "tmux_socket": "/tmp/cursor.sock",
                        "tmux_target": "cursor:0",
                    },
                },
                request=request,
            )
        raise AssertionError(f"unexpected GET {url}")

    async def patch(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.patch_calls.append((url, json))
        return httpx.Response(200, request=httpx.Request("PATCH", url))

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        **_: object,
    ) -> httpx.Response:
        self.post_calls.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.mark.asyncio
async def test_cursor_resume_to_live_terminal_is_marked_as_reattach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume with a still-running terminal is a true live reattach."""

    fake = _FakeAsyncClient(terminal_running=True)
    monkeypatch.setattr(cursor_native.httpx, "AsyncClient", lambda **_: fake)

    prepared = await cursor_native._prepare_cursor_terminal_via_daemon(
        base_url="http://server",
        headers={},
        session_id="conv_cursor",
        session_bundle=None,
        cursor_args=("-f",),
        host_id="host_1",
        workspace="/workspace",
    )

    assert prepared.reattached is True
    assert prepared.cold_resumed is False
    assert prepared.terminal_id == "terminal_cursor_main"
    assert fake.patch_calls == []
    assert fake.post_calls == []


@pytest.mark.asyncio
async def test_cursor_resume_without_live_terminal_is_marked_as_cold_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume whose terminal is gone cold-starts a fresh Cursor TUI."""

    fake = _FakeAsyncClient(terminal_running=False)
    monkeypatch.setattr(cursor_native.httpx, "AsyncClient", lambda **_: fake)
    monkeypatch.setattr(cursor_native, "wait_for_host_online", _async_noop)
    monkeypatch.setattr(cursor_native, "wait_for_runner_online", _async_noop)
    monkeypatch.setattr(cursor_native, "launch_or_reuse_daemon_runner", _launch_runner)
    monkeypatch.setattr(cursor_native, "_bind_session_runner", _async_noop)

    prepared = await cursor_native._prepare_cursor_terminal_via_daemon(
        base_url="http://server",
        headers={},
        session_id="conv_cursor",
        session_bundle=None,
        cursor_args=("-f",),
        host_id="host_1",
        workspace="/workspace",
    )

    assert prepared.reattached is False
    assert prepared.cold_resumed is True
    assert prepared.terminal_id == "terminal_cursor_main"
    assert fake.patch_calls == [("/v1/sessions/conv_cursor", {"terminal_launch_args": ["-f"]})]
    assert fake.post_calls == [
        (
            "/v1/sessions/conv_cursor/resources/terminals",
            {
                "terminal": "cursor",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_cursor_cold_resume_pins_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model pin on cold resume persists model_override alongside args."""

    fake = _FakeAsyncClient(terminal_running=False)
    monkeypatch.setattr(cursor_native.httpx, "AsyncClient", lambda **_: fake)
    monkeypatch.setattr(cursor_native, "wait_for_host_online", _async_noop)
    monkeypatch.setattr(cursor_native, "wait_for_runner_online", _async_noop)
    monkeypatch.setattr(cursor_native, "launch_or_reuse_daemon_runner", _launch_runner)
    monkeypatch.setattr(cursor_native, "_bind_session_runner", _async_noop)

    await cursor_native._prepare_cursor_terminal_via_daemon(
        base_url="http://server",
        headers={},
        session_id="conv_cursor",
        session_bundle=None,
        cursor_args=("-f",),
        model="gpt-5.2",
        host_id="host_1",
        workspace="/workspace",
    )

    assert fake.patch_calls == [
        ("/v1/sessions/conv_cursor", {"terminal_launch_args": ["-f"], "model_override": "gpt-5.2"})
    ]


_CURSOR_MODELS_OUTPUT = """Available models

auto - Auto (default)
gpt-5.3-codex-low - Codex 5.3 Low
gpt-5.3-codex-high-fast - Codex 5.3 High Fast
gpt-5.1-high - GPT-5.1 High
claude-4.6-opus-high - Opus 4.6 1M
claude-4.6-opus-high-thinking - Opus 4.6 1M Thinking
claude-4-sonnet-thinking - Sonnet 4 Thinking
composer-2.5 - Composer 2.5 (current)
"""


def test_parse_cursor_cli_model_options_normalizes_base_ids() -> None:
    """Live compound variants collapse to injectable base ids in CLI order."""
    models = cursor_native.parse_cursor_cli_model_options(_CURSOR_MODELS_OUTPUT)

    assert models == [
        {"id": "auto", "displayName": "Auto", "isDefault": True, "isCurrent": False},
        {
            "id": "gpt-5.3-codex",
            "displayName": "Codex 5.3",
            "isDefault": False,
            "isCurrent": False,
        },
        {
            "id": "gpt-5.1",
            "displayName": "GPT-5.1",
            "isDefault": False,
            "isCurrent": False,
        },
        {
            "id": "claude-opus-4-6",
            "displayName": "Opus 4.6",
            "isDefault": False,
            "isCurrent": False,
        },
        {
            "id": "composer-2.5",
            "displayName": "Composer 2.5",
            "isDefault": False,
            "isCurrent": True,
        },
    ]


def test_parse_cursor_cli_model_options_keeps_one_default_and_current() -> None:
    """Conflicting CLI tags resolve deterministically in catalog order."""
    models = cursor_native.parse_cursor_cli_model_options(
        """Available models
first-high - First High (default, current)
second-low - Second Low (default, current)
"""
    )

    assert [model["id"] for model in models if model["isDefault"]] == ["first"]
    assert [model["id"] for model in models if model["isCurrent"]] == ["first"]


def test_parse_cursor_cli_model_options_logs_unmapped_claude_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reversed Claude ids that cannot round-trip never reach the picker."""
    models = cursor_native.parse_cursor_cli_model_options(_CURSOR_MODELS_OUTPUT)

    assert all(model["id"] != "claude-4-sonnet" for model in models)
    assert "Skipping non-injectable Cursor model id 'claude-4-sonnet'" in caplog.text


def test_parse_cursor_cli_model_options_rejects_empty_catalog() -> None:
    """Malformed CLI output is retryable rather than cached as an empty picker."""
    with pytest.raises(ValueError, match="did not contain any valid models"):
        cursor_native.parse_cursor_cli_model_options("Available models\n")


def test_list_cursor_cli_model_options_runs_configured_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery invokes the resolved CLI and parses its stdout."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cursor_native,
        "resolve_cursor_executable",
        lambda **_: "/opt/cursor-agent",
    )

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(stdout=_CURSOR_MODELS_OUTPUT)

    monkeypatch.setattr(cursor_native.subprocess, "run", run)

    models = cursor_native.list_cursor_cli_model_options(env={"HOME": "/tmp/home"}, timeout_s=3.0)

    assert models[0]["id"] == "auto"
    assert calls == [
        {
            "command": ["/opt/cursor-agent", "models"],
            "check": True,
            "capture_output": True,
            "text": True,
            "timeout": 3.0,
            "env": {"HOME": "/tmp/home"},
        }
    ]


async def _async_noop(*_: object, **__: object) -> None:
    return None


async def _launch_runner(*_: object, **__: object) -> str:
    return "runner_1"


class TestIsValidCursorChatId:
    """``is_valid_cursor_chat_id`` gates the persisted external_session_id."""

    @pytest.mark.parametrize(
        "chat_id",
        [
            "0ef42bbf-3b80-4bec-ac39-ca46531cbc47",
            "00000000-0000-0000-0000-000000000000",
            "0EF42BBF-3B80-4BEC-AC39-CA46531CBC47",  # uppercase hex
        ],
    )
    def test_accepts_well_formed_uuids(self, chat_id: str) -> None:
        assert cursor_native.is_valid_cursor_chat_id(chat_id) is True

    @pytest.mark.parametrize(
        "chat_id",
        [
            None,
            "",
            "deadbeef",  # hex but not UUID-shaped
            "chat-uuid-abc123",  # non-hex letters
            "----",
            "../../etc/passwd",  # path traversal shape
            "0ef42bbf;reboot",  # shell metachar
            "0ef42bbf-3b80-4bec-ac39-ca46531cbc47x",  # trailing junk
            "0ef42bbf-3b80-4bec-ac39-ca46531cbc4",  # one short in last group
        ],
    )
    def test_rejects_malformed_or_empty(self, chat_id: str | None) -> None:
        assert cursor_native.is_valid_cursor_chat_id(chat_id) is False
