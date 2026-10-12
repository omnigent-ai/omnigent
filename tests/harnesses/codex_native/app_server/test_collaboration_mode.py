"""``apply_codex_thread_collaboration_mode`` tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.codex_native import app_server


@pytest.mark.parametrize(
    ("effort", "expected_effort"),
    [("high", "high"), (None, None)],
)
async def test_collaboration_mode_switch_keeps_developer_instructions_null(
    monkeypatch: pytest.MonkeyPatch,
    effort: str | None,
    expected_effort: str | None,
) -> None:
    """The switch names the thread's model and effort but never a developer prompt:
    a value there would replace Codex's built-in Plan Mode instructions."""
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.return_value = {"result": {}}
    client_calls: list[tuple[str, str]] = []

    def _client_for_transport(transport: str, *, client_name: str) -> AsyncMock:
        client_calls.append((transport, client_name))
        return client

    monkeypatch.setattr(app_server, "client_for_transport", _client_for_transport)

    await app_server.apply_codex_thread_collaboration_mode(
        "ws://127.0.0.1:1", "thread_fresh", "plan", model="gpt-5.4", effort=effort
    )

    assert client_calls == [("ws://127.0.0.1:1", "omnigent-codex-native-runner")]
    client.request.assert_awaited_once_with(
        "thread/settings/update",
        {
            "threadId": "thread_fresh",
            "collaborationMode": {
                "mode": "plan",
                "settings": {
                    "model": "gpt-5.4",
                    "reasoning_effort": expected_effort,
                    "developer_instructions": None,
                },
            },
        },
    )
    client.connect.assert_awaited_once()
    client.close.assert_awaited_once()


@pytest.mark.parametrize("failing_step", ["connect", "request"])
async def test_collaboration_mode_switch_closes_the_client_on_failure(
    monkeypatch: pytest.MonkeyPatch, failing_step: str
) -> None:
    """A rejected initialization or update still closes the client, and a close that
    itself fails does not mask the original error."""
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    getattr(client, failing_step).side_effect = RuntimeError(f"{failing_step} failed")
    client.close.side_effect = RuntimeError("close hung")
    monkeypatch.setattr(app_server, "client_for_transport", lambda *_a, **_k: client)

    with pytest.raises(RuntimeError, match=f"{failing_step} failed"):
        await app_server.apply_codex_thread_collaboration_mode(
            "ws://127.0.0.1:1", "thread_fresh", "plan", model="gpt-5.4"
        )

    client.close.assert_awaited_once()
    if failing_step == "connect":
        client.request.assert_not_awaited()


@pytest.mark.parametrize("stalled_step", ["connect", "request", "close"])
async def test_collaboration_mode_switch_bounds_an_unanswered_app_server(
    monkeypatch: pytest.MonkeyPatch, stalled_step: str
) -> None:
    """An app-server that never answers cannot hold session startup: initialization
    and the settings update time out, and a stalled close is abandoned silently."""
    monkeypatch.setattr(app_server, "_EFFORT_CONNECT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(app_server, "_EFFORT_SETTINGS_UPDATE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(app_server, "_EFFORT_CLOSE_TIMEOUT_SECONDS", 0.05)

    async def _never(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.return_value = {"result": {}}
    getattr(client, stalled_step).side_effect = _never
    monkeypatch.setattr(app_server, "client_for_transport", lambda *_a, **_k: client)

    switch = app_server.apply_codex_thread_collaboration_mode(
        "ws://127.0.0.1:1", "thread_fresh", "plan", model="gpt-5.4"
    )
    if stalled_step == "close":
        await asyncio.wait_for(switch, timeout=2)
    else:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(switch, timeout=2)
    client.close.assert_awaited_once()


async def test_collaboration_mode_switch_guards_a_capped_models_default_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no effort requested, a model whose gateway caps its ladder gets that
    model's fallback instead of inheriting a config default it would reject."""
    from omnigent.util import reasoning_effort

    caps = reasoning_effort.ModelEffortCaps(
        fallback={"glm-5": "high"}, unsupported={"glm-5": frozenset({"xhigh"})}
    )
    monkeypatch.setattr(reasoning_effort, "model_effort_caps", lambda _caps=None: caps)
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.return_value = {"result": {}}
    monkeypatch.setattr(app_server, "client_for_transport", lambda *_a, **_k: client)

    await app_server.apply_codex_thread_collaboration_mode(
        "ws://127.0.0.1:1", "thread_fresh", "plan", model="glm-5"
    )

    settings = client.request.await_args.args[1]["collaborationMode"]["settings"]
    assert settings == {
        "model": "glm-5",
        "reasoning_effort": "high",
        "developer_instructions": None,
    }
