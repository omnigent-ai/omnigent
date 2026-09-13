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
async def test_probe_teardown_completes_when_cancelled_during_grace_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during the SIGTERM grace wait still kills and cleans up.

    The child ignores SIGTERM, so ``wait()`` hangs; cancelling the probe
    while it waits must still escalate to ``kill_tree``, close stdin, and
    close the transport before the ``CancelledError`` propagates.
    """
    from omnigent.harnesses.codex_native import rate_limits_probe as probe_mod
    from omnigent.inner import _proc

    terminate_called = asyncio.Event()
    kill_called = asyncio.Event()
    killed = asyncio.Event()
    stdin_closed = asyncio.Event()
    transport_closed = asyncio.Event()

    def fake_terminate(process: object) -> None:
        terminate_called.set()

    def fake_kill(process: object) -> None:
        kill_called.set()
        killed.set()

    def fake_close_transport(proc: object) -> None:
        transport_closed.set()

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            stdin_closed.set()

        async def wait_closed(self) -> None:
            pass

    class _FakeStdout:
        def __init__(self) -> None:
            self._lines = [
                b'{"id":1,"result":{}}\n',
                b'{"id":2,"result":{"rateLimits":{"primary":'
                b'{"usedPercent":5,"windowDurationMins":300}}}}\n',
            ]

        async def readline(self) -> bytes:
            if self._lines:
                return self._lines.pop(0)
            await asyncio.sleep(3600)
            return b""

    class _FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.pid = 1234567
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout()

        async def wait(self) -> int:
            if killed.is_set():
                return 0
            await killed.wait()
            return 0

    fake_proc = _FakeProcess()

    async def fake_create(*args: object, **kwargs: object) -> object:
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(_proc, "terminate_tree", fake_terminate)
    monkeypatch.setattr(_proc, "kill_tree", fake_kill)
    monkeypatch.setattr(probe_mod, "close_subprocess_transport", fake_close_transport)

    task = asyncio.create_task(read_rate_limits("codex"))
    await asyncio.wait_for(terminate_called.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert terminate_called.is_set()
    assert kill_called.is_set()
    assert stdin_closed.is_set()
    assert transport_closed.is_set()


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
