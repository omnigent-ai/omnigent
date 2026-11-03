"""Runtime flag declarations and their refresh from the server."""

from __future__ import annotations

import asyncio
from types import MappingProxyType

import httpx
import pytest

from omnigent import runtime_flags
from omnigent.runtime_flags import (
    RUNTIME_FLAGS_PATH,
    BoolFeatureFlag,
    FloatFeatureFlag,
    IntFeatureFlag,
    RuntimeFlagRefresher,
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


async def test_failed_refresh_keeps_last_values_and_old_server_backs_off() -> None:
    enabled = BoolFeatureFlag("test.enabled", False)
    server = _FakeServer(
        _served({"test.enabled": True}),
        httpx.ConnectError("down"),
        httpx.Response(503),
        httpx.Response(404),
        _served({}),
    )
    refresher = RuntimeFlagRefresher(server.post, default_interval_s=30)

    assert await refresher.refresh_once() == 60
    assert await refresher.refresh_once() == 30
    assert await refresher.refresh_once() == 30
    assert enabled.get_current_value() is True
    assert await refresher.refresh_once() == 3600
    assert enabled.get_current_value() is True
    # An authoritative answer without the flag returns it to its default.
    await refresher.refresh_once()
    assert enabled.get_current_value() is False


async def test_no_declared_flags_sends_no_request() -> None:
    server = _FakeServer()
    assert await RuntimeFlagRefresher(server.post, default_interval_s=42).refresh_once() == 42
    assert server.requests == []


async def test_background_refresh_runs_until_closed() -> None:
    enabled = BoolFeatureFlag("test.enabled", False)
    served = asyncio.Event()

    async def post(path: str, *, json: object) -> httpx.Response:
        served.set()
        return _served({"test.enabled": True})

    refresher = RuntimeFlagRefresher(post)
    refresher.start()
    await asyncio.wait_for(served.wait(), timeout=5)
    await refresher.aclose()
    assert enabled.get_current_value() is True


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
