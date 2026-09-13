from __future__ import annotations

import json

import pytest

from omnigent.harnesses.codex_native.rate_limits import normalize_rate_limits, validate_rate_limits


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
