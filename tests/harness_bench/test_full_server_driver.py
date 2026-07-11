"""Unit tests for full-server session polling."""

from __future__ import annotations

from typing import Any

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.full_server_driver import FullServerDriver
from tests.harness_bench.profile import BenchProfile

_PROFILE = BenchProfile(harness="fake", model="m", env_prefix="HARNESS_FAKE_", marker="MARK")


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self._snapshots = iter(snapshots)

    def get(self, _url: str) -> _Response:
        return _Response(next(self._snapshots))


def _driver(snapshots: list[dict[str, Any]]) -> FullServerDriver:
    class _Shared:
        client = _Client(snapshots)

    return FullServerDriver(_PROFILE, databricks_profile=None, shared=_Shared())


def test_poll_session_collects_terminal_snapshot_once(monkeypatch) -> None:
    monkeypatch.setattr("tests.harness_bench.full_server_driver.time.sleep", lambda _: None)
    call = {"type": "function_call", "data": {"call_id": "c1", "name": "list_files"}}
    output = {"type": "function_call_output", "data": {"output": "ok"}}
    driver = _driver(
        [
            {"status": "running", "items": [call]},
            {
                "status": "idle",
                "items": [call, output, {"role": "assistant", "content": [{"text": "done"}]}],
            },
        ]
    )

    result = driver._poll_session("sess", TurnResult(), timeout=1, scan_tools=True)

    assert result.completed
    assert result.text == "done"
    assert result.tool_calls == [{"call_id": "c1", "name": "list_files", "arguments": None}]
    assert result.tool_call_allowed


def test_poll_session_reports_failure(monkeypatch) -> None:
    monkeypatch.setattr("tests.harness_bench.full_server_driver.time.sleep", lambda _: None)
    driver = _driver([{"status": "failed", "last_task_error": {"message": "boom"}}])

    result = driver._poll_session("sess", TurnResult(), timeout=1)

    assert result.failed
    assert result.error == {"message": "boom"}
