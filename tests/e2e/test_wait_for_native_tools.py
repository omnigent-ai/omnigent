"""Readiness evidence in ``wait_for_native_tools`` is scoped to the probed session."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.e2e import conftest as e2e_conftest

_TOOL = "mcp__omnigent"
_MOCK_URL = "http://mock.invalid"


class _FakeNativeSessions:
    """Idle sessions whose probe turns record mock LLM requests with or without tools."""

    def __init__(self, advertising_sessions: set[str]) -> None:
        self.advertising_sessions = advertising_sessions
        self.mock_requests: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        session_id = request.url.path.split("/")[3]
        if request.method == "POST":
            text = json.loads(request.content)["data"]["content"][0]["text"]
            tools = [{"name": _TOOL}] if session_id in self.advertising_sessions else []
            self.mock_requests.append(
                {"input": [{"role": "user", "content": text}], "tools": tools}
            )
            return httpx.Response(202, json={})
        return httpx.Response(200, json={"status": "idle"})


@pytest.fixture
def fake_sessions(monkeypatch: pytest.MonkeyPatch) -> _FakeNativeSessions:
    sessions = _FakeNativeSessions(advertising_sessions={"manager"})
    monkeypatch.setattr(e2e_conftest, "get_mock_requests", lambda _url: sessions.mock_requests)
    return sessions


def test_unready_session_fails_even_after_another_session_advertised(
    fake_sessions: _FakeNativeSessions,
) -> None:
    with httpx.Client(
        base_url="http://server.invalid", transport=httpx.MockTransport(fake_sessions.handle)
    ) as client:
        e2e_conftest.wait_for_native_tools(client, "manager", _MOCK_URL, _TOOL, timeout_s=2)
        with pytest.raises(AssertionError, match="session worker never advertised"):
            e2e_conftest.wait_for_native_tools(client, "worker", _MOCK_URL, _TOOL, timeout_s=1)


def test_session_passes_once_its_own_probe_advertises(
    fake_sessions: _FakeNativeSessions,
) -> None:
    fake_sessions.advertising_sessions.add("worker")
    with httpx.Client(
        base_url="http://server.invalid", transport=httpx.MockTransport(fake_sessions.handle)
    ) as client:
        e2e_conftest.wait_for_native_tools(client, "manager", _MOCK_URL, _TOOL, timeout_s=2)
        e2e_conftest.wait_for_native_tools(client, "worker", _MOCK_URL, _TOOL, timeout_s=2)
