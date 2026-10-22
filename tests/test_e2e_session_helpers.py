"""Regression coverage for turn boundaries in the shared E2E pollers."""

from __future__ import annotations

import httpx
import pytest

from tests.e2e import conftest as helpers


def _item(role: str, text: str, response_id: str, *, nested: bool) -> dict:
    data = {"role": role, "content": [{"type": "output_text", "text": text}]}
    return {"type": "message", "response_id": response_id, **({"data": data} if nested else data)}


@pytest.mark.parametrize("nested", [True, False], ids=["persisted", "flat"])
@pytest.mark.parametrize("observe_running", [True, False], ids=["running-edge", "between-polls"])
def test_poll_waits_for_the_requested_turn(monkeypatch, nested, observe_running) -> None:
    previous = [
        _item("user", "before restart", "input-old", nested=nested),
        _item("assistant", "old reply", "native-old", nested=nested),
    ]
    pending = [*previous, _item("user", "after restart", "input-new", nested=nested)]
    # Native harnesses allocate a different id for their output.
    complete = [*pending, _item("assistant", "new reply", "native-new", nested=nested)]
    snapshots = [{"status": "idle", "items": pending}]
    if observe_running:
        snapshots.append({"status": "running", "items": pending})
    snapshots.append({"status": "idle", "items": complete})
    requests = []

    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=snapshots[len(requests) - 1])

    monkeypatch.setattr(helpers, "POLL_INTERVAL_S", 0)
    with httpx.Client(transport=httpx.MockTransport(handle), base_url="http://test") as client:
        result = helpers.poll_session_until_terminal(
            client, session_id="session", response_id="input-new", timeout=1
        )
    assert requests == ["/v1/sessions/session"] * len(snapshots)
    assert result["status"] == "completed"
    assert [item["content"][0]["text"] for item in result["output"]] == ["new reply"]


def test_tool_poll_does_not_reuse_previous_turn_calls() -> None:
    items = [
        _item("user", "old", "input-old", nested=True),
        {"type": "function_call", "response_id": "native-old", "data": {"call_id": "old"}},
        _item("user", "new", "input-new", nested=True),
        {"type": "function_call", "response_id": "native-new", "data": {"call_id": "new"}},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"items": items})),
        base_url="http://test",
    ) as client:
        output = helpers._session_items_for_response(
            client, session_id="session", response_id="input-new"
        )
    assert [item["call_id"] for item in output] == ["new"]


def test_poll_waits_past_resource_events_and_missing_input(monkeypatch) -> None:
    old_reply = _item("assistant", "old reply", "native-old", nested=True)
    pending = [old_reply, _item("user", "new", "input-new", nested=True)]
    resource = {"type": "resource_event", "response_id": "native-new", "data": {}}
    reply = _item("assistant", "new reply", "native-new", nested=True)
    snapshots = iter(
        [
            {"status": "idle", "items": [old_reply]},
            {"status": "idle", "items": [*pending, resource]},
            {"status": "idle", "items": [*pending, resource, reply]},
        ]
    )
    monkeypatch.setattr(helpers, "POLL_INTERVAL_S", 0)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(snapshots))),
        base_url="http://test",
    ) as client:
        result = helpers.poll_session_until_terminal(
            client, session_id="session", response_id="input-new", timeout=1
        )
    assert [item["type"] for item in result["output"]] == ["resource_event", "message"]
    assert result["output"][-1]["content"][0]["text"] == "new reply"


@pytest.mark.parametrize("previous_call", [False, True], ids=["not-started", "stale-call"])
def test_pending_tool_poll_waits_for_this_turn(monkeypatch, previous_call) -> None:
    old_call = {"type": "function_call", "status": "action_required", "data": {"call_id": "old"}}
    pending = [
        _item("user", "old", "input-old", nested=True),
        *([old_call] if previous_call else []),
        _item("user", "new", "input-new", nested=True),
    ]
    new_call = {"type": "function_call", "status": "action_required", "data": {"call_id": "new"}}
    snapshots = iter(
        [
            {"status": "idle", "items": pending},
            {"status": "idle", "items": pending},
            {"status": "waiting", "items": [*pending, new_call]},
        ]
    )
    monkeypatch.setattr(helpers, "POLL_INTERVAL_S", 0)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(snapshots))),
        base_url="http://test",
    ) as client:
        output = helpers.poll_for_pending_tool_calls(
            client, "input-new", session_id="session", timeout=1
        )
    assert [item["call_id"] for item in output] == ["new"]
