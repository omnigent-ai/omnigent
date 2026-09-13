"""Unit tests for full-server session polling."""

from __future__ import annotations

import json
from typing import Any

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.full_server_driver import FullServerDriver
from tests.harness_bench.profile import BenchProfile

_PROFILE = BenchProfile(harness="fake", model="m", env_prefix="HARNESS_FAKE_", marker="MARK")


class _Response:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _Stream:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def iter_lines(self):
        for event in self._events:
            yield f"event: {event}"
            yield f"data: {json.dumps({'type': event})}"


class _Client:
    def __init__(
        self,
        snapshots: list[dict[str, Any]],
        *,
        stream_events: list[str] | None = None,
    ) -> None:
        self._snapshots = iter(snapshots)
        self._stream_events = stream_events or []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def get(self, _url: str) -> _Response:
        return _Response(next(self._snapshots))

    def post(self, url: str, json: dict[str, Any]) -> _Response:
        self.posts.append((url, json))
        return _Response({"id": "forked"}, status_code=201)

    def patch(self, url: str, json: dict[str, Any]) -> _Response:
        self.patches.append((url, json))
        return _Response({})

    def stream(self, method: str, url: str, timeout: float):
        return _Stream(self._stream_events)


def _driver(
    snapshots: list[dict[str, Any]],
    *,
    stream_events: list[str] | None = None,
) -> FullServerDriver:
    class _Shared:
        client = _Client(snapshots, stream_events=stream_events)
        runner_id = "runner-test"

    driver = FullServerDriver(_PROFILE, databricks_profile=None, shared=_Shared())
    driver._session_id = "source"
    return driver


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


def test_fork_probe_binds_clone_and_recalls_copied_history(monkeypatch) -> None:
    marker_item = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "MARK"}],
        },
    }
    driver = _driver([{"items": [marker_item]}])

    def _recall(sid: str, prompt: str, *, timeout: float) -> TurnResult:
        assert sid == "forked"
        assert "MARK" not in prompt
        return TurnResult(completed=True, text="MARK")

    monkeypatch.setattr(driver, "_run_turn_on_session", _recall)

    result = driver.fork_probe_turn("MARK")

    assert result.created and result.history_copied and result.recalled
    assert driver._client.patches == [("/v1/sessions/forked", {"runner_id": "runner-test"})]


def test_reasoning_turn_counts_forwarded_deltas(monkeypatch) -> None:
    monkeypatch.setattr("tests.harness_bench.full_server_driver.time.sleep", lambda _: None)
    driver = _driver(
        [],
        stream_events=[
            "response.reasoning.started",
            "response.reasoning_text.delta",
            "response.reasoning_summary_text.delta",
            "response.output_text.delta",
            "response.completed",
        ],
    )

    result = driver.streaming_probe_turn(prompt="reason", timeout=1)

    assert result.completed
    assert result.reasoning_delta_count == 2
    assert result.text_delta_count == 1


def test_reasoning_probe_requests_high_effort(monkeypatch) -> None:
    driver = _driver([])
    counts = iter([2, 3])
    monkeypatch.setattr(driver, "_reasoning_item_count", lambda: next(counts))
    monkeypatch.setattr(
        driver,
        "streaming_probe_turn",
        lambda **kwargs: TurnResult(completed=True, reasoning_delta_count=1),
    )

    result = driver.reasoning_probe_turn()

    assert result.reasoning_delta_count == 1
    assert result.reasoning_item_count == 1
    assert driver._client.patches[-1] == (
        "/v1/sessions/source",
        {"reasoning_effort": "high"},
    )
