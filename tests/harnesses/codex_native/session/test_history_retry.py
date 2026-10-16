"""Codex resume recovers from session API throttling without losing history."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click
import httpx
import pytest

from omnigent.harnesses.codex_native import main as codex_native
from omnigent.util import http_retry
from tests.harnesses.codex_native.session._support import _write_source_rollout

_THREAD_ID = "019e96aa-0be2-7343-8d3b-6f914d60936b"


@pytest.fixture
def retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(codex_native, "_codex_history_retry_sleep", sleep)
    monkeypatch.setattr(http_retry.random, "uniform", lambda low, high: high / 2)
    return slept


async def test_history_retries_only_the_throttled_page(retry_sleeps: list[float]) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200, json={"data": [{"id": "first"}], "has_more": True, "last_id": "first"}
            )
        if len(requests) == 2:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"data": [{"id": "last"}], "has_more": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        items = await codex_native._fetch_all_session_items_for_codex_resume(client, "conv_codex")

    assert items == [{"id": "first"}, {"id": "last"}]
    assert [r.url.params.get("after") for r in requests] == [None, "first", "first"]
    assert all(r.url.path == "/v1/sessions/conv_codex/items" for r in requests)
    assert retry_sleeps == [2.25]


@pytest.mark.parametrize("local_state", ["valid", "missing", "invalid"])
@pytest.mark.parametrize(
    ("retry_after", "attempts", "delays"),
    [(None, 3, [0.75, 1.5]), ("3600", 1, [])],
)
async def test_persistent_throttling_uses_only_valid_local_history(
    tmp_path: Path,
    retry_sleeps: list[float],
    local_state: str,
    retry_after: str | None,
    attempts: int,
    delays: list[float],
) -> None:
    codex_home = tmp_path / "codex-home"
    existing = None
    before = None
    if local_state != "missing":
        existing = _write_source_rollout(
            codex_home=codex_home, thread_id=_THREAD_ID, source_cwd="/local/fallback"
        )
        if local_state == "invalid":
            existing.write_text("not json\n")
        before = existing.read_bytes()
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={} if retry_after is None else {"Retry-After": retry_after},
            json={"error": "Too many requests"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        resume = codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id="conv_codex",
            external_session_id=_THREAD_ID,
            codex_home=codex_home,
            workspace=tmp_path / "workspace",
            model_provider="test_provider",
            codex_path=None,
        )
        if local_state == "valid":
            assert await resume == existing
        else:
            with pytest.raises(codex_native._CodexResumeHistoryUnavailableError, match="429"):
                await resume

    assert calls == attempts
    assert retry_sleeps == delays
    if existing is not None:
        assert existing.read_bytes() == before
    else:
        assert not list(codex_home.glob("sessions/**/*.jsonl"))


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_permanent_rejection_does_not_retry_or_fall_back(
    tmp_path: Path, retry_sleeps: list[float], status: int
) -> None:
    codex_home = tmp_path / "codex-home"
    existing = _write_source_rollout(
        codex_home=codex_home, thread_id=_THREAD_ID, source_cwd="/local/fallback"
    )
    before = existing.read_bytes()
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Retry-After": "2"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException, match=str(status)) as error:
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id=_THREAD_ID,
                codex_home=codex_home,
                workspace=tmp_path / "workspace",
                model_provider="test_provider",
                codex_path=None,
            )

    assert not isinstance(error.value, codex_native._CodexResumeHistoryUnavailableError)
    assert calls == 1
    assert retry_sleeps == []
    assert existing.read_bytes() == before


async def test_cancellation_during_backoff_stops_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    async def cancel(delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(codex_native, "_codex_history_retry_sleep", cancel)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await codex_native._fetch_all_session_items_for_codex_resume(client, "conv_codex")

    assert calls == 1
