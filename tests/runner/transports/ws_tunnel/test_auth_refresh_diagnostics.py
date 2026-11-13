"""Token refresh observations preserve cache, exceptions and credential privacy."""

import json
import logging

import pytest

from omnigent.runner.transports.ws_tunnel import serve


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["token", "none", "fallback", "raised"])
async def test_refresh_outcomes_preserve_behavior(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    cached = "synthetic-cached-credential"
    fresh = "synthetic-new-credential"

    def factory() -> str | None:
        if mode == "fallback":
            raise OSError("synthetic failure")
        if mode == "raised":
            raise RuntimeError("synthetic failure")
        return fresh if mode == "token" else None

    with caplog.at_level(logging.DEBUG, logger=serve.__name__):
        if mode == "raised":
            with pytest.raises(RuntimeError):
                await serve._refresh_auth_token(cached, factory)
        else:
            assert await serve._refresh_auth_token(cached, factory) == (
                fresh if mode == "token" else cached
            )
    records = [
        r
        for r in caplog.records
        if getattr(r, "event_name", "").startswith("runner_tunnel_token_refresh_")
    ]
    assert len(records) == 2
    first, last = [r.attributes for r in records]
    assert first["token_refresh_id"] == last["token_refresh_id"]
    assert last["credential_scope"] == "runner_tunnel"
    assert last["cached_token_present"] is True
    assert (
        last["outcome"]
        == {
            "token": "token_returned",
            "none": "no_token_fallback",
            "fallback": "error_fallback",
            "raised": "raised",
        }[mode]
    )
    assert last["elapsed_ms"] >= 0
    assert cached not in json.dumps(first) + json.dumps(last)
    assert fresh not in json.dumps(first) + json.dumps(last)


@pytest.mark.asyncio
async def test_broken_diagnostic_sink_does_not_change_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic sink error")

    monkeypatch.setattr(serve._logger, "debug", fail)
    monkeypatch.setattr(serve._logger, "log", fail)
    assert await serve._refresh_auth_token("cached", lambda: "fresh") == "fresh"
