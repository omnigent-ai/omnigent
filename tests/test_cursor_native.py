"""Tests for cursor-native CLI orchestration."""

from __future__ import annotations

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


def test_cursor_base_model_options_shape() -> None:
    """The curated base catalog yields id/displayName/isDefault/isCurrent dicts."""

    models = cursor_native.cursor_base_model_options()

    assert models, "catalog must be non-empty"
    assert all(set(m) == {"id", "displayName", "isDefault", "isCurrent"} for m in models)
    # Exactly one default (composer-2.5 is the cursor account default), never current.
    assert [m["id"] for m in models if m["isDefault"]] == ["composer-2.5"]
    assert all(m["isCurrent"] is False for m in models)


def test_cursor_base_model_options_uses_base_id_namespace() -> None:
    """Ids are base ids (round-trip across launch / inject / mirror), not compound.

    Pins the namespace contract: the compound ``--list-models`` ids
    (``gpt-5.2-high``) and the ``--list-models`` claude spelling
    (``claude-4.6-opus``) do NOT inject via ``/model``; the base ids
    (``claude-opus-4-6``) do and are what ``meta.lastUsedModel`` reports.
    """
    ids = {m["id"] for m in cursor_native.cursor_base_model_options()}

    assert {"claude-opus-4-6", "gpt-5.2", "composer-2.5"} <= ids
    # No flattened effort variants and no --list-models claude reordering.
    assert not any("-high" in i or "-low" in i or "-xhigh" in i for i in ids)
    assert "claude-4.6-opus" not in ids


def test_cursor_base_model_options_returns_fresh_copies() -> None:
    """Callers may mutate the returned dicts without corrupting the catalog."""

    first = cursor_native.cursor_base_model_options()
    first[0]["displayName"] = "MUTATED"
    second = cursor_native.cursor_base_model_options()
    assert second[0]["displayName"] != "MUTATED"


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
