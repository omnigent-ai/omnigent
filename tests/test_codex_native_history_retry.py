"""Cold resume recovers transient history reads without losing committed items."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import click
import httpx
import pytest

from omnigent.harnesses.codex_native import main as codex_native

_THREAD_ID = "019e96aa-0be2-7343-8d3b-6f914d60936b"


@pytest.fixture(autouse=True)
def retry_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(codex_native, "_codex_resume_history_retry_sleep", sleep, raising=False)
    return delays


def _item(item_id: str) -> dict[str, object]:
    return {
        "id": item_id,
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": item_id}],
    }


async def _resume(client: httpx.AsyncClient, tmp_path: Path) -> Path:
    return await codex_native._ensure_local_codex_resume_rollout(
        client,
        session_id="conv_resume",
        external_session_id=_THREAD_ID,
        codex_home=tmp_path / "codex-home",
        workspace=tmp_path.resolve(),
        model_provider="test_provider",
        codex_path=None,
    )


def _local_rollout(tmp_path: Path) -> Path:
    path = tmp_path / "codex-home" / "sessions" / f"rollout-local-{_THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": _THREAD_ID}}) + "\n")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_page", [None, "msg_first"])
@pytest.mark.parametrize(
    "fault", [500, 502, 503, 504, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
async def test_cold_resume_recovers_transient_history_page(
    tmp_path: Path,
    failed_page: str | None,
    fault: int | type[httpx.TransportError],
    retry_delays: list[float],
) -> None:
    """A failed page is retried in place and both committed messages reach Codex."""
    requested_pages: list[str | None] = []
    failed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        assert request.method == "GET"
        assert request.url.path == "/v1/sessions/conv_resume/items"
        after = request.url.params.get("after")
        requested_pages.append(after)
        if after == failed_page and not failed:
            failed = True
            if isinstance(fault, int):
                return httpx.Response(fault, text="temporarily unavailable")
            raise fault("temporary history read failure", request=request)
        if after is None:
            return httpx.Response(
                200, json={"data": [_item("msg_first")], "has_more": True, "last_id": "msg_first"}
            )
        assert after == "msg_first"
        return httpx.Response(200, json={"data": [_item("msg_second")], "has_more": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        rollout = await _resume(client, tmp_path)

    records = [json.loads(line) for line in rollout.read_text().splitlines()]
    assert records[0]["payload"]["id"] == _THREAD_ID
    assert [r["payload"]["id"] for r in records if r["type"] == "response_item"] == [
        "msg_first",
        "msg_second",
    ]
    assert requested_pages == (
        [None, None, "msg_first"] if failed_page is None else [None, "msg_first", "msg_first"]
    )
    assert retry_delays == [0.5]


@pytest.mark.asyncio
async def test_recovery_refreshes_server_history_before_using_local_fallback(
    tmp_path: Path,
) -> None:
    """An available server response wins over a stale local rollout after a blip."""
    existing = _local_rollout(tmp_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [_item("msg_committed")], "has_more": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        rollout = await _resume(client, tmp_path)

    assert rollout == existing
    assert "msg_committed" in rollout.read_text()
    assert calls == 2


@pytest.mark.asyncio
async def test_recovery_on_last_attempt_records_success(
    tmp_path: Path, retry_delays: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=codex_native.__name__)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary read timeout", request=request)
        if calls == 2:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [_item("msg_committed")], "has_more": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        rollout = await _resume(client, tmp_path)

    assert "msg_committed" in rollout.read_text()
    assert calls == 3
    assert retry_delays == [0.5, 1.0]
    recovered = [
        r
        for r in caplog.records
        if getattr(r, "event_name", "") == "codex_resume_history_recovered"
    ]
    assert len(recovered) == 1
    assert recovered[0].session_id == "conv_resume"
    assert recovered[0].attributes == {"retries": 2, "pages": 1, "items": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("local_fallback", [False, True])
async def test_persistent_failure_is_bounded_and_never_writes_partial_history(
    tmp_path: Path,
    local_fallback: bool,
    retry_delays: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exhaustion preserves a valid fallback or fails without writing a partial rollout."""
    existing = _local_rollout(tmp_path) if local_fallback else None
    original = existing.read_bytes() if existing else None
    requested_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        requested_pages.append(after)
        if after is None:
            return httpx.Response(
                200, json={"data": [_item("msg_first")], "has_more": True, "last_id": "msg_first"}
            )
        return httpx.Response(503)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        if existing:
            assert await _resume(client, tmp_path) == existing
            assert existing.read_bytes() == original
        else:
            with pytest.raises(click.ClickException, match="Failed to fetch history"):
                await _resume(client, tmp_path)
            assert not list((tmp_path / "codex-home").rglob("*.jsonl"))

    assert requested_pages == [None, "msg_first", "msg_first", "msg_first"]
    assert retry_delays == [0.5, 1.0]
    assert not [
        r
        for r in caplog.records
        if getattr(r, "event_name", "") == "codex_resume_history_recovered"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 422])
async def test_rejected_history_is_not_retried_or_replaced_by_local_data(
    tmp_path: Path, status: int, retry_delays: list[float]
) -> None:
    existing = _local_rollout(tmp_path)
    original = existing.read_bytes()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(click.ClickException, match="Failed to fetch history"):
            await _resume(client, tmp_path)

    assert calls == 1
    assert retry_delays == []
    assert existing.read_bytes() == original


@pytest.mark.asyncio
async def test_cancellation_during_retry_stops_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def cancel(delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(codex_native, "_codex_resume_history_retry_sleep", cancel, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await _resume(client, tmp_path)

    assert calls == 1
    assert not list((tmp_path / "codex-home").rglob("*.jsonl"))
