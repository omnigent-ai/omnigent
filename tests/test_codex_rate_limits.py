from __future__ import annotations

import asyncio
import json

import pytest

from omnigent.harnesses.codex_native.rate_limits import normalize_rate_limits, validate_rate_limits
from omnigent.harnesses.codex_native.rate_limits_probe import read_rate_limits


def test_normalizer_whitelists_display_fields() -> None:
    raw = json.loads(
        '{"result":{"account":{"email":"private@example.com"},"credits":{"balance":99},'
        '"rateLimitsByLimitId":{"codex":{"limitName":"Codex","token":"secret",'
        '"primary":{"usedPercent":11.4,"windowDurationMins":300,"resetsAt":2000000000}}}}}'
    )
    snapshot = normalize_rate_limits(raw, captured_at=1_900_000_000)
    expected = json.loads(
        '{"captured_at":1900000000,"limits":[{"limit_id":"codex","limit_name":"Codex",'
        '"windows":[{"kind":"primary","used_percent":11.4,"window_duration_mins":300,'
        '"resets_at":2000000000}]}]}'
    )
    assert snapshot == expected
    assert all(value not in json.dumps(snapshot) for value in ("private@example.com", "secret"))
    with pytest.raises(ValueError, match="snapshot"):
        validate_rate_limits({**expected, "account": "private"})
    expected["limits"][0]["windows"][0]["resets_at"] = 1 << 80
    with pytest.raises(ValueError, match="snapshot"):
        validate_rate_limits(expected)


@pytest.mark.parametrize("used", [-1, 101, True, "5", float("nan"), 10**400])
def test_normalizer_rejects_invalid_windows(used: object) -> None:
    raw = {"result": {"rateLimits": {"primary": {"usedPercent": used, "windowDurationMins": 300}}}}
    assert normalize_rate_limits(raw, captured_at=1) is None


@pytest.mark.asyncio
async def test_probe_env_excludes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("OPENAI_API_KEY", "UNRELATED_PROVIDER_SECRET"):
        monkeypatch.setenv(key, "must-not-cross")
    monkeypatch.setenv("CODEX_HOME", "C:/synthetic/codex-home")
    captured: dict[str, object] = {}

    async def capture(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    with pytest.raises(RuntimeError, match="captured"):
        await read_rate_limits("codex.exe")
    env = captured["env"]
    assert isinstance(env, dict) and env["CODEX_HOME"] == "C:/synthetic/codex-home"
    assert "OPENAI_API_KEY" not in env and "UNRELATED_PROVIDER_SECRET" not in env
