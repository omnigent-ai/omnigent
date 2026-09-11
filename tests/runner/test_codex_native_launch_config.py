"""Tests for ``_codex_native_launch_config`` in ``omnigent/runner/app.py``.

The runner fetches a session snapshot over HTTP and validates it before
launching a runner-owned Codex terminal. Each malformed field is meant to
fail loud with a RuntimeError rather than launch Codex with garbage; those
guards were previously unexercised by any direct test. These tests drive the
function with a stub async client returning controlled snapshots.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import omnigent.runner.native.orchestration as _orchestration
from omnigent.runner.app import _codex_native_launch_config


@pytest.fixture(autouse=True)
def retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make launch-config retry backoff instant and record the delays slept.

    Every test in this module drives the retry loop; sleeping the real backoff
    would make the suite slow. Patching the (deliberately extracted) sleep hook
    to a recorder keeps the tests fast and lets them assert the backoff schedule
    without coupling to wall-clock time. ``RUNNER_SERVER_URL`` is set here too so
    tests that reach the config-build step don't fail for a missing-env reason.
    """
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    # raising=False so the retry-behavior tests fail on their behavioral
    # assertion (no recovery) rather than erroring in setup when run against a
    # tree that predates the retry hook.
    monkeypatch.setattr(_orchestration, "_launch_config_retry_sleep", _fake_sleep, raising=False)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    return slept


