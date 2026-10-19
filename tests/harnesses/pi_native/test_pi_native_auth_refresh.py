"""Tests for the runner-owned Pi bridge bearer refresh."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from omnigent.harnesses.pi_native import auth_refresh
from omnigent.runner import _entry as runner_entry

_URL = "https://example.cloud.databricks.com/api/2.0/omnigent"


def _config(bridge_dir: Path) -> Path:
    path = bridge_dir / "config.json"
    headers = {"Authorization": "Bearer stale", "X-Omnigent-Runner-Tunnel-Token": "keep"}
    path.write_text(json.dumps({"authHeaders": headers}))
    return path


def _bearer(path: Path) -> str:
    return json.loads(path.read_text())["authHeaders"]["Authorization"]


async def _cancel_when(cond, task: asyncio.Task[None]) -> None:
    for _ in range(400):
        if cond():
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cond()


@pytest.mark.asyncio
async def test_refresh_retries_failures_and_keeps_launch_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Failures keep the config and log the type once; a later mint is merged in."""
    path = _config(tmp_path)
    mints = iter([RuntimeError("secret"), RuntimeError("secret"), "fresh"])
    calls: list[dict[str, object]] = []

    def _mint() -> str:
        result = next(mints, "fresh")
        if isinstance(result, Exception):
            assert _bearer(path) == "Bearer stale"
            raise result
        return result

    def _make(url: str, **kwargs: object):
        calls.append({"url": url, **kwargs})
        return _mint

    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", _make)
    task = asyncio.create_task(
        auth_refresh.supervise_pi_auth_refresh(tmp_path, _URL, interval_s=0)
    )
    await _cancel_when(lambda: _bearer(path) == "Bearer fresh", task)

    assert json.loads(path.read_text())["authHeaders"]["X-Omnigent-Runner-Tunnel-Token"] == "keep"
    assert calls == [{"url": _URL, "_allow_initial_token": False}] * 3
    assert [r.getMessage() for r in caplog.records] == [
        "Pi bridge auth refresh failed (RuntimeError)"
    ]


@pytest.mark.asyncio
async def test_cancel_during_mint_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mint that finishes after cancellation never reaches the config."""
    path = _config(tmp_path)
    started, release = threading.Event(), threading.Event()

    def _mint() -> str:
        started.set()
        release.wait(timeout=5)
        return "late"

    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", lambda *_a, **_k: _mint)
    task = asyncio.create_task(auth_refresh.supervise_pi_auth_refresh(tmp_path, _URL))
    await asyncio.to_thread(started.wait, 5)
    await _cancel_when(lambda: True, task)
    release.set()
    await asyncio.sleep(0.05)
    assert _bearer(path) == "Bearer stale"
