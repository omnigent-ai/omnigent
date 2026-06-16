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

from omnigent.runner.app import _codex_native_launch_config


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
    assert cfg.workspace.name == "repo", (
        f"Workspace path should resolve from snapshot, got {cfg.workspace}."
    )
