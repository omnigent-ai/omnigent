"""The host makes only scheduled-task calls, signed for the URL's own workspace."""

from __future__ import annotations

import httpx

from omnigent.host.automation_requests import run_automation_request
from omnigent.host.frames import (
    HostAutomationRequestFrame,
    HostAutomationResultFrame,
    decode_host_frame,
    encode_host_frame,
)

API = "https://automation.example.com/api/2.0/agent/scheduled-tasks"


class _Bearer(httpx.Auth):
    def auth_flow(self, request):
        request.headers["Authorization"] = "Bearer owner-token"
        yield request


def _resolver(resolved_host: str = "https://automation.example.com"):
    calls: list[str] = []

    def resolve(host: str):
        calls.append(host)
        return _Bearer(), resolved_host

    return resolve, calls


def _frame(url: str = API, method: str = "POST", body=None) -> HostAutomationRequestFrame:
    return HostAutomationRequestFrame(request_id="r1", method=method, url=url, body=body)


def test_frames_round_trip() -> None:
    request = _frame(body={"scheduled_task": {"display_name": "x"}})
    assert decode_host_frame(encode_host_frame(request)) == request
    result = HostAutomationResultFrame(request_id="r1", status_code=201, body="{}")
    assert decode_host_frame(encode_host_frame(result)) == result


def test_refuses_anything_but_scheduled_task_calls_without_resolving_a_login() -> None:
    resolve, calls = _resolver()
    for frame in (
        _frame(url="http://automation.example.com/api/2.0/agent/scheduled-tasks"),
        _frame(url="https://automation.example.com/api/2.0/clusters/list"),
        _frame(url="https://user:pw@automation.example.com/api/2.0/agent/scheduled-tasks"),
        _frame(method="PUT"),
    ):
        result = run_automation_request(frame, resolve_auth=resolve)
        assert result.status_code == 0 and result.error, frame
    assert calls == []


def test_token_only_goes_to_its_own_workspace() -> None:
    resolve, _ = _resolver(resolved_host="https://other.example.com")
    result = run_automation_request(_frame(), resolve_auth=resolve)
    assert result.status_code == 0 and "other.example.com" in (result.error or "")


def test_signed_call_is_made_and_answered() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"schedule_id": "s-1", "username": "ann_lee"})

    resolve, calls = _resolver()
    result = run_automation_request(
        _frame(body={"scheduled_task": {}}),
        resolve_auth=resolve,
        transport=httpx.MockTransport(handler),
    )
    assert calls == ["https://automation.example.com"]
    assert result.status_code == 200 and "s-1" in result.body and result.error is None
    assert seen[0].headers["Authorization"] == "Bearer owner-token"


def test_missing_login_says_how_to_sign_in() -> None:
    def resolve(host: str):
        raise RuntimeError("no profile")

    result = run_automation_request(_frame(), resolve_auth=resolve)
    assert result.status_code == 0
    assert "databricks auth login --host https://automation.example.com" in (result.error or "")
