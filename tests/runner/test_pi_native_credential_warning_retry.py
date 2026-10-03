"""``_post_pi_native_credential_warning`` must retry a transient blip.

This warning is the fail-loud signal for a cross-family model/gateway
misroute (e.g. a Claude id served over an openai-only gateway). Losing it to
one transient POST failure would silently turn "fail loud" back into "fail
silent" — the exact regression the pi-native routing fix exists to prevent.
"""

from __future__ import annotations

import httpx
import pytest

import omnigent.runner.native.orchestration as _orchestration
from omnigent.runner.native.orchestration import _post_pi_native_credential_warning


@pytest.fixture(autouse=True)
def retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make retry backoff instant and record the delays slept."""
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(_orchestration, "_launch_config_retry_sleep", _fake_sleep, raising=False)
    return slept


class _Client:
    """Async client stub whose ``post`` raises then recovers, or always fails."""

    def __init__(self, responses: list[Exception | int]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def post(self, path: str, *, json: dict, timeout: float) -> httpx.Response:
        self.calls += 1
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=httpx.Request("POST", "http://test" + path))


@pytest.mark.asyncio
async def test_transient_read_error_recovers_on_retry(retry_sleeps: list[float]) -> None:
    """A read-error blip on attempt 1 must not drop the warning."""
    client = _Client([httpx.ReadError("connection reset"), 200])

    await _post_pi_native_credential_warning(
        session_id="conv_abc",
        server_client=client,
        warning="routing mismatch",  # type: ignore[arg-type]
    )

    assert client.calls == 2
    assert retry_sleeps  # backed off between attempts


@pytest.mark.asyncio
async def test_retryable_5xx_recovers_on_retry(retry_sleeps: list[float]) -> None:
    """A transient 503 must not drop the warning either."""
    client = _Client([503, 200])

    await _post_pi_native_credential_warning(
        session_id="conv_abc",
        server_client=client,
        warning="routing mismatch",  # type: ignore[arg-type]
    )

    assert client.calls == 2


@pytest.mark.asyncio
async def test_persistent_failure_gives_up_without_raising(retry_sleeps: list[float]) -> None:
    """Exhausting every attempt must not fail the launch (best-effort delivery)."""
    client = _Client([httpx.ReadError("a"), httpx.ReadError("b"), httpx.ReadError("c")])

    await _post_pi_native_credential_warning(
        session_id="conv_abc",
        server_client=client,
        warning="routing mismatch",  # type: ignore[arg-type]
    )

    assert client.calls == _orchestration._LAUNCH_CONFIG_FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_non_transient_4xx_does_not_retry(retry_sleeps: list[float]) -> None:
    """A non-transient 4xx must fail fast instead of burning the retry budget."""
    client = _Client([404])

    await _post_pi_native_credential_warning(
        session_id="conv_abc",
        server_client=client,
        warning="routing mismatch",  # type: ignore[arg-type]
    )

    assert client.calls == 1
    assert not retry_sleeps
