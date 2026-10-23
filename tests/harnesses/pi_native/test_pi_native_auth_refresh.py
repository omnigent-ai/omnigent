"""Tests for the runner-owned Pi bridge bearer refresh."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent import cli_auth
from omnigent.harnesses.pi_native import auth_refresh
from omnigent.runner import _entry as runner_entry
from tests._helpers.polling import poll_until_async

_URL = "https://example.cloud.databricks.com/api/2.0/omnigent"


def _config(bridge_dir: Path) -> Path:
    path = bridge_dir / "config.json"
    headers = {"Authorization": "Bearer stale", "X-Omnigent-Runner-Tunnel-Token": "keep"}
    path.write_text(json.dumps({"authHeaders": headers}))
    return path


def _bearer(path: Path) -> str:
    return json.loads(path.read_text())["authHeaders"]["Authorization"]


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_refresh_retries_failures_and_keeps_launch_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Failures keep the config and log the type once; a later mint is merged in."""
    path = _config(tmp_path)
    mints = iter([RuntimeError("secret"), RuntimeError("secret"), None, "fresh"])
    calls: list[dict[str, object]] = []

    def _make(url: str, **kwargs: object):
        calls.append({"url": url, **kwargs})
        result = next(mints)

        def _mint() -> str | None:
            if isinstance(result, Exception):
                assert _bearer(path) == "Bearer stale"
                raise result
            return result

        return _mint

    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", _make)
    task = asyncio.create_task(
        auth_refresh.supervise_pi_auth_refresh(tmp_path, _URL, interval_s=0)
    )
    try:
        await poll_until_async(lambda: _bearer(path) == "Bearer fresh", timeout_s=5)
    finally:
        await _cancel(task)

    assert json.loads(path.read_text())["authHeaders"]["X-Omnigent-Runner-Tunnel-Token"] == "keep"
    assert calls == [{"url": _URL, "_allow_initial_token": False}] * 4
    assert [r.getMessage() for r in caplog.records] == [
        "Pi bridge auth refresh failed (RuntimeError)"
    ]


@pytest.mark.asyncio
async def test_default_interval_renews_cached_token_before_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's real OIDC factory must renew before the bridge bearer lapses."""
    path = _config(tmp_path)
    now = 0.0
    monkeypatch.delenv("OMNIGENT_PROXY_BEARER_FILE", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_DELEGATED_AUTH", raising=False)
    monkeypatch.setattr(cli_auth, "_token_file_path", lambda: tmp_path / "tokens.json")
    monkeypatch.setattr(cli_auth, "time", SimpleNamespace(time=lambda: now))
    cli_auth.store_token(_URL, "stale", "test@example.com", expires_at=150)

    def _renew(url: str) -> str:
        cli_auth.store_token(url, "fresh", "test@example.com", expires_at=3600)
        return "fresh"

    async def _advance(interval_s: float) -> None:
        nonlocal now
        now += interval_s
        if _bearer(path) == "Bearer fresh":
            raise asyncio.CancelledError
        assert now < 150, "the bridge bearer expired before the next refresh"

    monkeypatch.setattr(cli_auth, "refresh_stored_token", _renew)
    monkeypatch.setattr(
        auth_refresh,
        "asyncio",
        SimpleNamespace(to_thread=asyncio.to_thread, wait_for=asyncio.wait_for, sleep=_advance),
    )
    with pytest.raises(asyncio.CancelledError):
        await auth_refresh.supervise_pi_auth_refresh(tmp_path, _URL)
    assert _bearer(path) == "Bearer fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_interrupted_mint_does_not_write_late_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """A hung mint cannot overwrite the config after cancellation or recovery."""
    path = _config(tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def _mint() -> str:
        if started.is_set():
            return "fresh"
        started.set()
        release.wait(timeout=10)
        finished.set()
        return "late"

    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", lambda *_a, **_k: _mint)
    task = asyncio.create_task(
        auth_refresh.supervise_pi_auth_refresh(
            tmp_path, _URL, interval_s=0, timeout_s=0.1 if interruption == "timeout" else 30
        )
    )
    expected = "Bearer fresh" if interruption == "timeout" else "Bearer stale"
    try:
        await poll_until_async(started.is_set, timeout_s=5, interval_s=0.005)
        if interruption == "timeout":
            await poll_until_async(lambda: _bearer(path) == expected, timeout_s=5)
    finally:
        await _cancel(task)
        release.set()
        await poll_until_async(finished.is_set, timeout_s=5)
    assert _bearer(path) == expected
