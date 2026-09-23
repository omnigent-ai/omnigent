"""Tests for the process-global native prompt park registry."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from omnigent.native import prompt_parks


class _Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    fake = _Clock()
    monkeypatch.setattr(prompt_parks, "_clock", fake)
    yield fake
    for session in ("conv_a", "conv_b", "conv_threads"):
        prompt_parks.clear_session(session)


def test_open_is_idempotent_and_keeps_its_age(clock: _Clock) -> None:
    prompt_parks.open_park("conv_a", "goose:1")
    clock.now += 10
    prompt_parks.open_park("conv_a", "goose:1")
    assert prompt_parks.oldest_open_age_s("conv_a") == 10.0
    assert prompt_parks.open_keys("conv_a") == ("goose:1",)
    assert prompt_parks.oldest_open_age_s("conv_b") is None


def test_close_one_and_close_by_prefix(clock: _Clock) -> None:
    prompt_parks.open_park("conv_a", "kiro:r1")
    clock.now += 1
    prompt_parks.open_park("conv_a", "kiro:r2")
    prompt_parks.open_park("conv_a", "qwen:r3")
    prompt_parks.close_park("conv_a", "kiro:r1")
    assert prompt_parks.open_keys("conv_a") == ("kiro:r2", "qwen:r3")
    prompt_parks.close_parks("conv_a", "kiro:")
    assert prompt_parks.open_keys("conv_a") == ("qwen:r3",)
    prompt_parks.close_park("conv_a", "missing")
    prompt_parks.close_parks("conv_b", "kiro:")
    prompt_parks.clear_session("conv_a")
    assert prompt_parks.oldest_open_age_s("conv_a") is None


def test_hold_closes_on_exit_and_on_error(clock: _Clock) -> None:
    with prompt_parks.hold("conv_a", "relay-policy:1"):
        assert prompt_parks.open_keys("conv_a") == ("relay-policy:1",)
    assert prompt_parks.open_keys("conv_a") == ()
    with pytest.raises(RuntimeError), prompt_parks.hold("conv_a", "relay-policy:2"):
        raise RuntimeError("policy relay failed")
    assert prompt_parks.open_keys("conv_a") == ()


async def test_released_closes_a_mirror_prefix_sync_and_async(clock: _Clock) -> None:
    prompt_parks.open_park("conv_a", "hermes:1")
    prompt_parks.open_park("conv_a", "codex:x")
    with prompt_parks.released("conv_a", "hermes:"):
        pass
    assert prompt_parks.open_keys("conv_a") == ("codex:x",)
    async with prompt_parks.released("conv_a", "codex:"):
        pass
    assert prompt_parks.open_keys("conv_a") == ()


async def test_a_crashed_mirror_keeps_its_prompts_as_holds(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio

    prompt_parks.open_park("conv_a", "kiro:1")
    with (
        caplog.at_level("WARNING", logger="omnigent.native.prompt_parks"),
        pytest.raises(RuntimeError),
    ):
        async with prompt_parks.released("conv_a", "kiro:"):
            raise RuntimeError("record file vanished")
    # Nothing else reports a prompt still on screen: it holds until its ceiling.
    assert prompt_parks.open_keys("conv_a") == ("kiro:1",)
    assert any("keeping 1 as holds" in r.getMessage() for r in caplog.records)
    # A cancelled mirror (its pane torn down) releases them.
    with pytest.raises(asyncio.CancelledError):
        async with prompt_parks.released("conv_a", "kiro:"):
            raise asyncio.CancelledError
    assert prompt_parks.open_keys("conv_a") == ()


def test_oldest_age_tracks_the_oldest_open_park(clock: _Clock) -> None:
    prompt_parks.open_park("conv_a", "cursor:a")
    clock.now += 5
    prompt_parks.open_park("conv_a", "cursor:b")
    clock.now += 5
    assert prompt_parks.oldest_open_age_s("conv_a") == 10.0
    prompt_parks.close_park("conv_a", "cursor:a")
    assert prompt_parks.oldest_open_age_s("conv_a") == 5.0


def test_concurrent_open_and_close(clock: _Clock) -> None:
    def _worker(index: int) -> None:
        for step in range(300):
            key = f"relay-policy:{index}:{step}"
            prompt_parks.open_park("conv_threads", key)
            prompt_parks.close_park("conv_threads", key)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert prompt_parks.open_keys("conv_threads") == ()