class _Resp:
    """Minimal stand-in for an httpx response carrying a fixed status + payload."""

    def __init__(self, status_code: int, payload: Any, *, json_raises: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Async client stub whose ``get`` returns a fixed response or raises."""

    def __init__(self, resp: _Resp | None = None, raise_exc: Exception | None = None) -> None:
        self._resp = resp
        self._raise_exc = raise_exc

    async def get(self, url: str, timeout: float | None = None) -> _Resp:
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._resp is not None
        return self._resp


class _SequenceClient:
    """Async client stub that plays a fixed sequence of ``get`` outcomes.

    Each action is either an ``Exception`` to raise or a ``_Resp`` to return,
    consumed in order across calls. Records how many times ``get`` ran so a test
    can assert exactly how many fetch attempts the retry loop made.
    """

    def __init__(self, actions: list[Any]) -> None:
        self._actions = list(actions)
        self.calls = 0

    async def get(self, url: str, timeout: float | None = None) -> _Resp:
        self.calls += 1
        action = self._actions[self.calls - 1]
        if isinstance(action, Exception):
            raise action
        return action


async def _run(client: _Client | None, session_id: str = "conv_1") -> Any:
    return await _codex_native_launch_config(session_id=session_id, server_client=client)


@pytest.mark.asyncio
async def test_missing_client_raises() -> None:
    """No server client means there is no way to fetch config — fail loud."""
    with pytest.raises(RuntimeError, match="server_client is required"):
        await _run(None)


@pytest.mark.asyncio
async def test_http_error_raises() -> None:
    """A transport error fetching the snapshot surfaces as a RuntimeError."""
    client = _Client(raise_exc=httpx.ConnectError("boom"))
    with pytest.raises(RuntimeError, match="Could not fetch Codex launch config"):
        await _run(client)


@pytest.mark.asyncio
async def test_non_200_raises() -> None:
    """A non-200 status is rejected and names the status in the error."""
    client = _Client(_Resp(404, None))
    with pytest.raises(RuntimeError, match="returned 404"):
        await _run(client)


@pytest.mark.asyncio
async def test_invalid_json_raises() -> None:
    """A body that does not parse as JSON is rejected."""
    client = _Client(_Resp(200, None, json_raises=True))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        await _run(client)


@pytest.mark.asyncio
async def test_non_dict_snapshot_raises() -> None:
    """A JSON array (not an object) is not a valid session snapshot."""
    client = _Client(_Resp(200, ["not", "a", "dict"]))
    with pytest.raises(RuntimeError, match="not a JSON object"):
        await _run(client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("terminal_launch_args", "not-a-list", "terminal_launch_args"),
        ("terminal_launch_args", [1, 2], "terminal_launch_args"),
        ("model_override", "", "model_override"),
        ("model_override", 5, "model_override"),
        ("external_session_id", "", "external_session_id"),
        ("workspace", "", "workspace"),
    ],
)
async def test_invalid_field_raises(field: str, value: Any, match: str) -> None:
    """Each malformed optional field is rejected with a field-specific error."""
    client = _Client(_Resp(200, {field: value}))
    with pytest.raises(RuntimeError, match=match):
        await _run(client)


@pytest.mark.asyncio
async def test_happy_path_parses_full_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed snapshot (with fork labels) parses into a launch config."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "terminal_launch_args": ["--config", "approval_policy=on-request"],
        "model_override": "gpt-5.4-mini",
        "external_session_id": "thread_abc",
        "labels": {
            "omnigent.fork.source_id": "conv_source",
            "omnigent.fork.source_external_session_id": "thread_src",
            "omnigent.fork.carry_history": "1",
            "omnigent.codex_native.bypass_sandbox": "1",
        },
    }
    cfg = await _run(_Client(_Resp(200, snapshot)))
    assert cfg.policy_server_url == "http://127.0.0.1:8123"
    assert cfg.terminal_launch_args == ["--config", "approval_policy=on-request"]
    assert cfg.model_override == "gpt-5.4-mini"
    assert cfg.external_session_id == "thread_abc"
    assert cfg.fork_source_id == "conv_source", "Fork source id should be read from labels."
    assert cfg.fork_source_external_id == "thread_src"
    assert cfg.fork_carry_history is True, "carry_history label '1' should parse to True."
    assert cfg.bypass_sandbox is True, "bypass_sandbox label '1' should parse to True."
    assert cfg.workspace.name == "repo", (
        f"Workspace path should resolve from snapshot, got {cfg.workspace}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        None,  # no labels at all
        {},  # labels present but no bypass key
        {"omnigent.codex_native.bypass_sandbox": "0"},  # explicit off
        {"omnigent.codex_native.bypass_sandbox": "true"},  # only "1" arms it
        {"omnigent.codex_native.bypass_sandbox": ""},  # empty string
    ],
)
async def test_bypass_sandbox_defaults_off_unless_label_is_one(
    monkeypatch: pytest.MonkeyPatch, labels: Any
) -> None:
    """
    Fail-safe: ``bypass_sandbox`` is False unless the label is exactly ``"1"``.

    The dangerous full-bypass stance must never be entered by accident, so an
    absent label, an unrelated value, or any near-miss (``"0"``, ``"true"``,
    ``""``) leaves Codex in its normal approval/sandbox stance. Only the
    canonical ``"1"`` (set by the guarded web toggle) arms it.
    """
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot: dict[str, Any] = {"workspace": "/tmp/repo"}
    if labels is not None:
        snapshot["labels"] = labels
    cfg = await _run(_Client(_Resp(200, snapshot)))
    assert cfg.bypass_sandbox is False


@pytest.mark.asyncio
async def test_transient_timeout_recovers_on_retry(retry_sleeps: list[float]) -> None:
    """A first-attempt read timeout is retried, and a follow-up 200 succeeds.

    This is the reported failure: a single runner->server read timeout under
    load used to abort the terminal launch. The idempotent read should instead
    be retried and recover.
    """
    snapshot = {"workspace": "/tmp/repo", "terminal_launch_args": ["--config", "x=y"]}
    client = _SequenceClient([httpx.ReadTimeout("slow"), _Resp(200, snapshot)])
    cfg = await _codex_native_launch_config(session_id="conv_1", server_client=client)
    assert cfg.terminal_launch_args == ["--config", "x=y"]
    assert client.calls == 2, "Should retry once after the transient read timeout."
    assert retry_sleeps == [pytest.approx(0.5)], "One backoff sleep before the retry."


@pytest.mark.asyncio
async def test_persistent_transient_failure_raises_after_attempt_cap(
    retry_sleeps: list[float],
) -> None:
    """A read timeout on every attempt exhausts the bounded retries and fails loud."""
    client = _SequenceClient([httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(RuntimeError, match="Could not fetch Codex launch config"):
        await _codex_native_launch_config(session_id="conv_1", server_client=client)
    assert client.calls == 3, "Should attempt exactly the configured cap, then fail."
    assert retry_sleeps == [pytest.approx(0.5), pytest.approx(1.0)], (
        "Two exponentially-growing backoff sleeps between the three attempts."
    )


@pytest.mark.asyncio
async def test_retryable_status_recovers_on_retry(retry_sleeps: list[float]) -> None:
    """A retryable upstream status (503) is retried, and a follow-up 200 succeeds."""
    snapshot = {"workspace": "/tmp/repo"}
    client = _SequenceClient([_Resp(503, None), _Resp(200, snapshot)])
    cfg = await _codex_native_launch_config(session_id="conv_1", server_client=client)
    assert cfg.workspace.name == "repo"
    assert client.calls == 2, "A 503 should be retried, not surfaced immediately."


@pytest.mark.asyncio
async def test_non_retryable_status_fails_without_retry(retry_sleeps: list[float]) -> None:
    """A non-retryable status (404) fails on the first attempt without consuming retries."""
    client = _SequenceClient([_Resp(404, None)])
    with pytest.raises(RuntimeError, match="returned 404"):
        await _codex_native_launch_config(session_id="conv_1", server_client=client)
    assert client.calls == 1, "A 404 is a hard error, not a transient blip."
    assert retry_sleeps == [], "No backoff on a non-retryable status."


@pytest.mark.asyncio
async def test_non_transient_transport_error_fails_without_retry(
    retry_sleeps: list[float],
) -> None:
    """A non-transient httpx error surfaces immediately, without burning retries."""
    client = _SequenceClient([httpx.HTTPError("nope")])
    with pytest.raises(RuntimeError, match="Could not fetch Codex launch config"):
        await _codex_native_launch_config(session_id="conv_1", server_client=client)
    assert client.calls == 1, "A non-transient error should not be retried."
    assert retry_sleeps == []
