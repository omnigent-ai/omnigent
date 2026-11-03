"""Runtime flag declarations and their refresh from the server."""

from __future__ import annotations

import asyncio
import math
from types import MappingProxyType

import httpx
import pytest

from omnigent import runtime_flags
from omnigent.runtime_flags import (
    RUNTIME_FLAGS_PATH,
    BoolFeatureFlag,
    FloatFeatureFlag,
    IntFeatureFlag,
    LongFeatureFlag,
    RuntimeFlagRefresher,
    StringFeatureFlag,
    StringListFeatureFlag,
)


@pytest.fixture(autouse=True)
def isolated_flags(monkeypatch):
    """Give each test an empty flag registry and no served values."""
    monkeypatch.setattr(runtime_flags, "_registry", {})
    monkeypatch.setattr(runtime_flags, "_values", MappingProxyType({}))


class _FakeServer:
    """Answers runtime-flag POSTs with a queued response per call."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, object]] = []

    async def post(self, path: str, *, json: object) -> httpx.Response:
        self.requests.append((path, json))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _served(values: dict[str, object], refresh_after_s: int = 60) -> httpx.Response:
    return httpx.Response(200, json={"values": values, "refresh_after_s": refresh_after_s})


async def test_refresh_serves_values_and_falls_back_to_defaults() -> None:
    enabled = BoolFeatureFlag("test.enabled", False)
    limit = IntFeatureFlag("test.limit", 5)
    ratio = FloatFeatureFlag("test.ratio", 0.5)
    groups = StringListFeatureFlag("test.groups", [])
    server = _FakeServer(
        _served(
            {
                "test.enabled": True,
                "test.limit": "7",  # wrong type: ignored
                "test.ratio": 1,
                "test.groups": ["a", "b"],
                "test.undeclared": True,
            },
            refresh_after_s=5,
        )
    )

    delay = await RuntimeFlagRefresher(server.post).refresh_once()

    assert server.requests == [
        (
            RUNTIME_FLAGS_PATH,
            {
                "flags": [
                    {"name": "test.enabled", "type": "bool", "default": False},
                    {"name": "test.groups", "type": "string_list", "default": []},
                    {"name": "test.limit", "type": "int", "default": 5},
                    {"name": "test.ratio", "type": "float", "default": 0.5},
                ]
            },
        )
    ]
    assert enabled.get_current_value() is True
    assert limit.get_current_value() == 5
    assert ratio.get_current_value() == 1.0
    assert isinstance(ratio.get_current_value(), float)
    assert groups.get_current_value() == ["a", "b"]
    groups.get_current_value().append("mutated")
    assert groups.get_current_value() == ["a", "b"]
    assert delay == 15.0  # suggested delays are clamped to the minimum


async def test_failed_refresh_keeps_last_values_and_old_server_backs_off(monkeypatch) -> None:
    enabled = BoolFeatureFlag("test.enabled", False)
    monkeypatch.setattr(runtime_flags, "_REQUEST_TIMEOUT_S", 0.01)
    server = _FakeServer(
        _served({"test.enabled": True}),
        httpx.ConnectError("down"),
        httpx.Response(503),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"values": None}),
        httpx.Response(200, json={"values": []}),
        httpx.Response(404),
        httpx.Response(405),
        _served({}),
    )
    refresher = RuntimeFlagRefresher(server.post, default_interval_s=30)

    assert await refresher.refresh_once() == 60
    for _ in range(7):  # connection error, 503, invalid JSON, and malformed bodies
        assert await refresher.refresh_once() == 30
        assert enabled.get_current_value() is True
    assert await refresher.refresh_once() == 3600  # 404
    assert await refresher.refresh_once() == 3600  # 405
    assert enabled.get_current_value() is True
    # An authoritative answer without the flag returns it to its default.
    await refresher.refresh_once()
    assert enabled.get_current_value() is False


async def test_timed_out_request_keeps_last_values(monkeypatch) -> None:
    enabled = BoolFeatureFlag("test.enabled", False)
    monkeypatch.setattr(runtime_flags, "_REQUEST_TIMEOUT_S", 0.01)
    await RuntimeFlagRefresher(_FakeServer(_served({"test.enabled": True})).post).refresh_once()

    async def hang(path: str, *, json: object) -> httpx.Response:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    assert await RuntimeFlagRefresher(hang, default_interval_s=30).refresh_once() == 30
    assert enabled.get_current_value() is True


async def test_refresh_includes_all_declared_flags(monkeypatch) -> None:
    monkeypatch.setattr(runtime_flags, "MAX_RUNTIME_FLAGS_PER_REQUEST", 2)
    flags = [BoolFeatureFlag(f"test.flag{index}", False) for index in range(3)]
    everything = {flag.name: True for flag in flags}
    server = _FakeServer(
        _served(everything, refresh_after_s=120),
        _served(everything, refresh_after_s=90),
        _served({}),
        httpx.Response(503),
        _served({}),
        httpx.Response(200, json={"refresh_after_s": 60}),
    )
    refresher = RuntimeFlagRefresher(server.post)

    assert await refresher.refresh_once() == 90  # the shortest suggested delay
    assert [len(body["flags"]) for _, body in server.requests] == [2, 1]
    assert all(flag.get_current_value() for flag in flags)
    # A failed or malformed later batch leaves the previous complete answer in place.
    await refresher.refresh_once()
    assert all(flag.get_current_value() for flag in flags)
    await refresher.refresh_once()
    assert all(flag.get_current_value() for flag in flags)


async def test_no_declared_flags_sends_no_request() -> None:
    server = _FakeServer()
    assert await RuntimeFlagRefresher(server.post, default_interval_s=42).refresh_once() == 42
    assert server.requests == []


async def test_background_refresh_runs_until_closed(monkeypatch) -> None:
    enabled = BoolFeatureFlag("test.enabled", False)
    delays: list[float] = []
    real_sleep = asyncio.sleep
    second_refresh = asyncio.Event()

    async def record_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    async def post(path: str, *, json: object) -> httpx.Response:
        if delays:
            second_refresh.set()
        # No suggested delay, so the refresher waits its default interval.
        return httpx.Response(200, json={"values": {"test.enabled": True}})

    monkeypatch.setattr(runtime_flags.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(runtime_flags.random, "uniform", lambda low, high: 0.0)
    refresher = RuntimeFlagRefresher(post, default_interval_s=42)
    refresher.start()
    task = refresher._task
    await asyncio.wait_for(second_refresh.wait(), timeout=5)
    assert delays[0] == 42
    await refresher.aclose()
    assert enabled.get_current_value() is True
    assert task is not None and task.cancelled()


def test_declarations_validate_name_default_and_conflicts() -> None:
    first = BoolFeatureFlag("test.enabled", False)
    BoolFeatureFlag("test.enabled", False)  # identical re-declaration is allowed
    assert runtime_flags.declared_runtime_flags() == (first,)
    with pytest.raises(ValueError, match="already declared"):
        BoolFeatureFlag("test.enabled", True)
    with pytest.raises(ValueError, match="already declared"):
        IntFeatureFlag("test.enabled", 0)
    with pytest.raises(ValueError, match="invalid runtime flag name"):
        BoolFeatureFlag("1bad name", False)
    with pytest.raises(ValueError, match="invalid runtime flag name"):
        BoolFeatureFlag("test.trailing\n", False)
    with pytest.raises(ValueError, match="wrong type"):
        IntFeatureFlag("test.limit", True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="wrong type"):
        IntFeatureFlag("test.limit", 2**31)
    assert LongFeatureFlag("test.long_max", 2**63 - 1).default_value == 2**63 - 1
    with pytest.raises(ValueError, match="wrong type"):
        LongFeatureFlag("test.long_over", 2**63)
    assert StringFeatureFlag("test.mode", "on").get_current_value() == "on"
    with pytest.raises(ValueError, match="wrong type"):
        StringFeatureFlag("test.mode_number", 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [10**400, -(10**400), True, "1.5"])
def test_float_matcher_rejects_values_a_float_cannot_hold(value) -> None:
    assert runtime_flags.runtime_flag_value_matches("float", value) is False


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 10**400])
async def test_non_finite_float_defaults_are_rejected(value) -> None:
    valid = BoolFeatureFlag("test.enabled", False)
    with pytest.raises(ValueError, match="wrong type"):
        FloatFeatureFlag("test.ratio", value)
    server = _FakeServer(_served({"test.enabled": True}))
    await RuntimeFlagRefresher(server.post).refresh_once()
    assert valid.get_current_value() is True
    assert server.requests[0][1] == {
        "flags": [{"name": "test.enabled", "type": "bool", "default": False}]
    }
