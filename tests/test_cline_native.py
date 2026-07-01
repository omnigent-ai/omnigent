"""Tests for cline-native CLI orchestration."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent import cline_native


class _FakeAsyncClient:
    """Minimal async client for cline-native daemon orchestration tests."""

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
        if url == "/v1/sessions/conv_cline":
            return httpx.Response(
                200,
                json={"labels": {"omnigent.wrapper": "cline-native-ui"}},
                request=request,
            )
        if url.endswith("/resources/terminals/terminal_cline_main"):
            self.terminal_gets += 1
            if not self.terminal_running and self.terminal_gets == 1:
                return httpx.Response(404, request=request)
            return httpx.Response(
                200,
                json={
                    "id": "terminal_cline_main",
                    "metadata": {
                        "running": True,
                        "tmux_socket": "/tmp/cline.sock",
                        "tmux_target": "cline:0",
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
async def test_cline_resume_to_live_terminal_is_marked_as_reattach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume with a still-running terminal is a true live reattach."""

    fake = _FakeAsyncClient(terminal_running=True)
    monkeypatch.setattr(cline_native.httpx, "AsyncClient", lambda **_: fake)

    prepared = await cline_native._prepare_cline_terminal_via_daemon(
        base_url="http://server",
        headers={},
        session_id="conv_cline",
        session_bundle=None,
        cline_args=("-f",),
        host_id="host_1",
        workspace="/workspace",
    )

    assert prepared.reattached is True
    assert prepared.cold_resumed is False
    assert prepared.terminal_id == "terminal_cline_main"
    assert fake.patch_calls == []
    assert fake.post_calls == []


@pytest.mark.asyncio
async def test_cline_resume_without_live_terminal_is_marked_as_cold_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume whose terminal is gone cold-starts a fresh Cline TUI."""

    fake = _FakeAsyncClient(terminal_running=False)
    monkeypatch.setattr(cline_native.httpx, "AsyncClient", lambda **_: fake)
    monkeypatch.setattr(cline_native, "wait_for_host_online", _async_noop)
    monkeypatch.setattr(cline_native, "wait_for_runner_online", _async_noop)
    monkeypatch.setattr(cline_native, "launch_or_reuse_daemon_runner", _launch_runner)
    monkeypatch.setattr(cline_native, "_bind_session_runner", _async_noop)

    prepared = await cline_native._prepare_cline_terminal_via_daemon(
        base_url="http://server",
        headers={},
        session_id="conv_cline",
        session_bundle=None,
        cline_args=("-f",),
        host_id="host_1",
        workspace="/workspace",
    )

    assert prepared.reattached is False
    assert prepared.cold_resumed is True
    assert prepared.terminal_id == "terminal_cline_main"
    assert fake.patch_calls == [("/v1/sessions/conv_cline", {"terminal_launch_args": ["-f"]})]
    assert fake.post_calls == [
        (
            "/v1/sessions/conv_cline/resources/terminals",
            {
                "terminal": "cline",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        )
    ]


def test_resolve_cline_executable_prefers_env_override() -> None:
    """``OMNIGENT_CLINE_PATH`` overrides the default ``cline`` binary name."""

    resolved = cline_native.resolve_cline_executable(
        env={"OMNIGENT_CLINE_PATH": "/opt/cline/bin/cline"},
        which=lambda name: name if name == "/opt/cline/bin/cline" else None,
    )
    assert resolved == "/opt/cline/bin/cline"


def test_resolve_cline_executable_missing_raises() -> None:
    """A missing ``cline`` CLI raises an actionable install hint."""

    import click

    with pytest.raises(click.ClickException) as excinfo:
        cline_native.resolve_cline_executable(env={}, which=lambda _name: None)
    assert "npm install -g cline" in str(excinfo.value)


def test_build_cline_launch_opens_interactive_tui() -> None:
    """The launch argv runs the interactive TUI (``cline -i``) plus passthrough args."""

    launch = cline_native.build_cline_launch(
        ("--model", "claude-sonnet-4-6"),
        env={},
        which=lambda _name: "/usr/local/bin/cline",
    )
    assert launch.executable == "/usr/local/bin/cline"
    assert launch.argv == ["/usr/local/bin/cline", "-i", "--model", "claude-sonnet-4-6"]


async def _async_noop(*_: object, **__: object) -> None:
    return None


async def _launch_runner(*_: object, **__: object) -> str:
    return "runner_1"
